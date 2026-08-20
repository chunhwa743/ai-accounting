"""The processing graph.

A graph rather than an agent, because the sequence of steps is known before the
run starts. Letting a model decide the order would buy nothing and cost
determinism, testability, and the ability to answer "why was this coded to 489?"
with a stable record. The branching here - digital PDF versus scan, rule hit
versus model call - is a fixed decision tree, which is a conditional edge, not
reasoning.

There is exactly one cycle: extract -> validate -> extract. The *validator*
decides to loop, never the model, and it is capped.

Two phases, on different units of work. Phase 1 iterates over files and asks
"what does this paperwork say?"; phase 2 iterates over transactions and asks
"what does it mean?". Phase 1 has ground truth and arithmetic to prove it,
which is why it needs no confidence model and its gate almost never fires.
"""

from __future__ import annotations

import logging
import shutil
from uuid import uuid4
from decimal import Decimal
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from ..categorisation import (
    batches,
    categorise_batch,
    find_rule,
    similar_corrections_for_batch,
)
from ..config import get_settings
from ..confidence import Signals, compute_confidence, route_allocation
from ..db import Repositories
from ..extraction import (
    escalating_fields,
    apply_statement_to_document,
    apply_supporting_to_document,
    classify_document,
    extract_statement,
    extract_supporting,
    find_possible_duplicates,
    retry_context,
    to_bank_transactions,
    validate_statement,
)
from ..extraction.extract import SUPPORTING_TYPES
from ..ingestion import FileKind, route
from ..llm import LLMClient, get_llm_client
from ..matching import match_documents
from ..models import (
    AllocationStatus,
    DecisionMethod,
    DocumentType,
    Legibility,
    RunStatus,
    Verdict,
)
from ..db.models import (
    Allocation,
    Document,
)
from ..reference import get_chart_of_accounts, resolve_tax_code
from .state import PipelineState, new_state

log = logging.getLogger(__name__)


class Pipeline:
    """Owns the repositories and the model client for one process.

    Node functions are bound methods so they can reach the database without
    threading handles through the graph state, which would make the state a
    grab-bag rather than a description of progress.
    """

    def __init__(self, repos: Repositories, llm: LLMClient | None = None) -> None:
        self.repos = repos
        self.settings = get_settings()
        self.llm = llm or get_llm_client(self.settings)

    # ------------------------------------------------------------ phase 1

    def ingest(self, state: PipelineState) -> dict:
        """Store each uploaded file and create its Document row.

        The row exists before anything is read, so a crash mid-processing does
        not lose the upload and a re-upload of the same bytes is recognised.
        """
        settings = self.settings
        settings.ensure_dirs()
        document_ids, notes = [], []

        for raw_path in state["file_paths"]:
            source = Path(raw_path)
            routed = route(source)

            existing = self.repos.documents.find_by_hash(state["client_id"], routed.file_hash)
            if existing is not None:
                self.repos.documents.attach_to_run(existing.id, state["run_id"])
                document_ids.append(existing.id)
                notes.append(f"{source.name}: already uploaded, reusing extraction")
                continue

            # The hash makes the name unique; the original filename is kept so
            # stored files stay recognisable when debugging a run.
            stored = settings.upload_dir / f"{routed.file_hash[:12]}-{source.name}"
            if not stored.exists():
                shutil.copy2(source, stored)

            doc = self.repos.documents.create(
                Document(
                    client_id=state["client_id"],
                    original_filename=source.name,
                    storage_uri=str(stored),
                    mime_type=routed.mime_type,
                    file_hash=routed.file_hash,
                    page_count=routed.page_count,
                    uploaded_by=state.get("user_id"),
                ),
                run_id=state["run_id"],
            )
            document_ids.append(doc.id)
            notes.append(f"{source.name}: {routed.kind} - {routed.note}")

        return {"document_ids": document_ids, "log": notes}

    def classify(self, state: PipelineState) -> dict:
        """Call 1, per file. Page one only."""
        statements, supporting, issues, notes = [], [], [], []
        calls = tokens_in = tokens_out = 0

        for doc_id in state["document_ids"]:
            doc = self.repos.documents.get(doc_id)
            if doc.document_type != DocumentType.UNKNOWN:
                # Reused from an earlier run.
                (statements if doc.is_statement else supporting).append(doc_id)
                continue

            routed = route(Path(doc.storage_uri))
            if routed.kind == FileKind.UNSUPPORTED:
                issues.append({
                    "document_id": doc_id,
                    "code": "unsupported_file",
                    "message": f"{doc.original_filename}: {routed.note}",
                })
                continue

            kind, reasoning, t_in, t_out = classify_document(
                routed, self.llm, effort=self.settings.effort_classify
            )
            calls += 1
            tokens_in += t_in
            tokens_out += t_out

            self.repos.documents.set_type(doc_id, kind)
            notes.append(f"{doc.original_filename}: {kind}")

            if kind == DocumentType.BANK_STATEMENT:
                statements.append(doc_id)
            elif kind in SUPPORTING_TYPES:
                supporting.append(doc_id)
            else:
                # OTHER is an honest answer, not a failure - but a person has to
                # say what it is before it can be used.
                issues.append({
                    "document_id": doc_id,
                    "code": "unclassified",
                    "message": (
                        f"{doc.original_filename} could not be identified. "
                        f"{reasoning}"
                    ),
                })

        return {
            "statement_ids": statements,
            "supporting_ids": supporting,
            "extraction_issues": issues,
            "log": notes,
            "llm_calls": state.get("llm_calls", 0) + calls,
            "input_tokens": state.get("input_tokens", 0) + tokens_in,
            "output_tokens": state.get("output_tokens", 0) + tokens_out,
        }

    def extract(self, state: PipelineState) -> dict:
        """Call 2, per file. Whole documents, with a bounded repair cycle."""
        attempts = dict(state.get("extraction_attempts", {}))
        issues, notes, txn_ids = [], [], []
        calls = tokens_in = tokens_out = 0

        for doc_id in state["statement_ids"]:
            doc = self.repos.documents.get(doc_id)

            # Already read, and any field a person had to supply has been
            # supplied. Re-reading would discard their correction and re-raise
            # the problem they just fixed.
            already = self.repos.transactions.list_for_document(doc_id)
            if already and not escalating_fields(doc.field_legibility):
                # Re-check rather than trusting the stored verdict: it may have
                # been recorded before a person supplied the field that was
                # blocking, and everything downstream uses it to decide how far
                # to trust these figures.
                recheck = validate_statement(
                    already, doc.opening_balance, doc.closing_balance,
                    doc.period_start, doc.period_end, doc.field_legibility,
                )
                if doc.reconciles != recheck.reconciles:
                    self.repos.documents.set_reconciles(doc_id, recheck.reconciles)
                txn_ids.extend(t.id for t in already)
                notes.append(
                    f"{doc.original_filename}: already extracted, "
                    f"{len(already)} transactions reused ({recheck.verdict.lower()})"
                )
                continue

            routed = route(Path(doc.storage_uri))
            key = str(doc_id)
            retry_note = None

            for attempt in range(self.settings.max_extraction_attempts):
                attempts[key] = attempt + 1

                extraction, t_in, t_out = extract_statement(
                    routed, self.llm, effort=self.settings.effort_extract, retry_note=retry_note
                )
                if routed.kind != FileKind.TABULAR:
                    calls += 1
                tokens_in += t_in
                tokens_out += t_out

                apply_statement_to_document(doc, extraction)
                transactions = to_bank_transactions(extraction, doc_id, state["client_id"])

                report = validate_statement(
                    transactions,
                    doc.opening_balance,
                    doc.closing_balance,
                    doc.period_start,
                    doc.period_end,
                    doc.field_legibility,
                    extraction.stated_transaction_count,
                )

                if report.verdict != Verdict.FAIL:
                    doc.reconciles = report.reconciles
                    self.repos.documents.update_statement_fields(doc)
                    self.repos.transactions.delete_for_document(doc_id)
                    stored = self.repos.transactions.bulk_create(transactions)
                    txn_ids.extend(t.id for t in stored)
                    notes.append(
                        f"{doc.original_filename}: {len(stored)} transactions, "
                        f"{report.verdict.lower()}"
                    )
                    break

                # The validator decides to loop, never the model, and it gets
                # told exactly what went wrong plus the balances the answer has
                # to chain between - so the retry is more constrained than the
                # first attempt rather than a plain repeat.
                retry_note = retry_context(report, doc.opening_balance, doc.closing_balance)
                notes.append(
                    f"{doc.original_filename}: attempt {attempt + 1} failed "
                    f"({report.summary()[:90]})"
                )
            else:
                doc.reconciles = False
                self.repos.documents.update_statement_fields(doc)
                self.repos.transactions.delete_for_document(doc_id)
                stored = self.repos.transactions.bulk_create(transactions)
                txn_ids.extend(t.id for t in stored)
                for issue in report.blocking_issues:
                    issues.append({
                        "document_id": doc_id,
                        "code": issue.code,
                        "message": issue.message,
                        "line_no": issue.line_no,
                        "page": issue.page,
                        "field": issue.field,
                    })

        # Supporting documents have no arithmetic to check, so no repair cycle.
        for doc_id in state["supporting_ids"]:
            doc = self.repos.documents.get(doc_id)
            routed = route(Path(doc.storage_uri))
            extraction, t_in, t_out = extract_supporting(
                routed, self.llm, effort=self.settings.effort_extract
            )
            calls += 1
            tokens_in += t_in
            tokens_out += t_out
            apply_supporting_to_document(doc, extraction)
            self.repos.documents.update_supporting_fields(doc)
            notes.append(f"{doc.original_filename}: {doc.vendor_name} {doc.total_amount}")

        return {
            "transaction_ids": txn_ids,
            "extraction_attempts": attempts,
            "extraction_issues": issues,
            "log": notes,
            "llm_calls": state.get("llm_calls", 0) + calls,
            "input_tokens": state.get("input_tokens", 0) + tokens_in,
            "output_tokens": state.get("output_tokens", 0) + tokens_out,
        }

    def gate1(self, state: PipelineState) -> dict:
        """Stop only if the paperwork genuinely could not be read.

        Kept deliberately narrow. This gate asks an accountant to open a PDF and
        transcribe a field, which is precisely the work the system exists to
        remove, so only unrecoverable data reaches it. Everything merely
        uncertain carries a penalty into phase 2 and surfaces at gate 2, where
        the question is a judgement they can answer without the document.
        """
        issues = state.get("extraction_issues") or []
        if not issues:
            return {"gate1_required": False}

        self.repos.runs.set_status(state["run_id"], RunStatus.AWAITING_EXTRACTION_REVIEW)
        return {
            "gate1_required": True,
            "log": [f"gate 1: {len(issues)} extraction issue(s) need a person"],
        }

    # ------------------------------------------------------------ phase 2

    def resolve_and_categorise(self, state: PipelineState) -> dict:
        """Phase 2, steps 1 to 5.

        Learned rules first, then document matching, then a single batched call
        for whatever is left, then scoring and routing. Every transaction ends
        with an allocation; only the unresolved ones cost a model call.
        """
        client = self.repos.clients.get(state["client_id"])
        transactions = self.repos.transactions.list_for_run(state["run_id"])
        if not transactions:
            return {"log": ["no transactions to categorise"]}

        rules = self.repos.rules.list_active(client.id)
        corrections = self.repos.corrections.recent_for_client(client.id)
        documents = self.repos.documents.list_supporting(client.id)
        duplicate_ids = {i for pair in find_possible_duplicates(transactions) for i in pair}

        # --- step 1: learned rules ---
        rule_hits = {}
        unresolved = []
        for txn in transactions:
            hit = find_rule(txn, rules)
            if hit:
                rule_hits[txn.id] = hit
                self.repos.rules.touch(hit.rule.id)
            else:
                unresolved.append(txn)

        # --- step 2: document matching, over ALL transactions ---
        matches = match_documents(transactions, documents)

        # --- step 3: one batched call for what is left ---
        results: dict[int, object] = {}
        calls = tokens_in = tokens_out = 0
        for batch in batches(unresolved):
            examples = (
                similar_corrections_for_batch(batch, corrections) if corrections else []
            )
            matched_docs = {
                t.id: matches[t.id].document for t in batch if t.id in matches
            }
            batch_results, t_in, t_out = categorise_batch(
                client, batch, matched_docs, examples, self.llm
            )
            calls += 1
            tokens_in += t_in
            tokens_out += t_out
            for result in batch_results:
                results[result.transaction_id] = result

        # --- steps 4 and 5: score, route, persist ---
        allocation_ids = []
        coa = get_chart_of_accounts()

        for txn in transactions:
            match = matches.get(txn.id)
            hit = rule_hits.get(txn.id)
            result = results.get(txn.id)

            if hit:
                account_code = hit.rule.account_id
                tax_code = hit.rule.tax_code or resolve_tax_code(
                    account_code, client.profile.gst_registered
                )
                method = DecisionMethod.RULE
                reasoning = hit.explain()
                question = None
                needs_split = False
                alternatives = []
                identifiable = True   # a human taught this merchant
            elif result is not None:
                account_code = result.account_code if coa.exists(result.account_code) else None
                if result.account_code and account_code is None:
                    log.warning("model returned unknown account %r", result.account_code)
                tax_code = (
                    result.tax_code
                    if result.tax_code
                    else resolve_tax_code(account_code, client.profile.gst_registered)
                )
                method = DecisionMethod.LLM
                reasoning = result.reasoning
                question = result.clarification_question
                needs_split = result.needs_split
                alternatives = result.alternatives
                identifiable = result.identifiable
            else:
                account_code = tax_code = None
                method = DecisionMethod.LLM
                reasoning = "no categorisation was returned for this transaction"
                question = "We could not code this transaction. Could you describe it?"
                needs_split = False
                alternatives = []
                identifiable = False

            score = compute_confidence(
                Signals(
                    decision_method=method,
                    alternatives=list(alternatives),
                    rule_confirm_count=hit.rule.confirm_count if hit else 0,
                    document_match_score=match.score if match else None,
                    description_legibility=txn.field_legibility.get("raw_description"),
                    reconciles=self._reconciles(txn.document_id),
                    amount=txn.amount,
                    has_document=match is not None,
                )
            )

            routing = route_allocation(
                account_code=account_code,
                tax_code=tax_code,
                confidence=score.value,
                txn=txn,
                profile=client.profile,
                decision_method=method,
                is_possible_duplicate=txn.id in duplicate_ids,
                needs_split=needs_split,
                identifiable=identifiable,
            )

            explanation = reasoning or ""
            if routing.gated:
                explanation = f"{explanation}\n\nFlagged for review: {routing.reason}".strip()

            allocation = self.repos.allocations.create(
                Allocation(
                    bank_transaction_id=txn.id,
                    run_id=state["run_id"],
                    amount=txn.amount,
                    account_id=account_code,
                    tax_code=tax_code,
                    decision_method=method,
                    confidence=score.value,
                    status=routing.status,
                    reasoning=explanation,
                    question=question,
                    matched_document_id=match.document.id if match else None,
                    matched_rule_id=hit.rule.id if hit else None,
                )
            )
            allocation_ids.append(allocation.id)

        counts = self.repos.allocations.status_counts(state["run_id"])
        return {
            "allocation_ids": allocation_ids,
            "resolved_by_rule": len(rule_hits),
            "resolved_by_llm": len(results),
            "document_matches": {str(k): v.score for k, v in matches.items()},
            "llm_calls": state.get("llm_calls", 0) + calls,
            "input_tokens": state.get("input_tokens", 0) + tokens_in,
            "output_tokens": state.get("output_tokens", 0) + tokens_out,
            "log": [
                f"{len(rule_hits)} resolved by learned rules with no model call",
                f"{len(matches)} supporting documents matched",
                f"{len(unresolved)} categorised in {calls} batched call(s)",
                "routing: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
            ],
        }

    def _reconciles(self, document_id: int) -> bool | None:
        doc = self.repos.documents.get(document_id)
        return doc.reconciles if doc else None

    def gate2(self, state: PipelineState) -> dict:
        """Pause once, with everything uncertain batched together.

        Interrupting on the first low-confidence transaction would mean twenty
        round trips for one statement. An accountant wants a single review
        session, so the run pauses here and the API resumes it once they are
        done.
        """
        counts = self.repos.allocations.status_counts(state["run_id"])
        needing_attention = counts.get("NEEDS_REVIEW", 0) + counts.get("CLIENT_QUERY", 0)

        run = self.repos.runs.get(state["run_id"])
        self.repos.runs.add_usage(
            state["run_id"],
            state.get("input_tokens", 0) - run.input_tokens,
            state.get("output_tokens", 0) - run.output_tokens,
        )
        self.repos.runs.set_status(state["run_id"], RunStatus.AWAITING_REVIEW)

        return {
            "gate2_required": needing_attention > 0,
            "log": [
                f"gate 2: {counts.get('AUTO_POSTED', 0)} auto-posted, "
                f"{needing_attention} awaiting a person"
            ],
        }


# ---------------------------------------------------------------- assembly


def build_graph(pipeline: Pipeline, checkpointer=None):
    graph = StateGraph(PipelineState)

    graph.add_node("ingest", pipeline.ingest)
    graph.add_node("classify", pipeline.classify)
    graph.add_node("extract", pipeline.extract)
    graph.add_node("gate1", pipeline.gate1)
    graph.add_node("categorise", pipeline.resolve_and_categorise)
    graph.add_node("gate2", pipeline.gate2)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "classify")
    graph.add_edge("classify", "extract")
    graph.add_edge("extract", "gate1")

    # The only conditional edge in phase 1: stop for a person, or carry on.
    graph.add_conditional_edges(
        "gate1",
        lambda state: "halt" if state.get("gate1_required") and not state.get("gate1_resolved")
        else "continue",
        {"halt": END, "continue": "categorise"},
    )

    graph.add_edge("categorise", "gate2")
    graph.add_edge("gate2", END)

    return graph.compile(checkpointer=checkpointer)


def open_checkpointer():
    """Durable checkpoints, so a run survives a restart while it waits.

    Review can take days - the accountant has to hear back from the client -
    so the pause cannot live in process memory.

    LangGraph keeps its own tables. They live in the same database but their
    own schema, so they do not sit among the application's and Alembic's
    autogenerate does not try to manage them.
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    settings = get_settings()
    settings.ensure_dirs()
    # LangGraph uses psycopg directly, so it takes a plain DSN rather than the
    # SQLAlchemy URL form.
    dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
    return PostgresSaver.from_conn_string(dsn)


_checkpointer_ready = False


def ensure_checkpointer_tables() -> None:
    """Create LangGraph's tables once per process."""
    global _checkpointer_ready
    if _checkpointer_ready:
        return
    with open_checkpointer() as saver:
        saver.setup()
    _checkpointer_ready = True


def run_pipeline(
    repos: Repositories,
    client_id: int,
    file_paths: list[str],
    user_id: int | None = None,
    llm: LLMClient | None = None,
    run_id: int | None = None,
) -> PipelineState:
    """Execute a run up to the next gate and return the final state.

    ``run_id`` reuses a row created by the caller, which is how the API can
    return an id immediately and process in the background without ending up
    with two rows for one job.
    """
    from ..db.models import Run

    pipeline = Pipeline(repos, llm)
    run = (
        repos.runs.get(run_id)
        if run_id is not None
        else repos.runs.create(
            Run(
                client_id=client_id,
                started_by=user_id,
                model_used=get_settings().model_primary,
            )
        )
    )
    if run is None:
        raise ValueError(f"no run {run_id}")

    # A thread id has to be globally unique, not merely unique among current
    # rows. LangGraph's checkpoint tables are not managed by Alembic, so they
    # outlive a schema rebuild - and run ids restart at 1 after one. Keying the
    # thread on the id alone would resume a previous run's checkpoint and
    # replay its state. An existing id is reused so a paused run can resume.
    thread_id = run.langgraph_thread_id or f"run-{run.id}-{uuid4().hex[:8]}"
    ensure_checkpointer_tables()

    with open_checkpointer() as checkpointer:
        app = build_graph(pipeline, checkpointer)
        state = new_state(run.id, client_id, file_paths, user_id)
        final = app.invoke(state, config={"configurable": {"thread_id": thread_id}})

    repos.runs.set_thread_id(run.id, thread_id)
    return final

"""HTTP API.

The frontend is built separately, so this is the whole contract. See
HANDOFF_FRONTEND.md for the workflow semantics that OpenAPI cannot express -
what each status means, when to poll, which fields are safe to bulk-edit.

Runs execute in a background task because a statement takes tens of seconds to
read. The gates are not modelled as HTTP long-polls: a run sits in
AWAITING_REVIEW until an accountant finishes, which can be days.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..auth import AuthError, authenticate, create_access_token, user_from_token
from ..config import get_settings
from ..db import Repositories
from ..db.models import User
from ..export import export_all, run_summary
from ..graph import run_pipeline
from ..llm import get_llm_client
from ..models import AllocationStatus, ClientProfile, RunStatus
from ..db.models import Client
from ..reference import get_chart_of_accounts, get_tax_codes
from ..review import ReviewService
from . import schemas as api

log = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(
    title=settings.api_title,
    version="0.1.0",
    description=(
        "Reads a client's bank statement and supporting documents, codes each "
        "transaction to a general ledger account, scores how sure it is, and "
        "routes what it cannot resolve to an accountant. "
        "Sign in at POST /api/v1/auth/login and send the token as "
        "`Authorization: Bearer <token>`. Accounts are seeded, not "
        "self-registered."
    ),
)

# In-memory progress for the SSE stream. The database remains the source of
# truth; this only carries step-by-step messages while a run is executing.
_progress: dict[int, list[str]] = {}


def get_repos() -> Repositories:
    return Repositories.open()


bearer = HTTPBearer(auto_error=False, description="Token from POST /api/v1/auth/login")


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    repos: Repositories = Depends(get_repos),
) -> User:
    """Resolve the signed-in accountant, or reject the request.

    Every endpoint depends on this rather than on a shared key, because
    `approved_by` and `corrected_by` have to name a real person - a set of books
    that nobody is recorded as having signed off is not much of an audit trail.
    """
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="missing bearer token - sign in at POST /api/v1/auth/login",
        )
    try:
        return user_from_token(repos, credentials.credentials, settings)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def error(status: int, code: str, message: str, **details):
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or None}},
    )


@app.exception_handler(ValueError)
async def value_error_handler(request, exc: ValueError):
    # Domain rules raise ValueError with a message written for a person, e.g.
    # "the parts total 900.00 but the bank line is 1000.00".
    return error(400, "invalid_request", str(exc))


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {
        "status": "ok",
        "model": settings.model_primary,
        "offline_provider": settings.use_stub_llm or not settings.openai_api_key,
    }


# ---------------------------------------------------------------- auth


@app.post("/api/v1/auth/login", response_model=api.TokenResponse, tags=["auth"])
def login(body: api.LoginRequest, repos: Repositories = Depends(get_repos)):
    """Exchange an email and password for an access token.

    There is no sign-up endpoint by design: a firm decides who works on its
    clients' books, so accounts are seeded rather than self-registered. See the
    README for the seeded users and their password.
    """
    try:
        user = authenticate(repos, body.email, body.password)
    except AuthError as exc:
        # One message for both an unknown email and a wrong password, so this
        # cannot be used to discover which addresses exist.
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    token, expires_in = create_access_token(user, settings)
    return api.TokenResponse(
        access_token=token,
        expires_in=expires_in,
        user=_user_out(user),
    )


@app.get("/api/v1/auth/me", response_model=api.UserOut, tags=["auth"])
def whoami(user: User = Depends(current_user)):
    """Who the current token belongs to. Useful for restoring a session."""
    return _user_out(user)


def _user_out(user: User) -> api.UserOut:
    return api.UserOut(
        id=user.id, name=user.name, email=user.email, last_login_at=user.last_login_at
    )


# ---------------------------------------------------------------- reference


@app.get("/api/v1/chart-of-accounts", response_model=list[api.AccountOut], tags=["reference"])
def chart_of_accounts(user: User = Depends(current_user)):
    return [
        api.AccountOut(
            code=a.code, name=a.name, type=str(a.type),
            default_tax_code=a.default_tax_code, risk_level=str(a.risk_level),
            normal_balance=a.normal_balance, notes=a.notes,
        )
        for a in get_chart_of_accounts().active
    ]


@app.get("/api/v1/tax-codes", response_model=list[api.TaxCodeOut], tags=["reference"])
def tax_codes(user: User = Depends(current_user)):
    codes = get_tax_codes()
    return [
        api.TaxCodeOut(
            code=code, name=entry.name, rate=entry.rate, claimable=entry.claimable,
            applies_to=entry.applies_to, requires_review=codes.requires_review(code),
            description=entry.description,
        )
        for code, entry in codes._codes.items()
    ]


# ---------------------------------------------------------------- clients


@app.post("/api/v1/clients", response_model=api.ClientOut, tags=["clients"], status_code=201)
def create_client(
    body: api.ClientCreate,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    client = repos.clients.create(
        Client(
            name=body.name,
            uen=body.uen,
            profile=ClientProfile(**body.profile.model_dump()),
        )
    )
    return _client_out(client)


@app.get("/api/v1/clients", response_model=list[api.ClientOut], tags=["clients"])
def list_clients(repos: Repositories = Depends(get_repos), user: User = Depends(current_user)):
    return [_client_out(c) for c in repos.clients.list()]


@app.get("/api/v1/clients/{client_id}/profile", tags=["clients"])
def get_profile(
    client_id: int,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    client = repos.clients.get(client_id)
    if client is None:
        raise HTTPException(404, "no such client")
    return json.loads(client.profile.model_dump_json())


@app.patch("/api/v1/clients/{client_id}/profile", tags=["clients"])
def update_profile(
    client_id: int,
    body: api.ClientProfileIn,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    client = repos.clients.get(client_id)
    if client is None:
        raise HTTPException(404, "no such client")
    # learned_facts are appended by the system as clients answer queries, so a
    # profile edit must not silently wipe them.
    profile = ClientProfile(**body.model_dump(), learned_facts=client.profile.learned_facts)
    repos.clients.update_profile(client_id, profile)
    return json.loads(profile.model_dump_json())


def _client_out(client: Client) -> api.ClientOut:
    return api.ClientOut(
        id=client.id, name=client.name, uen=client.uen,
        profile=json.loads(client.profile.model_dump_json()),
        created_at=client.created_at,
    )


# ---------------------------------------------------------------- documents


@app.post("/api/v1/clients/{client_id}/documents", tags=["documents"], status_code=201)
async def upload_documents(
    client_id: int,
    files: list[UploadFile] = File(...),
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    """Accept one or more files. Nothing is read yet - that happens in a run.

    The upload names the client explicitly. The account holder extracted from a
    statement is used only to verify that choice, never to make it: posting one
    company's statement into another's books is unrecoverable.
    """
    if repos.clients.get(client_id) is None:
        raise HTTPException(404, "no such client")

    settings.ensure_dirs()
    staged = []
    for upload in files:
        target = Path(tempfile.mkdtemp(dir=settings.upload_dir)) / (upload.filename or "upload.bin")
        size = 0
        with open(target, "wb") as handle:
            while chunk := await upload.read(1 << 20):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, f"{upload.filename} exceeds the 50 MB limit")
                handle.write(chunk)
        staged.append({"filename": upload.filename, "path": str(target), "bytes": size})

    return {"client_id": client_id, "staged": staged}


@app.get("/api/v1/documents/{document_id}", response_model=api.DocumentOut, tags=["documents"])
def get_document(
    document_id: int,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    doc = repos.documents.get(document_id)
    if doc is None:
        raise HTTPException(404, "no such document")
    return _document_out(doc)


@app.get("/api/v1/documents/{document_id}/content", tags=["documents"])
def get_document_content(
    document_id: int,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    """The original file, so a reviewer can see it beside the coding."""
    doc = repos.documents.get(document_id)
    if doc is None:
        raise HTTPException(404, "no such document")
    return FileResponse(doc.storage_uri, media_type=doc.mime_type,
                        filename=doc.original_filename)


def _document_out(doc) -> api.DocumentOut:
    return api.DocumentOut(
        id=doc.id, client_id=doc.client_id, document_type=doc.document_type,
        original_filename=doc.original_filename, mime_type=doc.mime_type,
        page_count=doc.page_count,
        field_legibility={k: str(v) for k, v in doc.field_legibility.items()},
        period_start=doc.period_start, period_end=doc.period_end,
        opening_balance=doc.opening_balance, closing_balance=doc.closing_balance,
        bank_name=doc.bank_name, account_number=doc.account_number,
        reconciles=doc.reconciles, vendor_name=doc.vendor_name,
        doc_number=doc.doc_number, doc_date=doc.doc_date,
        total_amount=doc.total_amount, tax_amount=doc.tax_amount, summary=doc.summary,
    )


# ---------------------------------------------------------------- runs


@app.post("/api/v1/clients/{client_id}/runs", tags=["runs"], status_code=202)
def start_run(
    client_id: int,
    body: api.RunCreate,
    background: BackgroundTasks,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    """Start processing. Returns immediately; poll or stream for progress."""
    if repos.clients.get(client_id) is None:
        raise HTTPException(404, "no such client")

    paths = list(body.file_paths or [])
    for doc_id in body.document_ids or []:
        doc = repos.documents.get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no document {doc_id}")
        paths.append(doc.storage_uri)

    if not paths:
        raise HTTPException(400, "supply document_ids or file_paths")

    from ..db.models import Run

    run = repos.runs.create(Run(client_id=client_id, model_used=settings.model_primary))
    _progress[run.id] = ["run queued"]
    background.add_task(_execute_run, run.id, client_id, paths)
    return {"run_id": run.id, "status": "RUNNING"}


def _execute_run(run_id: int, client_id: int, paths: list[str]) -> None:
    """Process in the background, reusing the run row the caller was given."""
    repos = Repositories.open()
    try:
        state = run_pipeline(repos, client_id, paths, user_id=None, run_id=run_id)
        _progress[run_id] = state.get("log", [])
        if state.get("gate1_required"):
            # Stopped because something could not be read. The run stays at
            # AWAITING_EXTRACTION_REVIEW until a person supplies the field.
            _progress[run_id].append("waiting: extraction needs a person")
    except Exception as exc:  # noqa: BLE001
        log.exception("run %s failed", run_id)
        repos.runs.set_status(run_id, RunStatus.FAILED, str(exc))
        _progress.setdefault(run_id, []).append(f"failed: {exc}")
    finally:
        repos.close()


@app.get("/api/v1/runs/{run_id}", response_model=api.RunOut, tags=["runs"])
def get_run(
    run_id: int,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    run = repos.runs.get(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    summary = run_summary(repos, run_id)
    return api.RunOut(
        id=run.id, client_id=run.client_id, status=run.status,
        model_used=run.model_used, llm_calls=run.llm_calls,
        input_tokens=run.input_tokens, output_tokens=run.output_tokens,
        started_at=run.started_at, completed_at=run.completed_at,
        by_status=summary["by_status"],
        by_decision_method=summary["by_decision_method"],
        auto_post_rate=summary["auto_post_rate"],
        needs_attention=summary["needs_attention"],
    )


@app.get("/api/v1/runs/{run_id}/events", tags=["runs"])
async def run_events(
    run_id: int,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    """Server-sent progress. Closes when the run reaches a gate or finishes."""

    async def stream():
        sent = 0
        for _ in range(600):
            messages = _progress.get(run_id, [])
            while sent < len(messages):
                yield f"event: progress\ndata: {json.dumps({'message': messages[sent]})}\n\n"
                sent += 1

            run = repos.runs.get(run_id)
            if run and run.status in (
                RunStatus.COMPLETED, RunStatus.FAILED,
                RunStatus.AWAITING_REVIEW, RunStatus.AWAITING_EXTRACTION_REVIEW,
            ):
                yield f"event: done\ndata: {json.dumps({'status': str(run.status)})}\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/v1/runs/{run_id}/transactions", tags=["review"])
def run_transactions(
    run_id: int,
    status: AllocationStatus | None = Query(default=None),
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    """Allocations for a run, most in need of attention first."""
    allocations = repos.allocations.list_for_run(run_id, status)
    coa = get_chart_of_accounts()

    priority = {
        AllocationStatus.CLIENT_QUERY: 0,
        AllocationStatus.NEEDS_REVIEW: 1,
        AllocationStatus.APPROVED: 2,
        AllocationStatus.AUTO_POSTED: 3,
    }
    rows = []
    for allocation in allocations:
        txn = repos.transactions.get(allocation.bank_transaction_id)
        rows.append({
            "allocation": _allocation_out(allocation, coa),
            "transaction": _transaction_out(txn),
        })
    rows.sort(key=lambda r: (
        priority.get(r["allocation"].status, 9),
        -float(r["allocation"].amount),
    ))
    return {"run_id": run_id, "count": len(rows), "items": rows}


@app.get("/api/v1/runs/{run_id}/issues", response_model=list[api.ExtractionIssueOut], tags=["review"])
def run_issues(
    run_id: int,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    """Fields a person must supply before the run can continue.

    Only identifiers reach here - account numbers, references, invoice numbers -
    because a wrong character in one of those means a different real entity and
    nothing can verify it. Unclear ordinary text does not stop the run.
    """
    from ..extraction import escalating_fields

    issues = []
    for doc in repos.documents.list_for_run(run_id):
        for field in escalating_fields(doc.field_legibility):
            issues.append(api.ExtractionIssueOut(
                document_id=doc.id, code="unreadable_identifier", field=field,
                message=(
                    f"{doc.original_filename}: {field} could not be read reliably. "
                    f"A different reading would refer to a different entity."
                ),
            ))
        if doc.reconciles is False:
            issues.append(api.ExtractionIssueOut(
                document_id=doc.id, code="balance_mismatch",
                message=f"{doc.original_filename}: the statement does not reconcile",
            ))
    return issues


@app.post("/api/v1/runs/{run_id}/complete", tags=["review"])
def complete_run(
    run_id: int,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    """Close the run once the flagged items have been dealt with.

    Sign-off is at the batch level: the reviewer examines the exceptions and
    takes responsibility for the whole run, rather than initialling every line.
    """
    run = repos.runs.get(run_id)
    if run is None:
        raise HTTPException(404, "no such run")

    outstanding = repos.allocations.status_counts(run_id)
    remaining = outstanding.get("NEEDS_REVIEW", 0) + outstanding.get("CLIENT_QUERY", 0)
    repos.runs.complete(run_id, None)
    exported = export_all(repos, run_id, settings.export_dir)
    return {
        "run_id": run_id,
        "status": "COMPLETED",
        "still_unresolved": remaining,
        "exports": {k: Path(v).name for k, v in exported.items()},
    }


@app.get("/api/v1/runs/{run_id}/export", tags=["review"])
def export_run(
    run_id: int,
    format: str = Query(default="xlsx", pattern="^(xlsx|csv|json|journal)$"),
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    from ..export import build_journal, client_queries, journal_csv, review_csv, review_xlsx

    if format == "json":
        return {
            "summary": run_summary(repos, run_id),
            "client_queries": client_queries(repos, run_id),
        }
    if format == "csv":
        return StreamingResponse(
            iter([review_csv(repos, run_id)]), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="run-{run_id}-review.csv"'},
        )
    if format == "journal":
        return StreamingResponse(
            iter([journal_csv(build_journal(repos, run_id))]), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="run-{run_id}-journal.csv"'},
        )

    settings.ensure_dirs()
    path = settings.export_dir / f"run-{run_id}-review.xlsx"
    review_xlsx(repos, run_id, path)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


# ---------------------------------------------------------------- review


@app.get("/api/v1/transactions/{transaction_id}", response_model=api.TransactionDetail, tags=["review"])
def transaction_detail(
    transaction_id: int,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    txn = repos.transactions.get(transaction_id)
    if txn is None:
        raise HTTPException(404, "no such transaction")

    coa = get_chart_of_accounts()
    allocations = repos.allocations.list_for_transaction(transaction_id)
    matched = None
    if allocations and allocations[0].matched_document_id:
        matched = repos.documents.get(allocations[0].matched_document_id)

    return api.TransactionDetail(
        transaction=_transaction_out(txn),
        allocations=[_allocation_out(a, coa) for a in allocations],
        document=_document_out(repos.documents.get(txn.document_id)),
        matched_document=_document_out(matched) if matched else None,
    )


@app.post("/api/v1/allocations/{allocation_id}/review", response_model=api.ReviewResult, tags=["review"])
def review_allocation(
    allocation_id: int,
    body: api.ReviewAction,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    """Approve, correct, or split one allocation.

    `create_rule` means "always do this for this client". The model is asked
    once what pattern the rule should match, and the response reports how many
    past transactions it would have captured so the reviewer can sanity-check
    it. Rule creation is refused if the description was not read cleanly.
    """
    service = ReviewService(repos, get_llm_client(settings))
    coa = get_chart_of_accounts()

    if body.action == "approve":
        outcome = service.approve(allocation_id, user.id, create_rule=body.create_rule)
    elif body.action == "override":
        if not body.account_code:
            raise HTTPException(400, "override requires account_code")
        outcome = service.override(
            allocation_id, user.id, body.account_code, body.tax_code,
            body.note, body.create_rule,
        )
    elif body.action == "split":
        if not body.parts:
            raise HTTPException(400, "split requires parts")
        parts = [(p["account_code"], Decimal(str(p["amount"]))) for p in body.parts]
        created = service.split(allocation_id, user.id, parts, body.note)
        return api.ReviewResult(
            allocation=_allocation_out(created[0], coa),
            message=f"split into {len(created)} allocations",
        )
    else:
        raise HTTPException(400, f"unknown action {body.action!r}")

    return api.ReviewResult(
        allocation=_allocation_out(outcome.allocation, coa),
        message=outcome.message,
        rule_created=(
            {
                "id": outcome.rule.id,
                "match_pattern": outcome.rule.match_pattern,
                "account_id": outcome.rule.account_id,
            }
            if outcome.rule else None
        ),
        rule_preview_count=len(outcome.rule_preview) if outcome.rule_preview else None,
        rule_blocked_reason=outcome.rule_blocked_reason,
    )


@app.post("/api/v1/runs/{run_id}/bulk-review", tags=["review"])
def bulk_review(
    run_id: int,
    body: api.BulkReview,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    service = ReviewService(repos, get_llm_client(settings))
    done, skipped = [], []

    for allocation_id in body.allocation_ids:
        try:
            service.approve(allocation_id, user.id, create_rule=body.create_rule)
            done.append(allocation_id)
        except ValueError as exc:
            # Most commonly an unresolved allocation: there is no account to
            # approve, so it stays a client query.
            skipped.append({"allocation_id": allocation_id, "reason": str(exc)})

    return {"approved": done, "skipped": skipped}


@app.post("/api/v1/allocations/{allocation_id}/query/answer", tags=["review"])
def answer_query(
    allocation_id: int,
    body: api.QueryAnswer,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    """Record what the client said.

    The answer becomes a durable fact on the client profile, so the next run
    has the context this one lacked.
    """
    service = ReviewService(repos, get_llm_client(settings))
    outcome = service.answer_query(allocation_id, user.id, body.answer, body.account_code)
    return {
        "allocation_id": allocation_id,
        "message": outcome.message,
        "learned_fact_recorded": True,
    }


@app.post("/api/v1/extraction-fix", tags=["review"])
def fix_extraction(
    body: api.ExtractionFix,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    """Supply a value the machine could not read.

    Clears the legibility flag on that field, so the run can continue. Unlike a
    categorisation correction this teaches nothing - you cannot generalise
    "read this smudge as a 9" - but it is recorded for audit and for the
    extraction quality metric.
    """
    service = ReviewService(repos, get_llm_client(settings))
    correction = service.correct_extraction(
        user.id, body.field_name, body.new_value,
        document_id=body.document_id, transaction_id=body.transaction_id,
    )
    return {
        "correction_id": correction.id,
        "field": body.field_name,
        "old_value": correction.old_value,
        "new_value": correction.new_value,
    }


# ---------------------------------------------------------------- memory


@app.get("/api/v1/clients/{client_id}/memory/rules", response_model=list[api.RuleOut], tags=["memory"])
def list_rules(
    client_id: int,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    """What the system has learned about this client.

    Deliberately inspectable and editable: a rule that has started producing
    corrections has gone stale, and the accountant is the one who decides.
    """
    coa = get_chart_of_accounts()
    return [
        api.RuleOut(
            id=r.id, client_id=r.client_id, match_pattern=r.match_pattern,
            match_type=str(r.match_type), account_id=r.account_id,
            account_name=(coa.get(r.account_id).name if coa.get(r.account_id) else None),
            tax_code=r.tax_code, confirm_count=r.confirm_count,
            is_active=r.is_active, last_applied_at=r.last_applied_at,
            created_at=r.created_at,
        )
        for r in repos.rules.list_all(client_id)
    ]


@app.delete("/api/v1/clients/{client_id}/memory/rules/{rule_id}", tags=["memory"], status_code=204)
def delete_rule(
    client_id: int,
    rule_id: int,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    repos.rules.deactivate(rule_id)


@app.get("/api/v1/clients/{client_id}/metrics", tags=["memory"])
def client_metrics(
    client_id: int,
    repos: Repositories = Depends(get_repos),
    user: User = Depends(current_user),
):
    """Per-run history, which is where the learning shows up.

    Expect the share resolved from rules to climb and model calls to fall.
    """
    runs = []
    for run in repos.runs.list_for_client(client_id):
        summary = run_summary(repos, run.id)
        runs.append({
            "run_id": run.id,
            "started_at": run.started_at,
            "status": str(run.status),
            "transactions": summary["transactions"],
            "auto_post_rate": summary["auto_post_rate"],
            "resolved_without_model": summary["resolved_without_model"],
            "llm_calls": run.llm_calls,
            "needs_attention": summary["needs_attention"],
        })
    return {
        "client_id": client_id,
        "active_rules": len(repos.rules.list_active(client_id)),
        "runs": runs,
    }


# ---------------------------------------------------------------- helpers


def _transaction_out(txn) -> api.TransactionOut:
    return api.TransactionOut(
        id=txn.id, document_id=txn.document_id, line_no=txn.line_no,
        txn_date=txn.txn_date, raw_description=txn.raw_description,
        bank_reference=txn.bank_reference, money_in=txn.money_in,
        money_out=txn.money_out, balance_after=txn.balance_after, page=txn.page,
        field_legibility={k: str(v) for k, v in txn.field_legibility.items()},
    )


def _allocation_out(allocation, coa) -> api.AllocationOut:
    account = coa.get(allocation.account_id)
    return api.AllocationOut(
        id=allocation.id, bank_transaction_id=allocation.bank_transaction_id,
        run_id=allocation.run_id, amount=allocation.amount,
        account_id=allocation.account_id,
        account_name=account.name if account else None,
        tax_code=allocation.tax_code, decision_method=allocation.decision_method,
        confidence=allocation.confidence, status=allocation.status,
        reasoning=allocation.reasoning, question=allocation.question,
        matched_document_id=allocation.matched_document_id,
        matched_rule_id=allocation.matched_rule_id,
        approved_by=allocation.approved_by, approved_at=allocation.approved_at,
    )

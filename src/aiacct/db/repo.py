"""Persistence, through SQLAlchemy.

The method signatures are unchanged from the SQLite version, so the pipeline,
the review service, the exporters and the API did not have to change with it.
Only the bodies moved from hand-written SQL to Session calls, and the
hydration helpers that converted TEXT back into dates and decimals are gone
entirely - Postgres returns the right types.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models import (
    AllocationStatus,
    ClientProfile,
    CorrectionType,
    DecisionMethod,
    DocumentType,
    MatchType,
    RunStatus,
)
from .models import (
    Account,
    Allocation,
    BankTransaction,
    Base,
    Client,
    Correction,
    Document,
    MerchantRule,
    Run,
    User,
)
from .session import get_engine, get_sessionmaker

SUPPORTING_TYPES = (DocumentType.INVOICE, DocumentType.RECEIPT, DocumentType.PAYROLL)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- clients


class ClientRepo:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, client: Client) -> Client:
        self.session.add(client)
        self.session.commit()
        return client

    def get(self, client_id: int) -> Client | None:
        return self.session.get(Client, client_id)

    def get_by_uen(self, uen: str) -> Client | None:
        """Look a client up as master data rather than creating one.

        Scripts should find the client that was seeded, not invent a new one on
        every run.
        """
        return self.session.scalar(select(Client).where(Client.uen == uen))

    def list(self) -> list[Client]:
        return list(self.session.scalars(select(Client).order_by(Client.name)))

    def update_profile(self, client_id: int, profile: ClientProfile) -> None:
        client = self.session.get(Client, client_id)
        if client is None:
            return
        client.profile = profile
        self.session.commit()

    def add_learned_fact(self, client_id: int, fact: str) -> None:
        """Append a fact learned from a clarification the client answered.

        This is the third tier of memory: neither a rule nor an example, but
        something durably true about the client that the next run can use.
        """
        client = self.session.get(Client, client_id)
        if client is None or fact in client.profile.learned_facts:
            return
        profile = client.profile.model_copy(deep=True)
        profile.learned_facts.append(fact)
        client.profile = profile
        self.session.commit()


class UserRepo:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, user: User) -> User:
        self.session.add(user)
        self.session.commit()
        return user

    def get(self, user_id: int) -> User | None:
        return self.session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        return self.session.scalar(
            select(User).where(func.lower(User.email) == email.strip().lower())
        )

    def get_or_create(self, name: str, email: str) -> User:
        existing = self.get_by_email(email)
        if existing:
            return existing
        return self.create(User(name=name, email=email))

    def set_password(self, user_id: int, password_hash: str) -> None:
        user = self.session.get(User, user_id)
        if user is None:
            return
        user.password_hash = password_hash
        self.session.commit()

    def record_login(self, user_id: int) -> None:
        user = self.session.get(User, user_id)
        if user is None:
            return
        user.last_login_at = _now()
        self.session.commit()

    def list_all(self) -> list[User]:
        return list(self.session.scalars(select(User).order_by(User.name)))


class AccountRepo:
    """The chart of accounts. Seeded, then read; never inserted into by the AI."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_active(self) -> list[Account]:
        return list(
            self.session.scalars(
                select(Account).where(Account.is_active.is_(True)).order_by(Account.code)
            )
        )

    def get(self, code: str | None) -> Account | None:
        return self.session.get(Account, str(code).strip()) if code else None

    def upsert(self, account: Account) -> None:
        """Idempotent, so seeding can be re-run without duplicating."""
        self.session.execute(
            pg_insert(Account)
            .values(
                code=account.code,
                name=account.name,
                type=account.type,
                default_tax_code=account.default_tax_code,
                risk_level=account.risk_level,
                notes=account.notes,
                is_active=account.is_active,
            )
            .on_conflict_do_update(
                index_elements=[Account.code],
                set_={
                    "name": account.name,
                    "type": account.type,
                    "default_tax_code": account.default_tax_code,
                    "risk_level": account.risk_level,
                    "notes": account.notes,
                    "is_active": account.is_active,
                },
            )
        )


# ---------------------------------------------------------------- runs


class RunRepo:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, run: Run) -> Run:
        self.session.add(run)
        self.session.commit()
        return run

    def get(self, run_id: int) -> Run | None:
        return self.session.get(Run, run_id)

    def set_status(self, run_id: int, status: RunStatus, error: str | None = None) -> None:
        run = self.session.get(Run, run_id)
        if run is None:
            return
        run.status = status
        run.error = error
        if status in (RunStatus.COMPLETED, RunStatus.FAILED):
            run.completed_at = _now()
        self.session.commit()

    def set_thread_id(self, run_id: int, thread_id: str) -> None:
        """Record the LangGraph thread so a paused run survives a restart."""
        self.session.execute(
            update(Run).where(Run.id == run_id).values(langgraph_thread_id=thread_id)
        )
        self.session.commit()

    def add_usage(self, run_id: int, input_tokens: int, output_tokens: int) -> None:
        self.session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                llm_calls=Run.llm_calls + 1,
                input_tokens=Run.input_tokens + input_tokens,
                output_tokens=Run.output_tokens + output_tokens,
            )
        )
        self.session.commit()

    def complete(self, run_id: int, user_id: int | None) -> None:
        run = self.session.get(Run, run_id)
        if run is None:
            return
        run.status = RunStatus.COMPLETED
        run.completed_by = user_id
        run.completed_at = _now()
        self.session.commit()

    def list_for_client(self, client_id: int) -> list[Run]:
        return list(
            self.session.scalars(
                select(Run).where(Run.client_id == client_id).order_by(Run.id)
            )
        )


# ---------------------------------------------------------------- documents


class DocumentRepo:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, doc: Document, run_id: int | None = None) -> Document:
        doc.run_id = run_id
        self.session.add(doc)
        self.session.commit()
        return doc

    def get(self, doc_id: int) -> Document | None:
        return self.session.get(Document, doc_id)

    def find_by_hash(self, client_id: int, file_hash: str) -> Document | None:
        """The same bytes are never processed twice, however they were named."""
        return self.session.scalar(
            select(Document).where(
                Document.client_id == client_id, Document.file_hash == file_hash
            )
        )

    def set_type(self, doc_id: int, document_type: DocumentType) -> None:
        doc = self.session.get(Document, doc_id)
        if doc:
            doc.document_type = document_type
            self.session.commit()

    def update_statement_fields(self, doc: Document) -> None:
        self.session.add(doc)
        self.session.commit()

    # The ORM tracks changes, so these are the same operation. Both names are
    # kept because the callers read more clearly for saying which they mean.
    update_supporting_fields = update_statement_fields

    def set_field(self, doc_id: int, field: str, value: Any) -> None:
        """Apply one human correction to an extracted field."""
        allowed = {
            "account_number", "bank_name", "doc_number", "vendor_name",
            "period_start", "period_end", "opening_balance", "closing_balance",
            "total_amount", "tax_amount", "summary", "document_type",
        }
        if field not in allowed:
            raise ValueError(f"field not correctable: {field}")
        doc = self.session.get(Document, doc_id)
        if doc:
            setattr(doc, field, value)
            self.session.commit()

    def clear_legibility(self, doc_id: int, field: str) -> None:
        """Drop a field from the legibility map once a person supplied it.

        The value is now known, so it is no longer a reason to stop the run.
        """
        doc = self.session.get(Document, doc_id)
        if doc is None or field not in doc.field_legibility:
            return
        doc.field_legibility = {
            k: v for k, v in doc.field_legibility.items() if k != field
        }
        self.session.commit()

    def set_reconciles(self, doc_id: int, value: bool | None) -> None:
        doc = self.session.get(Document, doc_id)
        if doc:
            doc.reconciles = value
            self.session.commit()

    def list_supporting_for_run(self, run_id: int) -> list[Document]:
        """Supporting documents submitted with this run, and only those.

        Scoped to the run rather than the client, because a run has to be
        reproducible from the files it was handed. Matching against everything
        a client ever uploaded lets a statement be resolved by evidence nobody
        submitted with it, and lets one invoice be claimed by several bank
        lines in different periods - which would make a double payment look
        fully documented when both halves point at the same piece of paper.

        An invoice that arrived with an earlier batch and belongs to this one
        is re-submitted; ``attach_to_run`` recognises it by hash rather than
        storing it twice.
        """
        return list(
            self.session.scalars(
                select(Document).where(
                    Document.run_id == run_id,
                    Document.document_type.in_(SUPPORTING_TYPES),
                )
            )
        )

    def list_for_run(self, run_id: int) -> list[Document]:
        return list(
            self.session.scalars(select(Document).where(Document.run_id == run_id))
        )

    def attach_to_run(self, doc_id: int, run_id: int) -> None:
        doc = self.session.get(Document, doc_id)
        if doc:
            doc.run_id = run_id
            self.session.commit()


# ---------------------------------------------------------------- transactions


class BankTransactionRepo:
    def __init__(self, session: Session) -> None:
        self.session = session

    def bulk_create(self, txns: Iterable[BankTransaction]) -> list[BankTransaction]:
        rows = list(txns)
        self.session.add_all(rows)
        self.session.commit()
        return rows

    def delete_for_document(self, document_id: int) -> None:
        """Used when a failed extraction is retried."""
        self.session.execute(
            delete(BankTransaction).where(BankTransaction.document_id == document_id)
        )
        self.session.commit()

    def get(self, txn_id: int) -> BankTransaction | None:
        return self.session.get(BankTransaction, txn_id)

    def list_for_document(self, document_id: int) -> list[BankTransaction]:
        return list(
            self.session.scalars(
                select(BankTransaction)
                .where(BankTransaction.document_id == document_id)
                .order_by(BankTransaction.line_no)
            )
        )

    def list_for_run(self, run_id: int) -> list[BankTransaction]:
        return list(
            self.session.scalars(
                select(BankTransaction)
                .join(Document, Document.id == BankTransaction.document_id)
                .where(Document.run_id == run_id)
                .order_by(BankTransaction.txn_date, BankTransaction.line_no)
            )
        )

    def list_for_client_history(
        self, client_id: int, limit: int = 500
    ) -> list[BankTransaction]:
        """Everything seen for this client, newest first.

        Used to show an accountant which past transactions a proposed rule
        would have captured, before they confirm it.
        """
        return list(
            self.session.scalars(
                select(BankTransaction)
                .where(BankTransaction.client_id == client_id)
                .order_by(BankTransaction.txn_date.desc())
                .limit(limit)
            )
        )

    def set_field(self, txn_id: int, field: str, value: Any) -> None:
        allowed = {
            "txn_date", "raw_description", "bank_reference",
            "money_in", "money_out", "balance_after",
        }
        if field not in allowed:
            raise ValueError(f"field not correctable: {field}")
        txn = self.session.get(BankTransaction, txn_id)
        if txn:
            setattr(txn, field, value)
            self.session.commit()

    def clear_legibility(self, txn_id: int, field: str) -> None:
        txn = self.session.get(BankTransaction, txn_id)
        if txn is None or field not in txn.field_legibility:
            return
        txn.field_legibility = {
            k: v for k, v in txn.field_legibility.items() if k != field
        }
        self.session.commit()


# ---------------------------------------------------------------- allocations


class AllocationRepo:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, alloc: Allocation) -> Allocation:
        self.session.add(alloc)
        self.session.commit()
        return alloc

    def get(self, alloc_id: int) -> Allocation | None:
        return self.session.get(Allocation, alloc_id)

    def list_for_run(
        self, run_id: int, status: AllocationStatus | None = None
    ) -> list[Allocation]:
        stmt = select(Allocation).where(Allocation.run_id == run_id)
        if status:
            stmt = stmt.where(Allocation.status == status)
        return list(self.session.scalars(stmt.order_by(Allocation.id)))

    def list_for_transaction(self, txn_id: int) -> list[Allocation]:
        return list(
            self.session.scalars(
                select(Allocation)
                .where(Allocation.bank_transaction_id == txn_id)
                .order_by(Allocation.id)
            )
        )

    def apply_override(
        self,
        alloc_id: int,
        account_id: str,
        tax_code: str | None,
        user_id: int,
        note: str | None = None,
    ) -> None:
        alloc = self.session.get(Allocation, alloc_id)
        if alloc is None:
            return
        alloc.account_id = account_id
        alloc.tax_code = tax_code
        alloc.decision_method = DecisionMethod.HUMAN
        # A person's answer is not a probability.
        alloc.confidence = None
        alloc.status = AllocationStatus.APPROVED
        alloc.approved_by = user_id
        alloc.approved_at = _now()
        if note:
            alloc.reasoning = note
        self.session.commit()

    def approve(self, alloc_id: int, user_id: int) -> None:
        """Accept the proposed coding unchanged.

        No Correction is written - nothing changed - but the approval is itself
        evidence, and without it "reviewed and correct" is indistinguishable
        from "nobody has looked yet".
        """
        alloc = self.session.get(Allocation, alloc_id)
        if alloc is None:
            return
        alloc.status = AllocationStatus.APPROVED
        alloc.approved_by = user_id
        alloc.approved_at = _now()
        self.session.commit()

    def set_status(self, alloc_id: int, status: AllocationStatus) -> None:
        alloc = self.session.get(Allocation, alloc_id)
        if alloc:
            alloc.status = status
            self.session.commit()

    def replace_for_transaction(
        self, txn_id: int, allocations: list[Allocation]
    ) -> list[Allocation]:
        """Swap one allocation for a split, keeping the sum constraint intact.

        Existing rows are updated rather than deleted and recreated, so the
        corrections referencing them survive: the record of who changed what
        has to outlive the row it describes.
        """
        existing = self.list_for_transaction(txn_id)
        created: list[Allocation] = []

        for index, replacement in enumerate(allocations):
            if index < len(existing):
                target = existing[index]
                target.amount = replacement.amount
                target.account_id = replacement.account_id
                target.tax_code = replacement.tax_code
                target.decision_method = replacement.decision_method
                target.confidence = replacement.confidence
                target.status = replacement.status
                target.reasoning = replacement.reasoning
                target.question = None
                target.matched_document_id = replacement.matched_document_id
                target.matched_rule_id = None
                target.approved_by = replacement.approved_by
                created.append(target)
            else:
                self.session.add(replacement)
                created.append(replacement)

        # Surplus rows from a previous, wider split. Only ever machine
        # generated, so nothing references them.
        for surplus in existing[len(allocations):]:
            self.session.execute(
                update(Correction)
                .where(Correction.allocation_id == surplus.id)
                .values(allocation_id=None)
            )
            self.session.delete(surplus)

        self.session.commit()
        return created

    def status_counts(self, run_id: int) -> dict[str, int]:
        rows = self.session.execute(
            select(Allocation.status, func.count())
            .where(Allocation.run_id == run_id)
            .group_by(Allocation.status)
        ).all()
        return {str(status): count for status, count in rows}

    def decision_method_counts(self, run_id: int) -> dict[str, int]:
        rows = self.session.execute(
            select(Allocation.decision_method, func.count())
            .where(Allocation.run_id == run_id)
            .group_by(Allocation.decision_method)
        ).all()
        return {str(method): count for method, count in rows}

    def allocated_total(self, txn_id: int) -> Decimal:
        """Sum of a transaction's allocations.

        Postgres does this exactly, which is why money is Numeric and not the
        TEXT that SQLite forced.
        """
        total = self.session.scalar(
            select(func.coalesce(func.sum(Allocation.amount), 0)).where(
                Allocation.bank_transaction_id == txn_id
            )
        )
        return Decimal(total)


# ---------------------------------------------------------------- corrections


class CorrectionRepo:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, correction: Correction, document_id: int | None = None) -> Correction:
        if document_id is not None:
            correction.document_id = document_id
        self.session.add(correction)
        self.session.commit()
        return correction

    def recent_for_client(self, client_id: int, limit: int = 200) -> list[dict[str, Any]]:
        """Past categorisation corrections, as few-shot material.

        Returns the description that was corrected alongside the account the
        human chose - the pair the categorisation prompt needs.
        """
        rows = self.session.execute(
            select(
                Correction.to_account_id,
                Correction.to_tax_code,
                BankTransaction.raw_description,
                BankTransaction.money_out,
                BankTransaction.money_in,
            )
            .join(Allocation, Allocation.id == Correction.allocation_id)
            .join(BankTransaction, BankTransaction.id == Allocation.bank_transaction_id)
            .where(
                BankTransaction.client_id == client_id,
                Correction.correction_type == CorrectionType.CATEGORISATION,
                Correction.to_account_id.is_not(None),
            )
            .order_by(Correction.id.desc())
            .limit(limit)
        ).all()
        return [
            {
                "to_account_id": r[0],
                "to_tax_code": r[1],
                "raw_description": r[2],
                "money_out": r[3],
                "money_in": r[4],
            }
            for r in rows
        ]

    def count_for_run(self, run_id: int) -> int:
        return self.session.scalar(
            select(func.count())
            .select_from(Correction)
            .join(Allocation, Allocation.id == Correction.allocation_id)
            .where(Allocation.run_id == run_id)
        )


# ---------------------------------------------------------------- memory


class MerchantRuleRepo:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, rule: MerchantRule) -> MerchantRule:
        """Insert, or confirm an existing rule for the same pattern.

        Teaching the same thing twice is a confirmation, not a duplicate.
        """
        # A bare INSERT ... ON CONFLICT reads the attributes directly, so the
        # ORM-level `default=` on these columns never fires. Apply them here.
        result = self.session.execute(
            pg_insert(MerchantRule)
            .values(
                client_id=rule.client_id,
                match_pattern=rule.match_pattern,
                match_type=rule.match_type or MatchType.CONTAINS,
                account_id=rule.account_id,
                tax_code=rule.tax_code,
                confirm_count=rule.confirm_count if rule.confirm_count is not None else 1,
                created_from_correction_id=rule.created_from_correction_id,
                is_active=True,
                is_stale=False,
            )
            .on_conflict_do_update(
                index_elements=[MerchantRule.client_id, MerchantRule.match_pattern],
                set_={
                    "account_id": rule.account_id,
                    "tax_code": rule.tax_code,
                    "confirm_count": MerchantRule.confirm_count + 1,
                    "is_active": True,
                    "is_stale": False,
                },
            )
            .returning(MerchantRule.id)
        )
        rule_id = result.scalar_one()
        self.session.commit()
        return self.session.get(MerchantRule, rule_id)

    def list_active(self, client_id: int) -> list[MerchantRule]:
        # Longest pattern first, so "GRAB *TRIP" wins over a broader "GRAB".
        return list(
            self.session.scalars(
                select(MerchantRule)
                .where(
                    MerchantRule.client_id == client_id,
                    MerchantRule.is_active.is_(True),
                )
                .order_by(func.length(MerchantRule.match_pattern).desc())
            )
        )

    def list_all(self, client_id: int) -> list[MerchantRule]:
        return list(
            self.session.scalars(
                select(MerchantRule)
                .where(MerchantRule.client_id == client_id)
                .order_by(func.length(MerchantRule.match_pattern).desc())
            )
        )

    def get(self, rule_id: int) -> MerchantRule | None:
        return self.session.get(MerchantRule, rule_id)

    def confirm(self, rule_id: int) -> None:
        """An approval of a rule-driven allocation is evidence the rule is right.

        Set through the ORM rather than a bulk UPDATE, so anything already
        loaded in this session sees the new value.
        """
        rule = self.session.get(MerchantRule, rule_id)
        if rule is None:
            return
        rule.confirm_count += 1
        rule.last_applied_at = _now()
        self.session.commit()

    def mark_stale(self, rule_id: int) -> None:
        """A correction landed on an allocation this rule produced.

        Not deleted - the accountant decides - but flagged, because a rule that
        has started being corrected has stopped being right.
        """
        rule = self.session.get(MerchantRule, rule_id)
        if rule is None:
            return
        rule.is_stale = True
        self.session.commit()

    def deactivate(self, rule_id: int) -> None:
        rule = self.session.get(MerchantRule, rule_id)
        if rule is None:
            return
        rule.is_active = False
        self.session.commit()

    def touch(self, rule_id: int) -> None:
        rule = self.session.get(MerchantRule, rule_id)
        if rule is None:
            return
        rule.last_applied_at = _now()
        self.session.commit()


# ---------------------------------------------------------------- facade


class Repositories:
    """One handle for every repository, sharing a session."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.clients = ClientRepo(session)
        self.users = UserRepo(session)
        self.accounts = AccountRepo(session)
        self.runs = RunRepo(session)
        self.documents = DocumentRepo(session)
        self.transactions = BankTransactionRepo(session)
        self.allocations = AllocationRepo(session)
        self.corrections = CorrectionRepo(session)
        self.rules = MerchantRuleRepo(session)

    @classmethod
    def open(cls, url: str | None = None, session: Session | None = None) -> "Repositories":
        return cls(session or get_sessionmaker(url=url)())

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "Repositories":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def create_all(url: str | None = None) -> None:
    """Create every table directly from the models.

    Used by the test fixtures. Real environments go through Alembic, so that
    schema changes are versioned rather than applied by whatever happens to
    start first.
    """
    Base.metadata.create_all(get_engine(url=url))


def drop_all(url: str | None = None) -> None:
    Base.metadata.drop_all(get_engine(url=url))

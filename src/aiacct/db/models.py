"""SQLAlchemy models. The single definition of what is stored.

These replace both the hand-written SQL and the parallel Pydantic domain
classes that used to shadow it. Pydantic keeps the three jobs it is good at:
``ClientProfile`` as a JSONB payload, the strict LLM output schemas, and the
API request and response bodies.

Postgres removes two compromises SQLite forced:

  * Money is ``Numeric(15, 2)``. The old TEXT columns existed only because
    SQLite has no exact decimal type, which meant every SUM had to happen in
    Python. An accounting system that loses cents is worthless, and floats
    cannot represent 0.10.
  * ``field_legibility`` and ``profile`` are JSONB rather than TEXT holding
    JSON.

Enums are ``native_enum=False`` - VARCHAR plus a CHECK constraint - because
adding a value to a native Postgres enum later needs an ALTER TYPE migration
and buys nothing here.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Float,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from ..models import (
    AccountType,
    AllocationStatus,
    ClientProfile,
    CorrectionType,
    DecisionMethod,
    DocumentType,
    Legibility,
    MatchType,
    RiskLevel,
    RunStatus,
)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- types


def money(**kwargs) -> Mapped[Decimal]:
    """Exact to the cent. Never Float, in any column, anywhere."""
    return mapped_column(Numeric(15, 2), **kwargs)


def enum_column(python_enum, **kwargs):
    return mapped_column(
        Enum(python_enum, native_enum=False, length=32, validate_strings=True), **kwargs
    )


def timestamp(**kwargs) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kwargs)


class PydanticJSON(TypeDecorator):
    """Stores a Pydantic model as JSONB and returns it typed."""

    impl = JSONB
    cache_ok = True

    def __init__(self, model, **kwargs):
        self.model = model
        super().__init__(**kwargs)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, self.model):
            return value.model_dump(mode="json")
        return value

    def process_result_value(self, value, dialect):
        return self.model.model_validate(value or {})


class LegibilityMap(TypeDecorator):
    """``{"account_number": "ambiguous"}`` - only fields that were NOT clear.

    An entry here for an identifier is what stops a run at the extraction gate;
    an entry for a description only costs confidence.
    """

    impl = JSONB
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return {k: str(v) for k, v in (value or {}).items()}

    def process_result_value(self, value, dialect):
        return {k: Legibility(v) for k, v in (value or {}).items()}


# ---------------------------------------------------------------- reference


class Client(Base):
    """The business whose books are being kept.

    Master data, seeded rather than created by a script - and never created by
    the system from a document. Posting one company's statement into another's
    books is unrecoverable, so the accountant picks the client at upload and the
    extracted account-holder name is only used to check that choice.
    """

    __tablename__ = "client"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    uen: Mapped[str | None] = mapped_column(String(20), unique=True)
    profile: Mapped[ClientProfile] = mapped_column(
        PydanticJSON(ClientProfile), default=ClientProfile
    )
    created_at: Mapped[datetime] = timestamp(server_default=func.now())
    updated_at: Mapped[datetime] = timestamp(server_default=func.now(), onupdate=func.now())


class User(Base):
    """A member of the firm's staff, not a client.

    Accounts are seeded, not self-registered: a firm decides who works on its
    clients' books. There is deliberately no sign-up endpoint.

    The row is also what ``allocation.approved_by`` and
    ``correction.corrected_by`` point at, so who signed off on a set of books is
    a real question the database can answer.
    """

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    # Argon2id. Nullable so a seeded row can exist before a password is set;
    # a user without one simply cannot log in.
    password_hash: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = timestamp()
    created_at: Mapped[datetime] = timestamp(server_default=func.now())


class Account(Base):
    """The chart of accounts.

    Seeded once from YAML and changed deliberately. The system selects from
    this list and never inserts into it: allowing it to would fragment the
    chart into Telephone, Phone and Telco within a month, and every report
    would be wrong.
    """

    __tablename__ = "account"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    type: Mapped[AccountType] = enum_column(AccountType)
    default_tax_code: Mapped[str] = mapped_column(String(10))
    # HIGH forces human review regardless of confidence, because confidence and
    # consequence are independent: a model can be sure a large transfer is
    # drawings and be wrong, and drawings change the tax computation.
    risk_level: Mapped[RiskLevel] = enum_column(RiskLevel, default=RiskLevel.LOW)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def normal_balance(self) -> str:
        """Derived from type rather than stored, so the two cannot drift apart."""
        if self.type in (AccountType.ASSET, AccountType.EXPENSE):
            return "DEBIT"
        return "CREDIT"


# ---------------------------------------------------------------- runs


class Run(Base):
    """One processing job, from upload to sign-off.

    Long-lived by nature: it sits at AWAITING_REVIEW until an accountant has
    finished, which can be days while they wait on the client.
    """

    __tablename__ = "run"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"), index=True)
    started_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    # Sign-off is at batch level: the reviewer examines the exceptions and takes
    # responsibility for the whole run rather than initialling every line.
    completed_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    status: Mapped[RunStatus] = enum_column(RunStatus, default=RunStatus.RUNNING)
    langgraph_thread_id: Mapped[str | None] = mapped_column(String(64))
    model_used: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = timestamp(server_default=func.now())
    completed_at: Mapped[datetime | None] = timestamp()


# ---------------------------------------------------------------- documents


class Document(Base):
    """One uploaded file, whatever kind.

    Statement-only and invoice-only columns are nullable on one table rather
    than split out: a Document is a file, and a second table holding period and
    balance fields did not earn its keep.
    """

    __tablename__ = "document"
    __table_args__ = (
        # The same bytes are never processed twice, however they are named.
        UniqueConstraint("client_id", "file_hash", name="uq_document_hash"),
        Index("ix_document_client_type", "client_id", "document_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("run.id"), index=True)
    document_type: Mapped[DocumentType] = enum_column(
        DocumentType, default=DocumentType.UNKNOWN
    )
    original_filename: Mapped[str] = mapped_column(String(400))
    storage_uri: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(120))
    file_hash: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int | None] = mapped_column(Integer)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    field_legibility: Mapped[dict[str, Legibility]] = mapped_column(
        LegibilityMap, default=dict
    )

    # -- bank statements --
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    opening_balance: Mapped[Decimal | None] = money()
    closing_balance: Mapped[Decimal | None] = money()
    bank_name: Mapped[str | None] = mapped_column(String(120))
    account_number: Mapped[str | None] = mapped_column(String(64))
    account_holder: Mapped[str | None] = mapped_column(String(200))
    # Three states on purpose. NULL means the check could not run because no
    # balances were printed - which must never be recorded as "checked and fine".
    reconciles: Mapped[bool | None] = mapped_column(Boolean)

    # -- invoices, receipts, payroll --
    vendor_name: Mapped[str | None] = mapped_column(String(200))
    doc_number: Mapped[str | None] = mapped_column(String(64))
    doc_date: Mapped[date | None] = mapped_column(Date)
    total_amount: Mapped[Decimal | None] = money()
    tax_amount: Mapped[Decimal | None] = money()
    # What was actually bought. The bank description says who was paid; only
    # this says what for, which separates an office expense from a fixed asset.
    summary: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = timestamp(server_default=func.now())

    transactions: Mapped[list["BankTransaction"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    @property
    def is_statement(self) -> bool:
        return self.document_type == DocumentType.BANK_STATEMENT

    @property
    def is_supporting(self) -> bool:
        return self.document_type in (
            DocumentType.INVOICE,
            DocumentType.RECEIPT,
            DocumentType.PAYROLL,
        )


class BankTransaction(Base):
    """One line on a statement. A fact, not an interpretation."""

    __tablename__ = "bank_transaction"
    __table_args__ = (
        Index("ix_txn_document_line", "document_id", "line_no"),
        Index("ix_txn_client_date", "client_id", "txn_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE")
    )
    # Denormalised: every phase 2 query filters on it, and reaching it through
    # document would be a join on the hottest path.
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    txn_date: Mapped[date] = mapped_column(Date)
    # Exactly as printed. Never modified - this is the audit record.
    raw_description: Mapped[str] = mapped_column(Text)
    bank_reference: Mapped[str | None] = mapped_column(String(64))

    # Two columns, never one signed amount and never a direction flag. A
    # statement is written from the bank's point of view, the mirror of the
    # client's, and inferring a sign from it is how people lose hours.
    money_in: Mapped[Decimal | None] = money()
    money_out: Mapped[Decimal | None] = money()
    # The running balance printed on the line. This is what turns "something in
    # these 45 rows is wrong" into "row 23 is wrong".
    balance_after: Mapped[Decimal | None] = money()

    page: Mapped[int | None] = mapped_column(Integer)
    field_legibility: Mapped[dict[str, Legibility]] = mapped_column(
        LegibilityMap, default=dict
    )
    created_at: Mapped[datetime] = timestamp(server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="transactions")
    allocations: Mapped[list["Allocation"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )

    @property
    def amount(self) -> Decimal:
        """Magnitude of the movement, direction-free."""
        if self.money_in is not None:
            return self.money_in
        return self.money_out if self.money_out is not None else Decimal("0")

    @property
    def is_inflow(self) -> bool:
        return self.money_in is not None


# ---------------------------------------------------------------- allocations


class Allocation(Base):
    """What one slice of a bank line was for.

    The statement hands over one side of the entry for free; this is the other
    side. Created for every transaction, whether or not a supporting document
    exists - most have none, and every dollar through the bank still has to be
    explained or the books do not balance.
    """

    __tablename__ = "allocation"
    __table_args__ = (
        # A person's answer is not a probability. Storing 1.0 would flatter
        # every calibration band these rows land in.
        CheckConstraint(
            "decision_method <> 'HUMAN' OR confidence IS NULL",
            name="ck_human_has_no_confidence",
        ),
        # "We do not know" is not something anyone can sign off.
        CheckConstraint(
            "status <> 'APPROVED' OR account_id IS NOT NULL",
            name="ck_approved_needs_account",
        ),
        Index("ix_alloc_run_status", "run_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_transaction_id: Mapped[int] = mapped_column(
        ForeignKey("bank_transaction.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id"))
    amount: Mapped[Decimal] = money()

    # NULL means genuinely unresolved. Deliberately not Suspense: "we do not
    # know" and "deliberately coded to suspense" need different handling.
    account_id: Mapped[str | None] = mapped_column(ForeignKey("account.code"))
    tax_code: Mapped[str | None] = mapped_column(String(10))

    # How account_id was determined - not where the data came from. This is the
    # only way to show RULE 5% / LLM 85% becoming RULE 50% / LLM 40%, and
    # without it a learned answer and a fresh guess look identical.
    decision_method: Mapped[DecisionMethod] = enum_column(DecisionMethod)
    # Float, not Numeric: a score is not money. There is nothing to round
    # exactly, and Numeric would leak Decimal into every comparison in the
    # scorer and the evaluation harness.
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[AllocationStatus] = enum_column(AllocationStatus)
    # Shown at review. Without it a person cannot review non-blindly, which is
    # the whole point of the step.
    reasoning: Mapped[str | None] = mapped_column(Text)
    question: Mapped[str | None] = mapped_column(Text)

    matched_document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id"))
    matched_rule_id: Mapped[int | None] = mapped_column(ForeignKey("merchant_rule.id"))

    # An approval means a human looked and agreed. Without it, "reviewed and
    # correct" and "nobody has looked yet" are indistinguishable.
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    approved_at: Mapped[datetime | None] = timestamp()
    created_at: Mapped[datetime] = timestamp(server_default=func.now())
    updated_at: Mapped[datetime] = timestamp(server_default=func.now(), onupdate=func.now())

    transaction: Mapped[BankTransaction] = relationship(back_populates="allocations")
    account: Mapped[Account | None] = relationship()


# ---------------------------------------------------------------- human loop


class Correction(Base):
    """A human changing an answer. Exactly one target is set.

    Extraction corrections feed the audit trail and an extraction-quality
    metric; there is nothing to learn from them, because "read this smudge as a
    9" does not generalise. Categorisation corrections feed the audit trail and
    the learning loop.
    """

    __tablename__ = "correction"

    id: Mapped[int] = mapped_column(primary_key=True)
    allocation_id: Mapped[int | None] = mapped_column(
        ForeignKey("allocation.id", ondelete="SET NULL"), index=True
    )
    bank_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_transaction.id", ondelete="SET NULL")
    )
    document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id"))
    corrected_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    correction_type: Mapped[CorrectionType] = enum_column(CorrectionType)

    field_name: Mapped[str | None] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)

    # Typed columns for the categorisation case, which learning and metrics
    # join on constantly. The generic old/new pair covers everything else.
    from_account_id: Mapped[str | None] = mapped_column(String(10))
    to_account_id: Mapped[str | None] = mapped_column(String(10))
    from_tax_code: Mapped[str | None] = mapped_column(String(10))
    to_tax_code: Mapped[str | None] = mapped_column(String(10))
    # Kept so correction rate can be measured per confidence band, which is the
    # only way to know whether the score means anything at all.
    from_confidence: Mapped[float | None] = mapped_column(Float)

    note: Mapped[str | None] = mapped_column(Text)
    create_rule: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = timestamp(server_default=func.now())


class MerchantRule(Base):
    """What the accountant has taught the system about this client.

    ``match_pattern`` is proposed by the model once, at correction time, and
    then applied deterministically - so there is no normaliser to maintain and
    the same description always produces the same answer.

    Scoped by client, always. GRAB means travel for a design agency and a
    delivery cost for a restaurant; sharing a rule between them would be
    actively wrong rather than merely untidy.
    """

    __tablename__ = "merchant_rule"
    __table_args__ = (
        UniqueConstraint("client_id", "match_pattern", name="uq_rule_client_pattern"),
        Index("ix_rule_client_active", "client_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"))
    match_pattern: Mapped[str] = mapped_column(String(200))
    match_type: Mapped[MatchType] = enum_column(MatchType, default=MatchType.CONTAINS)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.code"))
    tax_code: Mapped[str | None] = mapped_column(String(10))
    # Every approval of a result this rule produced. A rule confirmed eight
    # times is trustworthy in a way a day-old one is not.
    confirm_count: Mapped[int] = mapped_column(Integer, default=1)
    # allocation -> merchant_rule -> correction -> allocation is a cycle, so
    # one edge has to be added after the tables exist. This is the weakest of
    # the three: pure provenance, nullable, and cleared rather than cascading.
    created_from_correction_id: Mapped[int | None] = mapped_column(
        ForeignKey("correction.id", ondelete="SET NULL", use_alter=True,
                   name="fk_rule_from_correction")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # A rule that starts producing corrections has stopped being right. It is
    # flagged rather than silently overwritten - the accountant decides whether
    # the client's habits changed.
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False)
    last_applied_at: Mapped[datetime | None] = timestamp()
    created_at: Mapped[datetime] = timestamp(server_default=func.now())


__all__ = [
    "Account",
    "Allocation",
    "BankTransaction",
    "Base",
    "Client",
    "Correction",
    "Document",
    "MerchantRule",
    "Run",
    "User",
]

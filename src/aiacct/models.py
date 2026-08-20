"""Enumerations and the strict schemas the model fills in.

What is stored lives in ``db/models.py`` as SQLAlchemy models. This module
holds the three things that are not rows in a table:

  * the enumerations, shared by the ORM, the API and the pipeline
  * ``ClientProfile``, which is a JSONB payload rather than columns
  * the LLM output schemas - the exact shape each call must return

OpenAI strict structured outputs require every field to be required and every
object to forbid extra properties, so the LLM schemas below carry no defaults
and use explicit ``| None`` for anything genuinely optional.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class DocumentType(StrEnum):
    UNKNOWN = "UNKNOWN"
    BANK_STATEMENT = "BANK_STATEMENT"
    INVOICE = "INVOICE"
    RECEIPT = "RECEIPT"
    PAYROLL = "PAYROLL"
    OTHER = "OTHER"


class AccountType(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"


class RiskLevel(StrEnum):
    LOW = "LOW"
    HIGH = "HIGH"


class Legibility(StrEnum):
    """How reliably a field was read off the page.

    An observation, not a probability. Models report what they could see far
    more honestly than they report how confident they feel.
    """

    CLEAR = "clear"
    INFERRED = "inferred"
    AMBIGUOUS = "ambiguous"
    UNREADABLE = "unreadable"


class FieldClass(StrEnum):
    """How much redundancy a field carries, which decides what to do when it
    is unclear. See ``extraction.field_policy``."""

    REDUNDANT = "REDUNDANT"
    VERIFIABLE = "VERIFIABLE"
    IDENTIFIER = "IDENTIFIER"


class DecisionMethod(StrEnum):
    """Who determined ``Allocation.account_id`` - not where the data came from."""

    RULE = "RULE"
    LLM = "LLM"
    HUMAN = "HUMAN"


class AllocationStatus(StrEnum):
    AUTO_POSTED = "AUTO_POSTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CLIENT_QUERY = "CLIENT_QUERY"
    APPROVED = "APPROVED"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    AWAITING_EXTRACTION_REVIEW = "AWAITING_EXTRACTION_REVIEW"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CorrectionType(StrEnum):
    EXTRACTION = "EXTRACTION"
    CATEGORISATION = "CATEGORISATION"


class MatchType(StrEnum):
    CONTAINS = "CONTAINS"
    PREFIX = "PREFIX"


class Verdict(StrEnum):
    """Outcome of the deterministic extraction checks.

    UNVERIFIABLE is distinct from PASS on purpose: some exports print no
    balances, and "could not check" must never be recorded as "checked and fine".
    """

    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIABLE = "UNVERIFIABLE"


# --------------------------------------------------------------------------
# Reference data
#
# The chart of accounts is a table (db/models.py). Tax codes stay here because
# they are fixed by legislation, identical for every client, and nothing
# foreign-keys to them.
# --------------------------------------------------------------------------


class TaxCode(BaseModel):
    code: str
    name: str
    rate: Decimal
    claimable: bool
    applies_to: str
    description: str | None = None


class ClientProfile(BaseModel):
    """Everything the categorisation prompt needs to know about the business.

    A JSON column rather than scalar columns: nothing queries these fields, they
    are read whole to build a prompt, and ``learned_facts`` grows without a
    migration every time a client answers a clarification.
    """

    business_description: str = ""
    gst_registered: bool = False
    own_bank_accounts: list[str] = Field(default_factory=list)
    capitalisation_threshold: Decimal = Decimal("1000.00")
    materiality_threshold: Decimal = Decimal("5000.00")
    learned_facts: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# LLM output schemas - strict mode, so no defaults and explicit nullability
# --------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldLegibilityItem(_Strict):
    """One field the model could not read cleanly.

    A list rather than a dict because strict JSON Schema cannot express an
    object with arbitrary keys. Fields absent from the list were clear.
    """

    field: str
    legibility: Legibility


class ClassificationResult(_Strict):
    document_type: DocumentType
    reasoning: str


class ExtractedTransaction(_Strict):
    line_no: int
    txn_date: str
    raw_description: str
    bank_reference: str | None
    money_in: float | None
    money_out: float | None
    balance_after: float | None
    page: int
    uncertain_fields: list[FieldLegibilityItem]


class StatementExtraction(_Strict):
    bank_name: str | None
    account_number: str | None
    account_holder: str | None
    period_start: str | None
    period_end: str | None
    opening_balance: float | None
    closing_balance: float | None
    stated_transaction_count: int | None
    transactions: list[ExtractedTransaction]
    uncertain_fields: list[FieldLegibilityItem]


class SupportingDocExtraction(_Strict):
    vendor_name: str | None
    doc_number: str | None
    doc_date: str | None
    total_amount: float | None
    tax_amount: float | None
    # What was bought. The bank description says who was paid; only this says
    # what for, which is what separates an expense from a capitalised asset.
    summary: str
    uncertain_fields: list[FieldLegibilityItem]


class ColumnMapping(_Strict):
    """Which column holds what, for a bank export we do not recognise.

    Zero-based positions. Asked for only when the alias table fails, because
    no fixed list of header names can cover every bank's export.
    """

    date_column: int | None
    description_column: int | None
    reference_column: int | None
    debit_column: int | None
    credit_column: int | None
    balance_column: int | None
    notes: str


class AccountCandidate(_Strict):
    account_code: str
    score: float


class TransactionCategorisation(_Strict):
    """One transaction's proposed coding.

    Note the absence of a self-reported confidence. The gap between the top two
    candidates is a far more honest uncertainty signal, and the final score is
    computed by code from signals this call never sees.
    """

    transaction_id: int
    account_code: str | None
    tax_code: str | None
    # Ranked, best first, at least one entry when account_code is set.
    alternatives: list[AccountCandidate]
    reasoning: str
    # Whether the description carries enough to reason from at all - a merchant
    # name, or a recognisable kind of transaction. "SERVICE CHARGE" names nobody
    # but is identifiable; "TRF 8891234" is not. An observation, so that code
    # rather than a maintained vocabulary list decides what to do about it.
    identifiable: bool
    # Written only when the model cannot resolve it; phrased for the client.
    clarification_question: str | None
    # True when one payment covers genuinely different things, e.g. a loan
    # repayment splitting into principal and interest.
    needs_split: bool
    split_note: str | None


class CategorisationBatch(_Strict):
    results: list[TransactionCategorisation]


class MatchPatternProposal(_Strict):
    """A rule pattern, proposed once at correction time.

    Asking the model here rather than hand-writing a normaliser is what keeps
    "GRAB *TRIP" from swallowing "GRABFOOD".
    """

    match_pattern: str
    reasoning: str

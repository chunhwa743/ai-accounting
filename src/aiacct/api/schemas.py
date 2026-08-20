"""Request and response bodies.

Kept separate from the domain models so the wire format can stay stable while
the internals move, and so every field the frontend sees is deliberate.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from ..models import AllocationStatus, DecisionMethod, DocumentType, RunStatus


# ---------------------------------------------------------------- errors


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# ---------------------------------------------------------------- clients


class ClientProfileIn(BaseModel):
    business_description: str = ""
    gst_registered: bool = False
    own_bank_accounts: list[str] = Field(default_factory=list)
    capitalisation_threshold: Decimal = Decimal("1000.00")
    materiality_threshold: Decimal = Decimal("5000.00")


class ClientCreate(BaseModel):
    name: str
    uen: str | None = None
    profile: ClientProfileIn = Field(default_factory=ClientProfileIn)


class ClientOut(BaseModel):
    id: int
    name: str
    uen: str | None
    profile: dict
    created_at: datetime | None


# ---------------------------------------------------------------- documents


class DocumentOut(BaseModel):
    id: int
    client_id: int
    document_type: DocumentType
    original_filename: str
    mime_type: str
    page_count: int | None
    # Only fields that were not read cleanly appear. An entry here for an
    # identifier is what stops a run at the extraction gate.
    field_legibility: dict[str, str]

    period_start: date | None = None
    period_end: date | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    bank_name: str | None = None
    account_number: str | None = None
    # true = arithmetic verified, false = it did not, null = no balances were
    # printed so the check could not run. Null is not a pass.
    reconciles: bool | None = None

    vendor_name: str | None = None
    doc_number: str | None = None
    doc_date: date | None = None
    total_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    summary: str | None = None


# ---------------------------------------------------------------- runs


class RunCreate(BaseModel):
    document_ids: list[int] | None = None
    file_paths: list[str] | None = None


class RunOut(BaseModel):
    id: int
    client_id: int
    status: RunStatus
    model_used: str | None
    llm_calls: int
    input_tokens: int
    output_tokens: int
    started_at: datetime | None
    completed_at: datetime | None
    by_status: dict[str, int] = Field(default_factory=dict)
    by_decision_method: dict[str, int] = Field(default_factory=dict)
    auto_post_rate: float | None = None
    needs_attention: int = 0


class ExtractionIssueOut(BaseModel):
    document_id: int | None
    code: str
    message: str
    field: str | None = None
    line_no: int | None = None
    page: int | None = None


# ---------------------------------------------------------------- review


class TransactionOut(BaseModel):
    id: int
    document_id: int
    line_no: int
    txn_date: date
    raw_description: str
    bank_reference: str | None
    money_in: Decimal | None
    money_out: Decimal | None
    balance_after: Decimal | None
    page: int | None
    field_legibility: dict[str, str]


class AllocationOut(BaseModel):
    id: int
    bank_transaction_id: int
    run_id: int
    amount: Decimal
    # Null means genuinely unresolved. It is not Suspense, and an allocation
    # cannot be approved while it is null.
    account_id: str | None
    account_name: str | None
    tax_code: str | None
    decision_method: DecisionMethod
    # Null when decision_method is HUMAN: a person's answer is not a probability.
    confidence: float | None
    status: AllocationStatus
    reasoning: str | None
    question: str | None
    matched_document_id: int | None
    matched_rule_id: int | None
    approved_by: int | None
    approved_at: datetime | None


class TransactionDetail(BaseModel):
    transaction: TransactionOut
    allocations: list[AllocationOut]
    document: DocumentOut | None = None
    matched_document: DocumentOut | None = None


class ReviewAction(BaseModel):
    action: str = Field(description="approve | override | split")
    account_code: str | None = None
    tax_code: str | None = None
    note: str | None = None
    # Ask the model for a match pattern, show what it would capture, and save
    # it. Blocked automatically if the description was not read cleanly.
    create_rule: bool = False
    # For action=split: parts that must sum to the transaction amount.
    parts: list[dict] | None = None


class ReviewResult(BaseModel):
    allocation: AllocationOut
    message: str
    rule_created: dict | None = None
    rule_preview_count: int | None = None
    rule_blocked_reason: str | None = None


class BulkReview(BaseModel):
    allocation_ids: list[int]
    action: str = "approve"
    create_rule: bool = False


class QueryAnswer(BaseModel):
    answer: str
    account_code: str | None = None


class ExtractionFix(BaseModel):
    field_name: str
    new_value: str
    document_id: int | None = None
    transaction_id: int | None = None


# ---------------------------------------------------------------- memory


class RuleOut(BaseModel):
    id: int
    client_id: int
    match_pattern: str
    match_type: str
    account_id: str
    account_name: str | None
    tax_code: str | None
    confirm_count: int
    is_active: bool
    last_applied_at: datetime | None
    created_at: datetime | None


class AccountOut(BaseModel):
    code: str
    name: str
    type: str
    default_tax_code: str
    risk_level: str
    normal_balance: str
    notes: str | None = None


class TaxCodeOut(BaseModel):
    code: str
    name: str
    rate: Decimal
    claimable: bool
    applies_to: str
    requires_review: bool
    description: str | None = None

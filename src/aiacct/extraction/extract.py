"""Call 2: read the document.

Whole documents, never page by page. The header, the closing balance, and the
running-balance chain live in different parts of the file, and a description
can wrap across a page break. Fragmenting the input and then blaming the model
for the result would be an architecture bug, not a model limitation.

A tabular export skips this call entirely - it is already structured.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..ingestion import (
    FileKind,
    RoutedFile,
    parse_date,
    read_docx_text,
    read_pdf_text,
    read_tabular_statement,
)
from ..llm import LLMClient
from ..models import (
    DocumentType,
    Legibility,
    StatementExtraction,
    SupportingDocExtraction,
)
from ..db.models import (
    BankTransaction,
    Document,
)
from .field_policy import LEGIBILITY_INSTRUCTIONS

log = logging.getLogger(__name__)


STATEMENT_PROMPT = """\
Extract every transaction from this bank statement.

Rules that matter:

  * Record amounts exactly as printed. Put withdrawals in `money_out` and
    deposits in `money_in`, and never combine them into one signed number.
    A bank statement is written from the bank's point of view, which is the
    mirror image of the account holder's, so inferring a sign is how errors
    creep in.
  * Include every line, in the order printed. Do not merge, split, reorder or
    tidy them.
  * `raw_description` must be exactly what is printed, including reference
    numbers and punctuation. Do not expand abbreviations or correct spelling -
    this is the audit record.
  * Do NOT include the brought-forward or carried-forward balance lines as
    transactions. Report those as `opening_balance` and `closing_balance`.
  * `balance_after` is the running balance printed on that line. Leave it null
    if the statement has no balance column.
  * `page` is the page the line appeared on.
  * Dates must be returned as ISO-8601, `YYYY-MM-DD`. Statements are often
    printed as `29/01/2026`; convert them. Singapore statements are day-first,
    so `03/05/2026` is 3 May.
  * If the statement states how many transactions it contains, report that as
    `stated_transaction_count`.

{legibility}
"""

STATEMENT_TEXT_PROMPT = STATEMENT_PROMPT + """
The statement's text layer follows. Page markers show where each page begins.

<statement>
{text}
</statement>
"""

SUPPORTING_PROMPT = """\
Extract the key fields from this supporting document.

`summary` is the most important field. Write one plain sentence describing what
was actually bought or supplied - for example "Dell Latitude 5450 laptop, 1
unit" or "Repaint meeting room and repair two office partitions".

This matters because the bank statement only records who was paid. Whether a
payment is an office expense or a capitalised asset depends entirely on what
was purchased, and this document is the only place that appears.

`total_amount` is the amount payable including tax. `tax_amount` is the GST
shown. If no GST is shown, report null rather than calculating one - a supplier
who is not GST-registered charges none, and that is a meaningful fact.

`doc_date` must be ISO-8601, `YYYY-MM-DD`. Convert whatever the document prints,
including forms like "28 December 2025".

{legibility}
"""

SUPPORTING_TEXT_PROMPT = SUPPORTING_PROMPT + """
<document>
{text}
</document>
"""


def _to_decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _to_date(value: str | None) -> date | None:
    """Parse a date the model returned.

    The prompt asks for ISO, but a model reading a statement printed as
    "29/01/2026" will sometimes hand back what it saw. Reusing the ingestion
    parser means the same formats are understood wherever a date arrives from,
    rather than silently dropping the row.
    """
    if not value:
        return None
    parsed = parse_date(value)
    if parsed is None:
        log.warning("unparseable date from extraction: %r", value)
    return parsed


def _legibility_map(items) -> dict[str, Legibility]:
    """Only fields the model flagged appear; anything absent was clear."""
    return {item.field: item.legibility for item in items}


# ---------------------------------------------------------------- statements


def extract_statement(
    routed: RoutedFile,
    llm: LLMClient,
    effort: str = "medium",
    retry_note: str | None = None,
) -> tuple[StatementExtraction, int, int]:
    """Read a statement, returning the raw extraction and token usage.

    ``retry_note`` carries the specific failure from a previous attempt, along
    with the balances the answer has to chain between - so a retry is more
    constrained than the first attempt rather than a plain repeat.
    """
    if routed.kind == FileKind.TABULAR:
        # Already structured. Parsing it costs nothing and cannot hallucinate.
        return read_tabular_statement(routed.path, llm), 0, 0

    prompt_extra = f"\n\n{retry_note}\n" if retry_note else ""

    if routed.kind == FileKind.PDF_DIGITAL:
        text = read_pdf_text(routed.path)
        prompt = STATEMENT_TEXT_PROMPT.format(
            legibility=LEGIBILITY_INSTRUCTIONS, text=text
        ) + prompt_extra
        result = llm.parse(
            prompt=prompt, schema=StatementExtraction, effort=effort,
            source_hint=routed.path,
        )
    elif routed.kind == FileKind.DOCX:
        text = read_docx_text(routed.path)
        prompt = STATEMENT_TEXT_PROMPT.format(
            legibility=LEGIBILITY_INSTRUCTIONS, text=text
        ) + prompt_extra
        result = llm.parse(
            prompt=prompt, schema=StatementExtraction, effort=effort,
            source_hint=routed.path,
        )
    else:
        prompt = STATEMENT_PROMPT.format(legibility=LEGIBILITY_INSTRUCTIONS) + prompt_extra
        result = llm.parse(
            prompt=prompt,
            schema=StatementExtraction,
            files=[routed.path],
            effort=effort,
            # A scan of dense tabular figures needs the detail.
            detail="high",
            source_hint=routed.path,
        )

    return result.parsed, result.input_tokens, result.output_tokens


def apply_statement_to_document(doc: Document, extraction: StatementExtraction) -> Document:
    doc.bank_name = extraction.bank_name
    doc.account_number = extraction.account_number
    doc.account_holder = extraction.account_holder
    doc.period_start = _to_date(extraction.period_start)
    doc.period_end = _to_date(extraction.period_end)
    doc.opening_balance = _to_decimal(extraction.opening_balance)
    doc.closing_balance = _to_decimal(extraction.closing_balance)
    doc.field_legibility = _legibility_map(extraction.uncertain_fields)
    return doc


def to_bank_transactions(
    extraction: StatementExtraction, document_id: int, client_id: int
) -> list[BankTransaction]:
    transactions: list[BankTransaction] = []
    for row in extraction.transactions:
        txn_date = _to_date(row.txn_date)
        if txn_date is None:
            log.warning("skipping line %s: unusable date %r", row.line_no, row.txn_date)
            continue
        transactions.append(
            BankTransaction(
                document_id=document_id,
                client_id=client_id,
                line_no=row.line_no,
                txn_date=txn_date,
                raw_description=row.raw_description.strip(),
                bank_reference=(row.bank_reference or "").strip() or None,
                money_in=_to_decimal(row.money_in),
                money_out=_to_decimal(row.money_out),
                balance_after=_to_decimal(row.balance_after),
                page=row.page,
                field_legibility=_legibility_map(row.uncertain_fields),
            )
        )
    return transactions


# ---------------------------------------------------------------- documents


def extract_supporting(
    routed: RoutedFile, llm: LLMClient, effort: str = "medium"
) -> tuple[SupportingDocExtraction, int, int]:
    if routed.kind == FileKind.PDF_DIGITAL:
        text = read_pdf_text(routed.path)
        prompt = SUPPORTING_TEXT_PROMPT.format(
            legibility=LEGIBILITY_INSTRUCTIONS, text=text
        )
        result = llm.parse(
            prompt=prompt, schema=SupportingDocExtraction, effort=effort,
            source_hint=routed.path,
        )
    elif routed.kind == FileKind.DOCX:
        text = read_docx_text(routed.path)
        prompt = SUPPORTING_TEXT_PROMPT.format(
            legibility=LEGIBILITY_INSTRUCTIONS, text=text
        )
        result = llm.parse(
            prompt=prompt, schema=SupportingDocExtraction, effort=effort,
            source_hint=routed.path,
        )
    else:
        prompt = SUPPORTING_PROMPT.format(legibility=LEGIBILITY_INSTRUCTIONS)
        result = llm.parse(
            prompt=prompt,
            schema=SupportingDocExtraction,
            files=[routed.path],
            effort=effort,
            detail="high",
            source_hint=routed.path,
        )
    return result.parsed, result.input_tokens, result.output_tokens


def apply_supporting_to_document(doc: Document, extraction: SupportingDocExtraction) -> Document:
    doc.vendor_name = extraction.vendor_name
    doc.doc_number = extraction.doc_number
    doc.doc_date = _to_date(extraction.doc_date)
    doc.total_amount = _to_decimal(extraction.total_amount)
    doc.tax_amount = _to_decimal(extraction.tax_amount)
    doc.summary = extraction.summary
    doc.field_legibility = _legibility_map(extraction.uncertain_fields)
    return doc


SUPPORTING_TYPES = {DocumentType.INVOICE, DocumentType.RECEIPT, DocumentType.PAYROLL}

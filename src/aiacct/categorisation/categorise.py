"""Call 3: decide what each remaining transaction was for.

This call never sees a file. It reads structured rows written by extraction,
plus the client profile, the chart of accounts, and a handful of the
accountant's past corrections - a couple of thousand tokens, not a PDF.

Transactions are batched. The prompt is mostly shared context, so sending one
call per transaction would resend the chart of accounts twenty times for
identical output, at roughly nine times the tokens and twenty times the
latency.

Note what is *not* requested: a self-reported confidence score. Models cluster
those near 0.9 regardless of input. The ranked alternatives are asked for
instead, because the gap between first and second choice is an observation
about how close the call was, and the final score is computed elsewhere from
signals this call never sees.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

from ..config import get_settings
from ..llm import LLMClient
from ..models import (
    CategorisationBatch,
    TransactionCategorisation,
)
from ..db.models import (
    BankTransaction,
    Client,
    Document,
)
from ..reference import get_chart_of_accounts, get_tax_codes
from .memory import Example, format_examples, format_facts

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are assisting a Singapore accounting firm to code a client's bank
transactions to their general ledger.

The bank statement already tells us that money moved. Your job is the other
side of the entry: which account it belongs to, and why.

CLIENT
{client_name}
{business_description}
GST registered: {gst_registered}
Capitalisation threshold: purchases at or above SGD {capitalisation_threshold}
are fixed assets rather than expenses.

Bank accounts this client owns. A payment to any of these is a transfer between
their own accounts - it is NOT income or expense and has no effect on profit:
{own_accounts}

Facts previously confirmed by this client:
{facts}

CHART OF ACCOUNTS - choose one code from this list and nothing else. If none
fits, return null rather than inventing a code or reaching for a vague one.
{accounts}

TAX CODES
{tax_codes}

HOW TO THINK ABOUT THIS

Most outgoing payments are not expenses. Before reaching for an expense
account, consider whether the money instead:
  - settled a liability already recorded (GST to IRAS, CPF, loan principal)
  - bought something lasting (equipment above the capitalisation threshold)
  - moved between the client's own accounts
  - was taken out by a director (drawings, which reduce equity and are not
    deductible - quite different from salary or a subcontractor fee)

Specific traps in Singapore:
  - GST on medical expenses, private motor car running costs, and club
    subscriptions is blocked (BL) and cannot be reclaimed even with a valid tax
    invoice.
  - Services bought from overseas suppliers are imported services and attract
    reverse charge (TX-RC), not ordinary TX.
  - Wages and CPF are not supplies at all, so they carry no GST (OP).
  - A refund received is a credit against the original expense account, not
    revenue.

WHAT THE PREVIOUS CORRECTIONS MEAN
These are decisions this firm's accountants already made for this same client.
They are authoritative. If one clearly applies, follow it.
{examples}

OUTPUT

For each transaction return:
  - account_code, or null if you genuinely cannot tell
  - alternatives: your ranked candidates with scores. Be honest about how close
    they are. Two accounts scoring 0.70 and 0.65 tells us to ask a human, which
    is far more useful than a confident single answer that turns out wrong.
  - identifiable: false when the description gives you nothing to reason from -
    no merchant name and no recognisable kind of transaction. "SERVICE CHARGE"
    names nobody but is identifiable; "TRF 8891234" is not. Judge the
    description, not your confidence in the account.
  - reasoning: one or two sentences an accountant can check at a glance
  - clarification_question: when you cannot resolve it, the question you would
    put to the client. Name the transaction, the amount, and what turns on the
    answer.
  - needs_split: true when one payment covers genuinely different things - a
    loan repayment is part principal and part interest, and the ratio comes
    from the loan schedule, which you cannot know.

Do not guess to avoid returning null. An honest "I do not know" costs an
accountant thirty seconds; a confident wrong answer can go unnoticed for months.
"""

TRANSACTIONS_PROMPT = """
Code the following transactions. Amounts are SGD. `direction` says whether
money left or entered the account.

<transactions>
{transactions}
</transactions>
"""


def build_prompt(
    client: Client,
    transactions: list[BankTransaction],
    documents: dict[int, Document],
    examples: list[Example],
) -> str:
    profile = client.profile
    coa = get_chart_of_accounts()
    tax = get_tax_codes()

    payload = []
    for txn in transactions:
        entry = {
            "transaction_id": txn.id,
            "date": txn.txn_date.isoformat(),
            "description": txn.raw_description,
            "reference": txn.bank_reference,
            "amount": str(txn.amount),
            "direction": "money in" if txn.is_inflow else "money out",
        }
        doc = documents.get(txn.id)
        if doc is not None:
            # What was actually bought. The description says who was paid; only
            # this says what for, which is what separates an office expense
            # from a capitalised asset.
            entry["document_summary"] = doc.summary
            entry["document_vendor"] = doc.vendor_name
            entry["document_total"] = str(doc.total_amount) if doc.total_amount else None
            entry["document_gst"] = str(doc.tax_amount) if doc.tax_amount else None
        else:
            entry["document_summary"] = None
            entry["supporting_document"] = "none found"
        payload.append(entry)

    header = SYSTEM_PROMPT.format(
        client_name=client.name,
        business_description=profile.business_description or "(not recorded)",
        gst_registered="yes" if profile.gst_registered else "no",
        capitalisation_threshold=profile.capitalisation_threshold,
        own_accounts="\n".join(f"  - {a}" for a in profile.own_bank_accounts) or "  (none recorded)",
        facts=format_facts(profile.learned_facts),
        accounts=coa.prompt_listing(),
        tax_codes=tax.prompt_listing(),
        examples=format_examples(examples),
    )
    return header + TRANSACTIONS_PROMPT.format(
        transactions=json.dumps(payload, indent=2)
    )


def categorise_batch(
    client: Client,
    transactions: list[BankTransaction],
    documents: dict[int, Document],
    examples: list[Example],
    llm: LLMClient,
    effort: str | None = None,
) -> tuple[list[TransactionCategorisation], int, int]:
    """Code one batch. Returns results plus token usage."""
    if not transactions:
        return [], 0, 0

    settings = get_settings()
    prompt = build_prompt(client, transactions, documents, examples)
    result = llm.parse(
        prompt=prompt,
        schema=CategorisationBatch,
        effort=effort or settings.effort_categorise,
    )

    returned = {r.transaction_id for r in result.parsed.results}
    missing = {t.id for t in transactions} - returned
    if missing:
        # Silently dropping a transaction would leave the bank account
        # unexplained, so this is worth shouting about.
        log.warning("categorisation omitted transaction ids %s", sorted(missing))

    return result.parsed.results, result.input_tokens, result.output_tokens


def batches(items: list[BankTransaction], size: int | None = None):
    size = size or get_settings().categorisation_batch_size
    for start in range(0, len(items), size):
        yield items[start:start + size]


def split_allocations(
    txn: BankTransaction, note: str | None
) -> list[tuple[str | None, Decimal]]:
    """Placeholder amounts for a transaction the model flagged as a split.

    Deliberately does not guess the ratio. A loan repayment divides into
    principal and interest according to the loan schedule, which is not in any
    document the system has seen, so the whole amount stays on one unresolved
    line and a human enters the real split at review.
    """
    return [(None, txn.amount)]

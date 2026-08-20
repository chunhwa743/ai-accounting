"""Works out which uploaded invoice belongs to which bank transaction.

Nothing in either file records the link. The invoice does not say when it was
paid, and the bank line does not say which invoice it settled, so it has to be
inferred from amount, date and vendor together.

Deterministic on purpose. This is a comparison problem - amount equality, date
arithmetic, string similarity - not a reasoning problem. Letting a model do it
would mean sending every invoice alongside every transaction, and would still
be less consistent than arithmetic. A score can also be tuned and measured
against ground truth; "the model thought so" cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from rapidfuzz import fuzz

from ..config import get_settings
from ..db.models import BankTransaction, Document

# Amount carries most of the signal, but not all of it: two different suppliers
# can be paid the same round figure in the same week, and then only the vendor
# name separates them.
WEIGHT_AMOUNT = 0.60
WEIGHT_DATE = 0.20
WEIGHT_VENDOR = 0.20

AMOUNT_TOLERANCE = Decimal("0.01")


@dataclass
class Match:
    document: Document
    score: float
    amount_matched: bool
    days_apart: int | None
    vendor_similarity: float

    def explain(self) -> str:
        parts = [f"amount {'exact' if self.amount_matched else 'differs'}"]
        if self.days_apart is not None:
            parts.append(f"paid {self.days_apart}d after invoice date")
        parts.append(f"vendor {self.vendor_similarity:.0%} similar")
        return ", ".join(parts)


def _vendor_similarity(vendor: str | None, description: str) -> float:
    """How strongly a vendor name shows up in a bank description.

    Partial ratio rather than a plain one, because the description wraps the
    name in payment-rail noise: "Acme Supplies Pte Ltd" has to be found inside
    "PAYNOW-ACME SUPPLIES PTE LTD-88291".
    """
    if not vendor:
        return 0.0
    return fuzz.partial_ratio(vendor.upper(), description.upper()) / 100.0


def score_match(txn: BankTransaction, doc: Document) -> Match:
    settings = get_settings()

    amount_matched = (
        doc.total_amount is not None
        and abs(txn.amount - doc.total_amount) <= AMOUNT_TOLERANCE
    )

    days_apart: int | None = None
    date_score = 0.0
    if doc.doc_date is not None:
        days_apart = (txn.txn_date - doc.doc_date).days
        if 0 <= days_apart <= settings.document_match_max_days:
            # Payment soon after the invoice is the normal case; the signal
            # decays as the gap widens.
            date_score = 1.0 - (days_apart / (settings.document_match_max_days * 2))
        elif -3 <= days_apart < 0:
            # A receipt can be dated a day or two after the card settles.
            date_score = 0.7

    vendor_similarity = _vendor_similarity(doc.vendor_name, txn.raw_description)

    score = (
        WEIGHT_AMOUNT * (1.0 if amount_matched else 0.0)
        + WEIGHT_DATE * date_score
        + WEIGHT_VENDOR * (vendor_similarity if vendor_similarity >= 0.75 else 0.0)
    )

    return Match(
        document=doc,
        score=round(score, 3),
        amount_matched=amount_matched,
        days_apart=days_apart,
        vendor_similarity=vendor_similarity,
    )


def match_documents(
    transactions: list[BankTransaction], documents: list[Document]
) -> dict[int, Match]:
    """Best document per transaction, above the configured threshold.

    Runs over every transaction, not only the ones still needing an account: a
    matched invoice supplies the GST figure and an audit reference even where a
    learned rule already decided the coding.

    One document is claimed by at most one transaction. A payment settling
    several invoices at once is a subset-sum problem and is deliberately out of
    scope; those simply stay unmatched and surface as a query rather than being
    matched wrongly.
    """
    settings = get_settings()
    candidates: list[tuple[float, int, Match]] = []

    for txn in transactions:
        if txn.id is None:
            continue
        for doc in documents:
            if doc.total_amount is None:
                continue
            match = score_match(txn, doc)
            if match.score >= settings.document_match_min_score:
                candidates.append((match.score, txn.id, match))

    # Highest-scoring pairs first, so a confident match is not stolen by a
    # weaker one that happened to be considered earlier.
    candidates.sort(key=lambda c: -c[0])

    matched: dict[int, Match] = {}
    claimed: set[int] = set()
    for _, txn_id, match in candidates:
        if txn_id in matched or match.document.id in claimed:
            continue
        matched[txn_id] = match
        claimed.add(match.document.id)

    return matched


def unmatched_documents(
    documents: list[Document], matched: dict[int, Match]
) -> list[Document]:
    """Documents that matched nothing.

    Worth surfacing: an invoice with no payment is either unpaid, paid from
    another account, or settled in a different period. Any of those is
    something an accountant wants to know.
    """
    claimed = {m.document.id for m in matched.values()}
    return [d for d in documents if d.id not in claimed]

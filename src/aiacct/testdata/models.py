"""The shapes the test data is parsed into and rendered from.

Deliberately plain dataclasses: they are read from markdown, handed to a
renderer, and compared against by the evaluation harness. Nothing persists
them, so they are not ORM models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

SEED = 20260101


@dataclass(frozen=True)
class SupportingDoc:
    """An invoice or receipt that exists for a transaction.

    ``summary`` is the field that earns this document its place: the bank
    description says who was paid, and only this says what for.
    """

    kind: str                      # INVOICE | RECEIPT | PAYROLL
    fmt: str                       # pdf | jpg | docx | xlsx
    doc_number: str
    doc_date: date
    vendor_name: str
    total: Decimal
    tax: Decimal
    summary: str
    line_items: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Txn:
    day: int
    description: str
    reference: str | None = None
    money_out: Decimal | None = None
    money_in: Decimal | None = None

    # ---- ground truth, never read by the pipeline ----
    expected_account: str | None = None
    expected_tax: str | None = None
    # More than one allocation: (account, amount) pairs summing to the line.
    expected_split: tuple[tuple[str, str], ...] | None = None
    # Why this one is hard, shown in the evaluation report.
    difficulty: str = ""
    doc: SupportingDoc | None = None


D = Decimal


def running_balances(opening: Decimal, txns: list[Txn]) -> list[Decimal]:
    balances, running = [], opening
    for txn in txns:
        running += (txn.money_in or Decimal("0")) - (txn.money_out or Decimal("0"))
        balances.append(running)
    return balances


def closing_balance(opening: Decimal, txns: list[Txn]) -> Decimal:
    return running_balances(opening, txns)[-1] if txns else opening


def period_dates(period: str) -> tuple[date, date]:
    year, month = (int(p) for p in period.split("-"))
    start = date(year, month, 1)
    end = date(year + (month // 12), (month % 12) + 1, 1)
    return start, date.fromordinal(end.toordinal() - 1)

"""Deterministic checks on an extracted statement. No model involvement.

This is where honest confidence starts. Extraction is the one part of the
system with ground truth - the answer is printed on the page - and arithmetic
can prove whether we read it correctly. Nothing downstream has that luxury.

Five checks, each covering a blind spot of the others:

  1. aggregate balance  - wrong amounts, missing rows, duplicated rows, in total
  2. per-row balance    - WHICH row is wrong; check 1 only says "somewhere"
  3. dates              - wrong dates, which are invisible to 1 and 2 because
                          they do not participate in arithmetic
  4. legibility         - unreadable text and identifiers, invisible to 1-3
  5. stated count       - missing rows when check 1 cannot run at all

The governing rule: arithmetic decides whether to stop; the model's own sense
of legibility only decides how suspicious to be later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..models import Legibility, Verdict
from ..db.models import BankTransaction
from .field_policy import escalating_fields

# Tolerance for a single reconciliation. Statements are printed to the cent;
# anything larger is a real discrepancy, not rounding.
TOLERANCE = Decimal("0.01")

# Issues that mean the figures themselves do not add up, as opposed to issues
# that stop the run for other reasons.
BALANCE_CODES = {"balance_mismatch", "running_balance_break", "count_mismatch"}


@dataclass
class Issue:
    code: str
    message: str
    line_no: int | None = None
    page: int | None = None
    field: str | None = None
    blocking: bool = True


@dataclass
class ValidationReport:
    verdict: Verdict
    issues: list[Issue] = field(default_factory=list)
    computed_closing: Decimal | None = None
    stated_closing: Decimal | None = None
    difference: Decimal | None = None

    @property
    def reconciles(self) -> bool | None:
        """Whether the arithmetic verified. Three states, deliberately.

        NULL means the check could not run because no balances were printed.
        Recording that as True would let an unverified extraction pass as
        verified; recording it as False would escalate every CSV export for no
        reason.

        Note this describes the *arithmetic only*. A smudged account number
        stops the run, but it says nothing about whether the figures add up -
        and downstream this value is used to decide how much to trust the
        amounts, so conflating the two would penalise every transaction on a
        statement whose header was hard to read.
        """
        if self.verdict == Verdict.UNVERIFIABLE:
            return None
        return not any(i.code in BALANCE_CODES for i in self.issues)

    @property
    def blocking_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.blocking]

    @property
    def failing_pages(self) -> list[int]:
        return sorted({i.page for i in self.issues if i.blocking and i.page is not None})

    @property
    def failing_lines(self) -> list[int]:
        return sorted({i.line_no for i in self.issues if i.blocking and i.line_no is not None})

    def summary(self) -> str:
        if not self.issues:
            return "statement reconciles"
        return "; ".join(f"{i.code}: {i.message}" for i in self.issues[:6])


def _movement(txn: BankTransaction) -> Decimal:
    return (txn.money_in or Decimal("0")) - (txn.money_out or Decimal("0"))


def validate_statement(
    transactions: list[BankTransaction],
    opening_balance: Decimal | None,
    closing_balance: Decimal | None,
    period_start: date | None = None,
    period_end: date | None = None,
    header_legibility: dict[str, Legibility] | None = None,
    stated_count: int | None = None,
) -> ValidationReport:
    issues: list[Issue] = []

    # -- check 4: legibility -------------------------------------------------
    # Runs first because an unreadable identifier blocks regardless of whether
    # the arithmetic works out.
    for field_name in escalating_fields(header_legibility or {}):
        issues.append(
            Issue(
                code="unreadable_identifier",
                message=(
                    f"{field_name} could not be read reliably. A different reading "
                    f"would refer to a different entity, and nothing can verify it."
                ),
                field=field_name,
            )
        )
    for txn in transactions:
        for field_name in escalating_fields(txn.field_legibility):
            issues.append(
                Issue(
                    code="unreadable_identifier",
                    message=f"line {txn.line_no}: {field_name} could not be read reliably",
                    line_no=txn.line_no,
                    page=txn.page,
                    field=field_name,
                )
            )

    # -- check 3: dates ------------------------------------------------------
    # Invisible to the balance checks: a date read as 15 Jan instead of 15 Jul
    # still reconciles perfectly, but lands the transaction in the wrong
    # accounting period.
    if period_start and period_end:
        for txn in transactions:
            if not (period_start <= txn.txn_date <= period_end):
                issues.append(
                    Issue(
                        code="date_out_of_period",
                        message=(
                            f"line {txn.line_no}: {txn.txn_date} falls outside the "
                            f"statement period {period_start} to {period_end}"
                        ),
                        line_no=txn.line_no,
                        page=txn.page,
                        field="txn_date",
                    )
                )

    # Out-of-order dates are a soft flag: some banks group by transaction type
    # rather than printing in strict date order.
    for previous, current in zip(transactions, transactions[1:]):
        if current.txn_date < previous.txn_date:
            issues.append(
                Issue(
                    code="date_out_of_order",
                    message=(
                        f"line {current.line_no}: {current.txn_date} precedes line "
                        f"{previous.line_no} ({previous.txn_date})"
                    ),
                    line_no=current.line_no,
                    page=current.page,
                    field="txn_date",
                    blocking=False,
                )
            )

    # -- check 5: stated count ----------------------------------------------
    if stated_count is not None and stated_count != len(transactions):
        issues.append(
            Issue(
                code="count_mismatch",
                message=(
                    f"statement declares {stated_count} transactions but "
                    f"{len(transactions)} were extracted"
                ),
            )
        )

    # -- checks 1 and 2: balances -------------------------------------------
    if opening_balance is None or closing_balance is None:
        # Not a failure. Some exports print no balances at all, and "could not
        # check" must stay distinct from "checked and fine".
        verdict = Verdict.FAIL if any(i.blocking for i in issues) else Verdict.UNVERIFIABLE
        return ValidationReport(verdict=verdict, issues=issues)

    total_movement = sum((_movement(t) for t in transactions), Decimal("0"))
    computed = opening_balance + total_movement
    difference = computed - closing_balance

    if abs(difference) > TOLERANCE:
        issues.append(
            Issue(
                code="balance_mismatch",
                message=(
                    f"opening {opening_balance} + movements {total_movement} = {computed}, "
                    f"but the statement closes at {closing_balance} "
                    f"(difference {difference})"
                ),
            )
        )

        # Check 2 turns "something in these 45 rows is wrong" into "row 23 is
        # wrong", which is what makes a targeted retry and a ten-second human
        # fix possible.
        running = opening_balance
        for txn in transactions:
            running += _movement(txn)
            if txn.balance_after is None:
                continue
            drift = running - txn.balance_after
            if abs(drift) > TOLERANCE:
                issues.append(
                    Issue(
                        code="running_balance_break",
                        message=(
                            f"line {txn.line_no}: expected balance {running}, "
                            f"statement shows {txn.balance_after} (out by {drift})"
                        ),
                        line_no=txn.line_no,
                        page=txn.page,
                    )
                )
                # Resynchronise so one bad row does not flag every later one.
                running = txn.balance_after

    verdict = Verdict.FAIL if any(i.blocking for i in issues) else Verdict.PASS
    return ValidationReport(
        verdict=verdict,
        issues=issues,
        computed_closing=computed,
        stated_closing=closing_balance,
        difference=difference,
    )


def find_possible_duplicates(transactions: list[BankTransaction]) -> list[tuple[int, int]]:
    """Pairs sharing a date, amount and description.

    Deliberately "possible": two identical coffees on one day are legitimate,
    and so is a supplier invoice paid twice by mistake. The system cannot tell
    them apart, so it does not try - it flags them for a human. A genuine
    extraction error duplicating a row would already have broken check 1.
    """
    seen: dict[tuple, int] = {}
    pairs: list[tuple[int, int]] = []
    for txn in transactions:
        key = (
            txn.txn_date,
            txn.money_in,
            txn.money_out,
            txn.raw_description.strip().upper(),
        )
        if key in seen and txn.id is not None:
            pairs.append((seen[key], txn.id))
        elif txn.id is not None:
            seen[key] = txn.id
    return pairs


def retry_context(report: ValidationReport, opening: Decimal | None, closing: Decimal | None) -> str:
    """The error description fed back into a re-extraction.

    A retry is given the balances the answer must chain between, so it is more
    constrained than the first attempt rather than merely repeated.
    """
    lines = ["The previous extraction did not reconcile. Specific problems:"]
    for issue in report.blocking_issues[:10]:
        lines.append(f"  - {issue.message}")
    if opening is not None and closing is not None:
        lines.append("")
        lines.append(
            f"The opening balance is {opening} and the closing balance is {closing}. "
            f"Opening plus all money in, minus all money out, must equal the closing "
            f"balance exactly. Re-read the statement carefully, paying particular "
            f"attention to the lines named above."
        )
    return "\n".join(lines)

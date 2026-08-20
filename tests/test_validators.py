"""The extraction checks are the only place with ground truth, so they get the
most tests. If these are wrong, everything downstream inherits bad numbers
without knowing it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from aiacct.extraction.field_policy import Action, classify_field, decide, escalating_fields
from aiacct.extraction.validators import (
    find_possible_duplicates,
    validate_statement,
)
from aiacct.models import FieldClass, Legibility, Verdict
from aiacct.db.models import BankTransaction


def txn(line_no, day, desc, out=None, inn=None, balance=None, page=1, legibility=None):
    return BankTransaction(
        document_id=1,
        client_id=1,
        line_no=line_no,
        txn_date=date(2026, 2, day),
        raw_description=desc,
        money_in=Decimal(str(inn)) if inn is not None else None,
        money_out=Decimal(str(out)) if out is not None else None,
        balance_after=Decimal(str(balance)) if balance is not None else None,
        page=page,
        field_legibility=legibility or {},
        id=line_no,
    )


def clean_statement():
    """Opening 10000, three movements, closing 8420."""
    return [
        txn(1, 3, "GIRO PAYMENT SINGTEL", out=500, balance=9500),
        txn(2, 10, "PAYNOW-ACME SUPPLIES", out=1090, balance=8410),
        txn(3, 20, "PAYMENT RECEIVED INV-1002", inn=10, balance=8420),
    ]


class TestBalanceChecks:
    def test_clean_statement_passes(self):
        report = validate_statement(
            clean_statement(), Decimal("10000.00"), Decimal("8420.00")
        )
        assert report.verdict == Verdict.PASS
        assert report.reconciles is True
        assert report.blocking_issues == []

    def test_wrong_digit_is_caught_and_the_row_is_named(self):
        # A misread on line 2: 1090 read as 1990. The aggregate check notices
        # something is wrong; the per-row check says which line.
        rows = clean_statement()
        rows[1].money_out = Decimal("1990.00")

        report = validate_statement(rows, Decimal("10000.00"), Decimal("8420.00"))

        assert report.verdict == Verdict.FAIL
        assert report.reconciles is False
        assert any(i.code == "balance_mismatch" for i in report.issues)
        assert 2 in report.failing_lines, "the per-row check must name the bad line"

    def test_missing_transaction_breaks_the_aggregate(self):
        rows = clean_statement()[:2]
        report = validate_statement(rows, Decimal("10000.00"), Decimal("8420.00"))
        assert report.verdict == Verdict.FAIL

    def test_no_balances_is_unverifiable_not_a_pass(self):
        # Some CSV exports print no balances. "Could not check" must never be
        # recorded as "checked and fine".
        report = validate_statement(clean_statement(), None, None)
        assert report.verdict == Verdict.UNVERIFIABLE
        assert report.reconciles is None, "must be NULL, not True or False"

    def test_one_cent_rounding_still_passes(self):
        report = validate_statement(
            clean_statement(), Decimal("10000.00"), Decimal("8420.01")
        )
        assert report.verdict == Verdict.PASS


class TestDateChecks:
    def test_date_outside_period_is_blocking(self):
        # Invisible to arithmetic: the balances still reconcile perfectly, but
        # the transaction lands in the wrong accounting period.
        rows = clean_statement()
        rows[0].txn_date = date(2026, 7, 15)

        report = validate_statement(
            rows,
            Decimal("10000.00"),
            Decimal("8420.00"),
            period_start=date(2026, 2, 1),
            period_end=date(2026, 2, 28),
        )

        assert report.verdict == Verdict.FAIL
        assert any(i.code == "date_out_of_period" for i in report.issues)

    def test_out_of_order_dates_are_only_a_soft_flag(self):
        # Some banks group by transaction type rather than date order.
        rows = clean_statement()
        rows[2].txn_date = date(2026, 2, 5)

        report = validate_statement(
            rows,
            Decimal("10000.00"),
            Decimal("8420.00"),
            period_start=date(2026, 2, 1),
            period_end=date(2026, 2, 28),
        )

        assert report.verdict == Verdict.PASS
        assert any(i.code == "date_out_of_order" and not i.blocking for i in report.issues)


class TestCountCheck:
    def test_stated_count_catches_a_gap_when_balances_are_absent(self):
        report = validate_statement(clean_statement(), None, None, stated_count=5)
        assert report.verdict == Verdict.FAIL
        assert any(i.code == "count_mismatch" for i in report.issues)


class TestFieldPolicy:
    @pytest.mark.parametrize(
        "field_name,expected",
        [
            ("raw_description", FieldClass.REDUNDANT),
            ("money_out", FieldClass.VERIFIABLE),
            ("bank_reference", FieldClass.IDENTIFIER),
            ("account_number", FieldClass.IDENTIFIER),
            ("doc_number", FieldClass.IDENTIFIER),
        ],
    )
    def test_field_classification(self, field_name, expected):
        assert classify_field(field_name) == expected

    def test_unknown_fields_fail_safe_to_identifier(self):
        # Escalating something harmless beats silently accepting a guess.
        assert classify_field("something_new") == FieldClass.IDENTIFIER

    def test_inferred_text_proceeds(self):
        # "SINGT?L" has exactly one plausible reading.
        assert decide("raw_description", Legibility.INFERRED) == Action.PROCEED

    def test_inferred_identifier_still_escalates(self):
        # A serial number has no context that makes alternatives implausible,
        # so a model claiming inference there is over-claiming.
        assert decide("bank_reference", Legibility.INFERRED) == Action.ESCALATE

    def test_unclear_amount_defers_to_arithmetic(self):
        assert decide("money_out", Legibility.AMBIGUOUS) == Action.LET_CHECK_DECIDE

    def test_ambiguous_text_penalises_without_blocking(self):
        assert decide("raw_description", Legibility.AMBIGUOUS) == Action.PENALISE

    def test_escalating_fields_picks_only_identifiers(self):
        legibility = {
            "raw_description": Legibility.INFERRED,
            "money_out": Legibility.AMBIGUOUS,
            "account_number": Legibility.AMBIGUOUS,
        }
        assert escalating_fields(legibility) == ["account_number"]


class TestLegibilityInValidation:
    def test_unreadable_account_number_blocks_the_statement(self):
        report = validate_statement(
            clean_statement(),
            Decimal("10000.00"),
            Decimal("8420.00"),
            header_legibility={"account_number": Legibility.AMBIGUOUS},
        )
        assert report.verdict == Verdict.FAIL
        assert any(i.code == "unreadable_identifier" for i in report.issues)

    def test_unclear_description_does_not_block(self):
        # Downgrade, do not block: phase 2 carries the penalty instead.
        rows = clean_statement()
        rows[0].field_legibility = {"raw_description": Legibility.AMBIGUOUS}

        report = validate_statement(rows, Decimal("10000.00"), Decimal("8420.00"))

        assert report.verdict == Verdict.PASS


class TestDuplicates:
    def test_identical_lines_are_flagged_not_removed(self):
        rows = [
            txn(1, 10, "PAYNOW-ACME SUPPLIES", out=1090, balance=8910),
            txn(2, 12, "PAYNOW-ACME SUPPLIES", out=1090, balance=7820),
        ]
        rows[1].txn_date = rows[0].txn_date
        assert find_possible_duplicates(rows) == [(1, 2)]

    def test_different_amounts_are_not_duplicates(self):
        rows = [
            txn(1, 10, "PAYNOW-ACME SUPPLIES", out=1090, balance=8910),
            txn(2, 10, "PAYNOW-ACME SUPPLIES", out=1091, balance=7819),
        ]
        assert find_possible_duplicates(rows) == []

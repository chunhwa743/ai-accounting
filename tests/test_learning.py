"""Memory: what gets learned, what does not, and who it belongs to."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from aiacct.categorisation import find_rule, preview_rule, should_create_rule
from aiacct.models import AllocationStatus, DecisionMethod, DocumentType, Legibility
from aiacct.db.models import Allocation, BankTransaction, Document, MerchantRule, Run
from aiacct.review import ReviewService, _fallback_pattern


def make_statement(repos, client_id: int) -> tuple[int, int]:
    run = repos.runs.create(Run(client_id=client_id))
    doc = repos.documents.create(
        Document(
            client_id=client_id, document_type=DocumentType.BANK_STATEMENT,
            original_filename="statement.pdf", storage_uri="/tmp/s.pdf",
            mime_type="application/pdf", file_hash=f"hash-{client_id}-{run.id}",
        ),
        run_id=run.id,
    )
    return run.id, doc.id


def add_transaction(repos, client_id, doc_id, description, amount="100.00", line_no=1,
                    legibility=None):
    return repos.transactions.bulk_create([
        BankTransaction(
            document_id=doc_id, client_id=client_id, line_no=line_no,
            txn_date=date(2026, 1, 15), raw_description=description,
            money_out=Decimal(amount), balance_after=Decimal("1000.00"),
            field_legibility=legibility or {},
        )
    ])[0]


def add_allocation(repos, run_id, txn, account="429", status=AllocationStatus.NEEDS_REVIEW):
    return repos.allocations.create(
        Allocation(
            bank_transaction_id=txn.id, run_id=run_id, amount=txn.amount,
            account_id=account, tax_code="TX",
            decision_method=DecisionMethod.LLM, confidence=0.62, status=status,
        )
    )


class TestPatternDerivation:
    @pytest.mark.parametrize(
        "description,expected",
        [
            ("GRAB *TRIP 8829 SG", "GRAB *TRIP"),
            ("GRABFOOD *ORDER 4471", "GRABFOOD *ORDER"),
            ("GIRO PAYMENT SINGTEL 2891004", "SINGTEL"),
            ("PAYNOW-ACME SUPPLIES PTE LTD-88291", "ACME SUPPLIES"),
            ("NETS QR NTUC FAIRPRICE #04-22", "NTUC FAIRPRICE"),
        ],
    )
    def test_patterns(self, description, expected):
        assert _fallback_pattern(description) == expected

    def test_pattern_is_always_a_substring_of_its_source(self):
        # A pattern that is not in the description would never fire.
        for description in [
            "GRABFOOD *ORDER 4471", "ADOBE *SYSTEMS 4085366000 USD89.99",
            "ESSO SERVICE STN 42 SBA1234S", "TRF TO 501-44012-8",
        ]:
            assert _fallback_pattern(description).upper() in description.upper()

    def test_travel_rule_does_not_capture_food(self):
        # The whole reason the pattern is proposed rather than normalised: a
        # rule of just "GRAB" would silently miscode every food order.
        pattern = _fallback_pattern("GRAB *TRIP 8829 SG")
        assert pattern.upper() not in "GRABFOOD *ORDER 4471"


class TestRuleMatching:
    def test_longest_pattern_wins(self, repos, agency):
        # Both match; the more specific one has to take precedence.
        rules = [
            MerchantRule(client_id=agency.id, match_pattern="GRAB", account_id="429"),
            MerchantRule(client_id=agency.id, match_pattern="GRAB *TRIP", account_id="493"),
        ]
        _, doc_id = make_statement(repos, agency.id)
        txn = add_transaction(repos, agency.id, doc_id, "GRAB *TRIP 9931 SG")

        hit = find_rule(txn, rules)
        assert hit.rule.account_id == "493"

    def test_preview_shows_what_a_rule_would_capture(self, repos, agency):
        _, doc_id = make_statement(repos, agency.id)
        for index, description in enumerate([
            "GRAB *TRIP 8829 SG", "GRAB *TRIP 9931 SG", "GRABFOOD *ORDER 4471",
        ], start=1):
            add_transaction(repos, agency.id, doc_id, description, line_no=index)

        history = repos.transactions.list_for_client_history(agency.id)
        captured = preview_rule("GRAB *TRIP", history)

        assert len(captured) == 2
        assert all("*TRIP" in t.raw_description for t in captured)


class TestRuleCreation:
    def test_correction_creates_a_rule(self, repos, agency, user):
        run_id, doc_id = make_statement(repos, agency.id)
        txn = add_transaction(repos, agency.id, doc_id, "GRAB *TRIP 8829 SG")
        allocation = add_allocation(repos, run_id, txn, account="429")

        service = ReviewService(repos)
        outcome = service.override(allocation.id, user.id, "493", create_rule=True)

        assert outcome.rule is not None
        assert outcome.rule.account_id == "493"
        assert outcome.correction.from_account_id == "429"
        assert outcome.correction.to_account_id == "493"
        # Kept so correction rate can be measured per confidence band.
        assert outcome.correction.from_confidence == 0.62

    def test_approving_a_flagged_item_can_also_teach(self, repos, agency, user):
        # "Yes this is right, and stop asking me" is the commonest way an
        # accountant creates a rule.
        run_id, doc_id = make_statement(repos, agency.id)
        txn = add_transaction(repos, agency.id, doc_id, "GIRO PAYMENT SINGTEL 2891004")
        allocation = add_allocation(repos, run_id, txn, account="489")

        outcome = ReviewService(repos).approve(allocation.id, user.id, create_rule=True)

        assert outcome.rule is not None
        assert outcome.rule.match_pattern == "SINGTEL"

    def test_guessed_description_never_becomes_a_rule(self, repos, agency, user):
        # A rule keyed on a half-read merchant name would miscode silently
        # every month. The allocation still stands - a person approved it.
        run_id, doc_id = make_statement(repos, agency.id)
        txn = add_transaction(
            repos, agency.id, doc_id, "PAYNOW-TAN WEI MING",
            legibility={"raw_description": Legibility.AMBIGUOUS},
        )
        allocation = add_allocation(repos, run_id, txn)

        outcome = ReviewService(repos).override(
            allocation.id, user.id, "310", create_rule=True
        )

        assert outcome.rule is None
        assert "ambiguous" in outcome.rule_blocked_reason
        assert outcome.allocation.account_id == "310"

    def test_opaque_description_never_becomes_a_rule(self, repos, agency, user):
        run_id, doc_id = make_statement(repos, agency.id)
        txn = add_transaction(repos, agency.id, doc_id, "TRF 8891234")
        allocation = add_allocation(repos, run_id, txn)

        outcome = ReviewService(repos).override(
            allocation.id, user.id, "429", create_rule=True
        )

        assert outcome.rule is None
        assert "merchant name" in outcome.rule_blocked_reason

    def test_rejects_an_account_outside_the_chart(self, repos, agency, user):
        # The AI selects from the chart; it never adds to it. Nor does review.
        run_id, doc_id = make_statement(repos, agency.id)
        txn = add_transaction(repos, agency.id, doc_id, "SOMETHING NEW")
        allocation = add_allocation(repos, run_id, txn)

        with pytest.raises(ValueError, match="not in the chart of accounts"):
            ReviewService(repos).override(allocation.id, user.id, "12345")


class TestFeedback:
    def test_approval_confirms_the_rule_that_produced_it(self, repos, agency, user):
        rule = repos.rules.create(
            MerchantRule(client_id=agency.id, match_pattern="SINGTEL", account_id="489")
        )
        run_id, doc_id = make_statement(repos, agency.id)
        txn = add_transaction(repos, agency.id, doc_id, "GIRO PAYMENT SINGTEL 2891004")
        allocation = repos.allocations.create(
            Allocation(
                bank_transaction_id=txn.id, run_id=run_id, amount=txn.amount,
                account_id="489", tax_code="TX", decision_method=DecisionMethod.RULE,
                confidence=0.95, status=AllocationStatus.AUTO_POSTED,
                matched_rule_id=rule.id,
            )
        )

        ReviewService(repos).approve(allocation.id, user.id)

        assert repos.rules.list_active(agency.id)[0].confirm_count == 2

    def test_correcting_a_rule_driven_allocation_flags_the_rule(self, repos, agency, user):
        # A rule that starts producing corrections has gone stale. It is
        # flagged rather than silently overwritten - the accountant decides.
        rule = repos.rules.create(
            MerchantRule(client_id=agency.id, match_pattern="GRAB", account_id="493")
        )
        run_id, doc_id = make_statement(repos, agency.id)
        txn = add_transaction(repos, agency.id, doc_id, "GRABFOOD *ORDER 4471")
        allocation = repos.allocations.create(
            Allocation(
                bank_transaction_id=txn.id, run_id=run_id, amount=txn.amount,
                account_id="493", decision_method=DecisionMethod.RULE,
                confidence=0.95, status=AllocationStatus.AUTO_POSTED,
                matched_rule_id=rule.id,
            )
        )

        ReviewService(repos).override(allocation.id, user.id, "425")

        assert repos.rules.get(rule.id).is_stale is True

    def test_client_answer_becomes_a_durable_fact(self, repos, agency, user):
        run_id, doc_id = make_statement(repos, agency.id)
        txn = add_transaction(repos, agency.id, doc_id, "PAYNOW-TAN WEI MING")
        allocation = add_allocation(repos, run_id, txn, account=None,
                                    status=AllocationStatus.CLIENT_QUERY)

        ReviewService(repos).answer_query(
            allocation.id, user.id, "freelance illustrator, not a director", "310"
        )

        facts = repos.clients.get(agency.id).profile.learned_facts
        assert any("illustrator" in fact for fact in facts)


class TestClientScoping:
    def test_rules_never_cross_clients(self, repos, agency, restaurant, user):
        """GRAB is travel for an agency and a delivery cost for a restaurant.

        Sharing rules between clients would be actively wrong, not merely
        untidy.
        """
        run_id, doc_id = make_statement(repos, agency.id)
        txn = add_transaction(repos, agency.id, doc_id, "GRAB *TRIP 8829 SG")
        allocation = add_allocation(repos, run_id, txn)
        ReviewService(repos).override(allocation.id, user.id, "493", create_rule=True)

        assert len(repos.rules.list_active(agency.id)) == 1
        assert repos.rules.list_active(restaurant.id) == []

    def test_a_rule_does_not_fire_for_another_client(self, repos, agency, restaurant):
        repos.rules.create(
            MerchantRule(client_id=agency.id, match_pattern="GRAB *TRIP", account_id="493")
        )
        _, doc_id = make_statement(repos, restaurant.id)
        txn = add_transaction(repos, restaurant.id, doc_id, "GRAB *TRIP 5501 SG")

        assert find_rule(txn, repos.rules.list_active(restaurant.id)) is None


class TestSplits:
    def test_loan_repayment_splits_into_principal_and_interest(self, repos, agency, user):
        run_id, doc_id = make_statement(repos, agency.id)
        txn = add_transaction(
            repos, agency.id, doc_id, "LOAN REPAYMENT DBS 88291", amount="1000.00"
        )
        allocation = add_allocation(repos, run_id, txn, account="437")

        created = ReviewService(repos).split(
            allocation.id, user.id,
            [("900", Decimal("800.00")), ("437", Decimal("200.00"))],
            note="per loan schedule",
        )

        assert len(created) == 2
        assert sum(a.amount for a in created) == Decimal("1000.00")
        # A person's answer is not a probability.
        assert all(a.decision_method == DecisionMethod.HUMAN for a in created)
        assert all(a.confidence is None for a in created)

    def test_split_must_account_for_every_cent(self, repos, agency, user):
        run_id, doc_id = make_statement(repos, agency.id)
        txn = add_transaction(
            repos, agency.id, doc_id, "LOAN REPAYMENT DBS 88291", amount="1000.00"
        )
        allocation = add_allocation(repos, run_id, txn, account="437")

        with pytest.raises(ValueError, match="but the bank line is"):
            ReviewService(repos).split(
                allocation.id, user.id,
                [("900", Decimal("800.00")), ("437", Decimal("150.00"))],
            )

    def test_split_preserves_the_correction_history(self, repos, agency, user):
        # The record of who changed what has to outlive the row it describes.
        run_id, doc_id = make_statement(repos, agency.id)
        txn = add_transaction(repos, agency.id, doc_id, "LOAN REPAYMENT", amount="1000.00")
        allocation = add_allocation(repos, run_id, txn, account="437")

        ReviewService(repos).split(
            allocation.id, user.id,
            [("900", Decimal("800.00")), ("437", Decimal("200.00"))],
        )

        from sqlalchemy import func, select

        from aiacct.db.models import Correction

        count = repos.session.scalar(select(func.count()).select_from(Correction))
        assert count == 1

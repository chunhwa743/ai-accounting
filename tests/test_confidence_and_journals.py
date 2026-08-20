"""Scoring, routing, document matching, and double entry."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from aiacct.confidence import Signals, compute_confidence, is_opaque, route_allocation
from aiacct.export import build_journal, split_tax
from aiacct.matching import match_documents, score_match
from aiacct.models import AccountCandidate, AllocationStatus, ClientProfile, DecisionMethod, DocumentType, Legibility
from aiacct.db.models import Allocation, BankTransaction, Document, Run

PROFILE = ClientProfile(
    business_description="Design agency",
    gst_registered=True,
    own_bank_accounts=["003-88291-1"],
    materiality_threshold=Decimal("5000.00"),
)


def txn(description="GIRO PAYMENT SINGTEL", out="500.00", inn=None, legibility=None):
    return BankTransaction(
        id=1, document_id=1, client_id=1, line_no=1, txn_date=date(2026, 1, 5),
        raw_description=description,
        money_out=Decimal(out) if out else None,
        money_in=Decimal(inn) if inn else None,
        field_legibility=legibility or {},
    )


class TestConfidence:
    def test_a_learned_rule_scores_high_enough_to_post(self):
        score = compute_confidence(
            Signals(decision_method=DecisionMethod.RULE, rule_confirm_count=3)
        )
        assert score.value >= 0.90

    def test_repeated_confirmation_raises_confidence(self):
        fresh = compute_confidence(
            Signals(decision_method=DecisionMethod.RULE, rule_confirm_count=1)
        )
        seasoned = compute_confidence(
            Signals(decision_method=DecisionMethod.RULE, rule_confirm_count=6)
        )
        assert seasoned.value > fresh.value

    def test_human_answers_carry_no_confidence(self):
        # A person's answer is not a probability. Storing 1.0 would poison the
        # calibration curve, which is measured by excluding these rows.
        assert compute_confidence(
            Signals(decision_method=DecisionMethod.HUMAN)
        ).value == 0.0

    def test_a_close_runner_up_lowers_confidence(self):
        decisive = compute_confidence(Signals(
            decision_method=DecisionMethod.LLM,
            alternatives=[AccountCandidate(account_code="493", score=0.90),
                          AccountCandidate(account_code="425", score=0.30)],
        ))
        ambiguous = compute_confidence(Signals(
            decision_method=DecisionMethod.LLM,
            alternatives=[AccountCandidate(account_code="493", score=0.90),
                          AccountCandidate(account_code="425", score=0.88)],
        ))
        assert ambiguous.value < decisive.value

    def test_the_same_account_twice_is_not_ambiguity(self):
        # Two candidates naming one account is the same answer reached twice,
        # not a disagreement.
        duplicated = compute_confidence(Signals(
            decision_method=DecisionMethod.LLM,
            alternatives=[AccountCandidate(account_code="489", score=0.93),
                          AccountCandidate(account_code="489", score=0.90),
                          AccountCandidate(account_code="429", score=0.30)],
        ))
        single = compute_confidence(Signals(
            decision_method=DecisionMethod.LLM,
            alternatives=[AccountCandidate(account_code="489", score=0.93),
                          AccountCandidate(account_code="429", score=0.30)],
        ))
        assert duplicated.value == single.value

    def test_a_matched_document_raises_confidence(self):
        base = Signals(
            decision_method=DecisionMethod.LLM,
            alternatives=[AccountCandidate(account_code="453", score=0.80),
                          AccountCandidate(account_code="461", score=0.30)],
        )
        without = compute_confidence(base)
        with_doc = compute_confidence(Signals(
            decision_method=DecisionMethod.LLM,
            alternatives=base.alternatives,
            document_match_score=0.95, has_document=True,
        ))
        assert with_doc.value > without.value

    def test_a_guessed_description_lowers_confidence(self):
        # Phase 1 information the categorisation call never sees.
        clean = compute_confidence(Signals(
            decision_method=DecisionMethod.RULE, rule_confirm_count=3
        ))
        smudged = compute_confidence(Signals(
            decision_method=DecisionMethod.RULE, rule_confirm_count=3,
            description_legibility=Legibility.AMBIGUOUS,
        ))
        assert smudged.value < clean.value

    def test_an_unverifiable_statement_lowers_confidence(self):
        checked = compute_confidence(
            Signals(decision_method=DecisionMethod.RULE, rule_confirm_count=3, reconciles=True)
        )
        unchecked = compute_confidence(
            Signals(decision_method=DecisionMethod.RULE, rule_confirm_count=3, reconciles=None)
        )
        failed = compute_confidence(
            Signals(decision_method=DecisionMethod.RULE, rule_confirm_count=3, reconciles=False)
        )
        assert failed.value < unchecked.value < checked.value

    def test_a_missing_invoice_does_not_penalise_a_learned_rule(self):
        # The accountant already decided how this merchant is coded, knowing
        # no paperwork arrives for it.
        rule = compute_confidence(Signals(
            decision_method=DecisionMethod.RULE, rule_confirm_count=3,
            amount=Decimal("3500.00"), has_document=False,
        ))
        assert rule.value >= 0.90


class TestRouting:
    def _route(self, **kwargs):
        defaults = dict(
            account_code="489", tax_code="TX", confidence=0.95, txn=txn(),
            profile=PROFILE, decision_method=DecisionMethod.LLM,
        )
        return route_allocation(**{**defaults, **kwargs})

    def test_high_confidence_posts_automatically(self):
        assert self._route().status == AllocationStatus.AUTO_POSTED

    def test_a_high_risk_account_is_reviewed_however_confident(self):
        # Confidence and consequence are independent: a model can be sure a
        # large payment is drawings and be wrong, and drawings change the tax
        # computation.
        routing = self._route(account_code="980", confidence=0.99)
        assert routing.status == AllocationStatus.NEEDS_REVIEW
        assert routing.gated

    def test_blocked_input_tax_is_reviewed(self):
        routing = self._route(account_code="483", tax_code="BL", confidence=0.99)
        assert routing.status == AllocationStatus.NEEDS_REVIEW

    def test_reverse_charge_is_reviewed(self):
        routing = self._route(account_code="463", tax_code="TX-RC", confidence=0.99)
        assert routing.status == AllocationStatus.NEEDS_REVIEW

    def test_an_amount_over_materiality_is_reviewed(self):
        routing = self._route(txn=txn(out="8000.00"), confidence=0.99)
        assert routing.status == AllocationStatus.NEEDS_REVIEW

    def test_an_opaque_description_goes_to_the_client(self):
        """The model reports it could not identify anything; code acts on that.

        Deliberately not decided by a list of payment-rail words: that would
        need a new entry for every bank, rail and language, and would fail
        silently once it fell behind.
        """
        routing = self._route(
            txn=txn(description="TRF TO 8891234"), confidence=0.95, identifiable=False
        )
        assert routing.status == AllocationStatus.CLIENT_QUERY
        assert routing.gated

    def test_a_confident_model_can_still_be_overruled_by_its_own_observation(self):
        # It named an account, but also said the description identifies
        # nothing. The observation wins.
        routing = self._route(account_code="429", confidence=0.99, identifiable=False)
        assert routing.status == AllocationStatus.CLIENT_QUERY

    def test_an_identified_transaction_posts_even_without_a_merchant_name(self):
        # "SERVICE CHARGE" names nobody but is a recognisable kind of
        # transaction, which a vocabulary-based check got wrong.
        routing = self._route(
            txn=txn(description="SERVICE CHARGE"), account_code="404",
            tax_code="EP", confidence=0.95, identifiable=True,
        )
        assert routing.status == AllocationStatus.AUTO_POSTED

    def test_an_unresolved_allocation_goes_to_the_client(self):
        routing = self._route(account_code=None, confidence=0.80)
        assert routing.status == AllocationStatus.CLIENT_QUERY

    def test_a_split_is_always_reviewed(self):
        routing = self._route(needs_split=True, confidence=0.99)
        assert routing.status == AllocationStatus.NEEDS_REVIEW

    def test_a_possible_duplicate_is_reviewed(self):
        routing = self._route(is_possible_duplicate=True, confidence=0.99)
        assert routing.status == AllocationStatus.NEEDS_REVIEW

    def test_low_confidence_goes_to_the_client(self):
        assert self._route(confidence=0.30).status == AllocationStatus.CLIENT_QUERY

    @pytest.mark.parametrize("identifiable,opaque", [(True, False), (False, True)])
    def test_the_model_observation_decides(self, identifiable, opaque):
        assert is_opaque("TRF 8891234", identifiable) is opaque

    @pytest.mark.parametrize(
        "description,opaque",
        [
            ("8891234", True),        # no letters at all: names nobody
            ("   ", True),
            ("", True),
            ("TRF 8891234", False),   # has letters; only the model can judge further
            ("转账 8891234", False),  # non-Latin script must not be called opaque
        ],
    )
    def test_local_floor_without_an_observation(self, description, opaque):
        """The only claim made locally: no letters means no counterparty.

        That holds in any language and any bank format, which a list of English
        payment words does not.
        """
        assert is_opaque(description) is opaque


class TestDocumentMatching:
    def _invoice(self, doc_id, vendor, total, doc_date):
        return Document(
            id=doc_id, client_id=1, document_type=DocumentType.INVOICE,
            original_filename="inv.pdf", storage_uri="/tmp/inv.pdf",
            mime_type="application/pdf", file_hash=f"h{doc_id}",
            vendor_name=vendor, total_amount=Decimal(total), doc_date=doc_date,
            tax_amount=Decimal("90.00"), summary="Dell laptop",
        )

    def test_vendor_name_breaks_an_amount_tie(self):
        """Two payments of the same amount in the same week.

        Amount alone cannot separate them, which is why matching needs three
        signals rather than one.
        """
        invoice = self._invoice(7, "Acme Supplies Pte Ltd", "1090.00", date(2026, 1, 8))
        right = BankTransaction(
            id=1, document_id=1, client_id=1, line_no=1, txn_date=date(2026, 1, 10),
            raw_description="PAYNOW-ACME SUPPLIES PTE LTD-88291",
            money_out=Decimal("1090.00"),
        )
        wrong = BankTransaction(
            id=2, document_id=1, client_id=1, line_no=2, txn_date=date(2026, 1, 10),
            raw_description="NETS CHALLENGER TECH", money_out=Decimal("1090.00"),
        )

        assert score_match(right, invoice).score > score_match(wrong, invoice).score

    def test_one_document_is_claimed_by_one_transaction(self):
        invoice = self._invoice(7, "Acme Supplies Pte Ltd", "1090.00", date(2026, 1, 8))
        transactions = [
            BankTransaction(id=1, document_id=1, client_id=1, line_no=1,
                            txn_date=date(2026, 1, 10),
                            raw_description="PAYNOW-ACME SUPPLIES PTE LTD-88291",
                            money_out=Decimal("1090.00")),
            BankTransaction(id=2, document_id=1, client_id=1, line_no=2,
                            txn_date=date(2026, 1, 12),
                            raw_description="PAYNOW-ACME SUPPLIES PTE LTD-91002",
                            money_out=Decimal("1090.00")),
        ]

        matched = match_documents(transactions, [invoice])

        assert len(matched) == 1

    def test_a_wrong_amount_does_not_match(self):
        invoice = self._invoice(7, "Acme Supplies Pte Ltd", "1090.00", date(2026, 1, 8))
        transaction = BankTransaction(
            id=1, document_id=1, client_id=1, line_no=1, txn_date=date(2026, 1, 10),
            raw_description="PAYNOW-ACME SUPPLIES PTE LTD-88291",
            money_out=Decimal("264.00"),
        )
        assert match_documents([transaction], [invoice]) == {}


class TestTaxSplit:
    def test_gst_is_separated_from_a_gross_amount(self):
        net, tax = split_tax(Decimal("500.00"), "TX")
        assert net == Decimal("458.72")
        assert tax == Decimal("41.28")
        assert net + tax == Decimal("500.00")

    def test_blocked_input_tax_stays_in_the_expense(self):
        # GST on medical, private car and club costs cannot be reclaimed, so
        # treating it as a receivable would overstate assets.
        net, tax = split_tax(Decimal("156.00"), "BL")
        assert net == Decimal("156.00")
        assert tax == Decimal("0.00")

    def test_wages_carry_no_gst(self):
        net, tax = split_tax(Decimal("18600.00"), "OP")
        assert tax == Decimal("0.00")


class TestJournals:
    def _prepare(self, repos, client_id, description, out=None, inn=None,
                 account="489", tax="TX"):
        run = repos.runs.create(Run(client_id=client_id))
        doc = repos.documents.create(
            Document(client_id=client_id, document_type=DocumentType.BANK_STATEMENT,
                     original_filename="s.pdf", storage_uri="/tmp/s.pdf",
                     mime_type="application/pdf", file_hash=f"h{client_id}{description[:4]}"),
            run_id=run.id,
        )
        transaction = repos.transactions.bulk_create([
            BankTransaction(
                document_id=doc.id, client_id=client_id, line_no=1,
                txn_date=date(2026, 1, 5), raw_description=description,
                money_out=Decimal(out) if out else None,
                money_in=Decimal(inn) if inn else None,
            )
        ])[0]
        repos.allocations.create(Allocation(
            bank_transaction_id=transaction.id, run_id=run.id,
            amount=transaction.amount, account_id=account, tax_code=tax,
            decision_method=DecisionMethod.LLM, confidence=0.95,
            status=AllocationStatus.AUTO_POSTED,
        ))
        return run.id

    def test_a_purchase_balances_with_gst_split_out(self, repos, agency):
        run_id = self._prepare(repos, agency.id, "GIRO PAYMENT SINGTEL", out="500.00")
        entry = build_journal(repos, run_id)[0]

        assert entry.balances
        assert entry.total_debit == Decimal("500.00")
        by_account = {line.account_code: line for line in entry.lines}
        assert by_account["489"].debit == Decimal("458.72")
        assert by_account["820"].debit == Decimal("41.28")
        # Money out credits the bank asset - the mirror of how the bank prints it.
        assert by_account["090"].credit == Decimal("500.00")

    def test_a_sale_balances_the_other_way(self, repos, agency):
        run_id = self._prepare(
            repos, agency.id, "PAYMENT RECEIVED", inn="2180.00", account="200", tax="SR"
        )
        entry = build_journal(repos, run_id)[0]

        assert entry.balances
        by_account = {line.account_code: line for line in entry.lines}
        assert by_account["200"].credit == Decimal("2000.00")
        assert by_account["820"].credit == Decimal("180.00")
        # Money in debits the bank asset.
        assert by_account["090"].debit == Decimal("2180.00")

    def test_unresolved_allocations_are_not_posted(self, repos, agency):
        # An entry that cannot be explained should not reach the ledger at all,
        # rather than being dumped into suspense.
        run = repos.runs.create(Run(client_id=agency.id))
        doc = repos.documents.create(
            Document(client_id=agency.id, document_type=DocumentType.BANK_STATEMENT,
                     original_filename="s.pdf", storage_uri="/tmp/s.pdf",
                     mime_type="application/pdf", file_hash="unresolved"),
            run_id=run.id,
        )
        transaction = repos.transactions.bulk_create([
            BankTransaction(document_id=doc.id, client_id=agency.id, line_no=1,
                            txn_date=date(2026, 1, 5), raw_description="TRF 8891234",
                            money_out=Decimal("780.00"))
        ])[0]
        repos.allocations.create(Allocation(
            bank_transaction_id=transaction.id, run_id=run.id,
            amount=transaction.amount, account_id=None, tax_code=None,
            decision_method=DecisionMethod.LLM, confidence=0.20,
            status=AllocationStatus.CLIENT_QUERY,
        ))

        assert build_journal(repos, run.id) == []

    def test_every_entry_balances(self, repos, agency):
        for description, out, inn, account, tax in [
            ("GIRO RENT", "4800.00", None, "469", "TX"),
            ("SALARY PAYMENT", "18600.00", None, "477", "OP"),
            ("RAFFLES MEDICAL", "156.00", None, "483", "BL"),
            ("CREDIT INTEREST", None, "24.80", "260", "EP"),
        ]:
            run_id = self._prepare(repos, agency.id, description, out, inn, account, tax)
            for entry in build_journal(repos, run_id):
                assert entry.balances, f"{description} does not balance"

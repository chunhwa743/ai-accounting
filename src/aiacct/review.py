"""The human loop: approving, correcting, and learning from both.

Two things happen here that the rest of the system depends on.

First, the distinction between an approval and a correction. Approving records
that a person looked and agreed; correcting records that they disagreed and
what they changed it to. Both are evidence - five approvals of a rule-driven
allocation say the rule is right - and without the approval half you cannot
tell "reviewed and correct" from "nobody has looked yet".

Second, rule creation. When an accountant says "always do this", the model is
asked once what pattern the rule should match, the accountant is shown what it
would have captured, and only then is it saved. That is what keeps a rule for
"GRAB *TRIP" from swallowing "GRABFOOD", without anyone maintaining a regex.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal

from .categorisation import preview_rule, should_create_rule
from .db import Repositories
from .llm import LLMClient
from .models import (
    AllocationStatus,
    CorrectionType,
    DecisionMethod,
    MatchPatternProposal,
    MatchType,
)
from .db.models import (
    Allocation,
    BankTransaction,
    Correction,
    MerchantRule,
)
from .reference import get_chart_of_accounts, resolve_tax_code

log = logging.getLogger(__name__)


PATTERN_PROMPT = """\
An accountant has just corrected how a bank transaction is coded, and asked for
the same treatment to be applied automatically in future.

Propose the substring that should trigger that rule.

<description>
{description}
</description>

Other descriptions from this same client, for context:
{context}

Rules:
  * The pattern must appear verbatim in the description above.
  * Make it specific enough not to capture a different kind of transaction from
    the same merchant family. "GRAB *TRIP" and "GRABFOOD" are different
    accounts, so "GRAB" alone would be wrong.
  * Strip anything that varies between transactions: trip numbers, reference
    ids, card fragments, dates, amounts.
  * Prefer the merchant's recognisable name over payment-rail noise such as
    GIRO, PAYNOW, NETS or VISA.
"""


@dataclass
class ReviewOutcome:
    allocation: Allocation
    correction: Correction | None = None
    rule: MerchantRule | None = None
    rule_preview: list[BankTransaction] | None = None
    rule_blocked_reason: str | None = None
    message: str = ""


class ReviewService:
    def __init__(self, repos: Repositories, llm: LLMClient | None = None) -> None:
        self.repos = repos
        self.llm = llm

    # ------------------------------------------------------------- approve

    def approve(
        self, allocation_id: int, user_id: int, create_rule: bool = False
    ) -> ReviewOutcome:
        """Accept the proposed coding unchanged.

        Writes no Correction - nothing changed - but an approval on a
        rule-driven allocation is evidence the rule is working, so its
        confirmation count goes up and future confidence with it.
        """
        allocation = self.repos.allocations.get(allocation_id)
        if allocation is None:
            raise ValueError(f"no allocation {allocation_id}")
        if allocation.account_id is None:
            raise ValueError(
                "this allocation has no account yet, so there is nothing to "
                "approve. Set an account, or leave it as a client query."
            )

        self.repos.allocations.approve(allocation_id, user_id)

        if allocation.matched_rule_id:
            self.repos.rules.confirm(allocation.matched_rule_id)

        outcome = ReviewOutcome(
            allocation=self.repos.allocations.get(allocation_id),
            message="approved as proposed",
        )

        # "Yes, this is right - and stop asking me about it." A reviewer
        # confirming a flagged item is the most common way a rule gets created,
        # more so than an outright correction: the answer was already right, it
        # just was not certain enough to post on its own.
        if create_rule and allocation.matched_rule_id is None:
            txn = self.repos.transactions.get(allocation.bank_transaction_id)
            self._create_rule(
                outcome, txn, allocation.account_id, allocation.tax_code, None
            )

        return outcome

    # ------------------------------------------------------------ override

    def override(
        self,
        allocation_id: int,
        user_id: int,
        account_code: str,
        tax_code: str | None = None,
        note: str | None = None,
        create_rule: bool = False,
    ) -> ReviewOutcome:
        allocation = self.repos.allocations.get(allocation_id)
        if allocation is None:
            raise ValueError(f"no allocation {allocation_id}")

        coa = get_chart_of_accounts()
        if not coa.exists(account_code):
            raise ValueError(
                f"{account_code} is not in the chart of accounts. Accounts are "
                f"added deliberately, not created during review."
            )

        txn = self.repos.transactions.get(allocation.bank_transaction_id)
        client = self.repos.clients.get(txn.client_id)
        tax_code = tax_code or resolve_tax_code(account_code, client.profile.gst_registered)

        # Read what the allocation was before changing it. It is a live ORM
        # object, so applying the override rewrites these in place - and the
        # question "did this come from a rule?" can only be asked of the state
        # that existed beforehand.
        was_from_rule = (
            allocation.matched_rule_id is not None
            and allocation.decision_method == DecisionMethod.RULE
        )
        previous_rule_id = allocation.matched_rule_id
        previous_account = allocation.account_id
        previous_tax_code = allocation.tax_code
        previous_confidence = allocation.confidence

        correction = self.repos.corrections.create(
            Correction(
                allocation_id=allocation_id,
                corrected_by=user_id,
                correction_type=CorrectionType.CATEGORISATION,
                field_name="account_id",
                old_value=previous_account,
                new_value=account_code,
                from_account_id=previous_account,
                to_account_id=account_code,
                from_tax_code=previous_tax_code,
                to_tax_code=tax_code,
                # Kept so correction rate can be measured per confidence band,
                # which is the only way to know whether the score means anything.
                from_confidence=previous_confidence,
                note=note,
                create_rule=create_rule,
            )
        )

        self.repos.allocations.apply_override(
            allocation_id, account_code, tax_code, user_id, note
        )

        # A correction landing on a rule-driven allocation means the rule has
        # stopped being right. Flag it rather than silently overwriting: the
        # accountant decides whether the client's habits changed.
        if was_from_rule:
            self.repos.rules.mark_stale(previous_rule_id)
            log.info("rule %s flagged stale after a correction", previous_rule_id)

        outcome = ReviewOutcome(
            allocation=self.repos.allocations.get(allocation_id),
            correction=correction,
            message=f"recoded to {account_code}",
        )

        if create_rule:
            self._create_rule(outcome, txn, account_code, tax_code, correction.id)

        return outcome

    # -------------------------------------------------------------- rules

    def _create_rule(
        self,
        outcome: ReviewOutcome,
        txn: BankTransaction,
        account_code: str,
        tax_code: str | None,
        correction_id: int | None,
    ) -> None:
        allowed, reason = should_create_rule(txn)
        if not allowed:
            # The allocation still stands - a person approved it - but a rule
            # keyed on a half-read merchant name would miscode silently every
            # month until somebody noticed.
            outcome.rule_blocked_reason = reason
            outcome.message += f"; no rule created because {reason}"
            return

        pattern = self._propose_pattern(txn)
        if pattern is None:
            outcome.rule_blocked_reason = "could not derive a safe pattern"
            return

        # A pattern with no merchant name in it - "8891234" - would either
        # match nothing next month or match something unrelated. Opaque
        # descriptions are for asking the client about, not for learning from.
        if not re.search(r"[A-Za-z]{3,}", pattern):
            outcome.rule_blocked_reason = (
                f"{pattern!r} contains no merchant name, so it would not "
                f"reliably identify anything next month"
            )
            outcome.message += f"; no rule created because {outcome.rule_blocked_reason}"
            return

        history = self.repos.transactions.list_for_client_history(txn.client_id)
        outcome.rule_preview = preview_rule(pattern, history)

        rule = self.repos.rules.create(
            MerchantRule(
                client_id=txn.client_id,
                match_pattern=pattern,
                match_type=MatchType.CONTAINS,
                account_id=account_code,
                tax_code=tax_code,
                created_from_correction_id=correction_id,
            )
        )
        outcome.rule = rule
        outcome.message += (
            f"; learned the rule {pattern!r}, which matches "
            f"{len(outcome.rule_preview)} transaction(s) on record"
        )

    def _propose_pattern(self, txn: BankTransaction) -> str | None:
        """Ask the model what the rule should match.

        Doing this once, at correction time, is what removes the need for a
        hand-written normaliser - and it is the step that knows "GRAB *TRIP"
        must not be shortened to "GRAB".
        """
        if self.llm is None:
            return _fallback_pattern(txn.raw_description)

        history = self.repos.transactions.list_for_client_history(txn.client_id, limit=20)
        context = "\n".join(f"  {t.raw_description}" for t in history) or "  (none)"

        try:
            result = self.llm.parse(
                prompt=PATTERN_PROMPT.format(
                    description=txn.raw_description, context=context
                ),
                schema=MatchPatternProposal,
                effort="low",
            )
            pattern = result.parsed.match_pattern.strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("pattern proposal failed (%s); using a local fallback", exc)
            return _fallback_pattern(txn.raw_description)

        # The model must not invent a pattern that is not in the description,
        # or the rule would never fire.
        if pattern and pattern.upper() in txn.raw_description.upper():
            return pattern
        log.warning(
            "proposed pattern %r is not present in %r; using a local fallback",
            pattern, txn.raw_description,
        )
        return _fallback_pattern(txn.raw_description)

    # ------------------------------------------------------------- splits

    def split(
        self,
        allocation_id: int,
        user_id: int,
        parts: list[tuple[str, Decimal]],
        note: str | None = None,
    ) -> list[Allocation]:
        """Replace one allocation with several that sum to the same amount.

        This is how a loan repayment gets recorded properly: part reduces the
        liability, part is interest. The ratio comes from the loan schedule, so
        it can only be entered by a person.
        """
        allocation = self.repos.allocations.get(allocation_id)
        if allocation is None:
            raise ValueError(f"no allocation {allocation_id}")

        total = sum((amount for _, amount in parts), Decimal("0"))
        if abs(total - allocation.amount) > Decimal("0.01"):
            raise ValueError(
                f"the parts total {total} but the bank line is {allocation.amount}. "
                f"Every cent of a transaction has to be accounted for."
            )

        coa = get_chart_of_accounts()
        txn = self.repos.transactions.get(allocation.bank_transaction_id)
        client = self.repos.clients.get(txn.client_id)

        replacements = []
        for account_code, amount in parts:
            if not coa.exists(account_code):
                raise ValueError(f"{account_code} is not in the chart of accounts")
            replacements.append(
                Allocation(
                    bank_transaction_id=allocation.bank_transaction_id,
                    run_id=allocation.run_id,
                    amount=amount,
                    account_id=account_code,
                    tax_code=resolve_tax_code(account_code, client.profile.gst_registered),
                    decision_method=DecisionMethod.HUMAN,
                    confidence=None,
                    status=AllocationStatus.APPROVED,
                    reasoning=note or "split entered during review",
                    matched_document_id=allocation.matched_document_id,
                    approved_by=user_id,
                )
            )

        self.repos.corrections.create(
            Correction(
                allocation_id=allocation_id,
                corrected_by=user_id,
                correction_type=CorrectionType.CATEGORISATION,
                field_name="split",
                old_value=str(allocation.account_id),
                new_value=", ".join(f"{a}:{m}" for a, m in parts),
                from_account_id=allocation.account_id,
                from_confidence=allocation.confidence,
                note=note,
            )
        )

        created = self.repos.allocations.replace_for_transaction(
            allocation.bank_transaction_id, replacements
        )
        for row in created:
            self.repos.allocations.approve(row.id, user_id)
        return created

    # ------------------------------------------------------- clarification

    def answer_query(
        self, allocation_id: int, user_id: int, answer: str, account_code: str | None = None
    ) -> ReviewOutcome:
        """Record a client's reply.

        The answer becomes a durable fact on the client profile, so the next
        run has the context this one lacked. That is the third tier of memory -
        neither a rule nor an example, but something true about the client.
        """
        allocation = self.repos.allocations.get(allocation_id)
        if allocation is None:
            raise ValueError(f"no allocation {allocation_id}")

        txn = self.repos.transactions.get(allocation.bank_transaction_id)
        self.repos.clients.add_learned_fact(
            txn.client_id, f"{txn.raw_description}: {answer}"
        )

        if account_code:
            return self.override(
                allocation_id, user_id, account_code,
                note=f"client confirmed: {answer}",
            )

        return ReviewOutcome(
            allocation=allocation,
            message="answer recorded on the client profile",
        )

    # ------------------------------------------------------ extraction fix

    def correct_extraction(
        self,
        user_id: int,
        field_name: str,
        new_value: str,
        document_id: int | None = None,
        transaction_id: int | None = None,
    ) -> Correction:
        """Supply a value the machine could not read.

        Not a model limitation being patched - a person doing something the
        image genuinely does not support, using context it does not contain.
        These corrections feed the audit trail and an extraction quality
        metric; unlike a categorisation correction there is nothing to learn,
        because you cannot generalise "read this smudge as a 9".
        """
        if (document_id is None) == (transaction_id is None):
            raise ValueError("supply exactly one of document_id or transaction_id")

        if document_id is not None:
            document = self.repos.documents.get(document_id)
            old_value = getattr(document, field_name, None)
            self.repos.documents.set_field(document_id, field_name, new_value)
            # The field is now known, so it is no longer a reason to stop. A
            # person supplied what the image could not, using context the image
            # does not contain.
            self.repos.documents.clear_legibility(document_id, field_name)
        else:
            txn = self.repos.transactions.get(transaction_id)
            old_value = getattr(txn, field_name, None)
            self.repos.transactions.set_field(transaction_id, field_name, new_value)
            self.repos.transactions.clear_legibility(transaction_id, field_name)

        return self.repos.corrections.create(
            Correction(
                bank_transaction_id=transaction_id,
                corrected_by=user_id,
                correction_type=CorrectionType.EXTRACTION,
                field_name=field_name,
                old_value=str(old_value) if old_value is not None else None,
                new_value=new_value,
            ),
            document_id=document_id,
        )


RAIL_PREFIXES = (
    "GIRO PAYMENT ", "GIRO ", "PAYNOW-", "PAYNOW ", "NETS QR PAYMENT ",
    "NETS QR ", "NETS ", "VISA ", "FAST ", "TRF TO ", "TRF ",
)

# Anything that changes between transactions: trip ids, reference numbers,
# card fragments, dates. A pattern containing one of these would match once and
# never again.
_VARIABLE_TOKEN = re.compile(r"^[#*]?\d[\w\-/]*$|^\d")


def _fallback_pattern(description: str) -> str:
    """Derive a rule pattern locally, when no model is available.

    Slices the *original* string rather than rebuilding it from tokens, so
    punctuation survives: "GRABFOOD *ORDER 4471" has to keep its asterisk or
    the pattern would not appear in the description it came from.
    """
    text = description.strip()
    upper = text.upper()

    offset = 0
    for prefix in RAIL_PREFIXES:
        if upper.startswith(prefix):
            offset = len(prefix)
            break

    remainder = text[offset:]
    words = remainder.split()
    if not words:
        return text[:24].strip()

    # Keep leading words until one looks like an identifier, capped at two so
    # "GRAB *TRIP" stays distinct from "GRABFOOD" without becoming so specific
    # that it only matches this one transaction.
    kept = []
    for word in words[:2]:
        if _VARIABLE_TOKEN.match(word):
            break
        kept.append(word)
    if not kept:
        kept = words[:1]

    # Slice the original so the pattern is a genuine substring of it.
    end = remainder.find(kept[-1]) + len(kept[-1])
    return remainder[:end].strip()

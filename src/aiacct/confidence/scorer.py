"""How sure we are, and who therefore has to look at it.

The score is computed here, by code. The categorisation call never returns one,
for three reasons:

  * Most transactions never reach that call - they are resolved by a learned
    rule - and every allocation still needs a confidence.
  * Two of the penalties come from extraction facts the call never sees, and
    should not: whether the description was partly guessed, and whether the
    statement's arithmetic verified.
  * Self-reported floats are uncalibrated. Models cluster near 0.9 regardless
    of input, so routing on one would auto-post nearly everything, wrong
    answers included, and the whole mechanism would be decorative.

What the model does contribute honestly is its ranked alternatives. The gap
between first and second choice is an observation about how close the call was.

Hard gates then override the score entirely, because confidence and consequence
are independent: a model can be very sure that a large transfer is director's
drawings and be wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from ..models import (
    AccountCandidate,
    AllocationStatus,
    ClientProfile,
    DecisionMethod,
    Legibility,
)
from ..db.models import (
    BankTransaction,
)
from ..reference import get_chart_of_accounts, get_confidence_config, get_tax_codes

# A description with no letters at all cannot name anybody, in any language or
# any bank's format. That is the only claim about description text this module
# makes: everything subtler is an observation the model reports, because a
# maintained vocabulary of payment-rail words would need extending for every
# new bank, rail and language, and would fail silently when it fell behind.
_ANY_LETTER = re.compile(r"[^\W\d_]")


@dataclass
class Signals:
    """Everything feeding the score, kept explicit so it can be shown to a
    reviewer and replayed in the evaluation harness."""

    decision_method: DecisionMethod
    alternatives: list[AccountCandidate] = field(default_factory=list)
    rule_confirm_count: int = 0
    document_match_score: float | None = None
    description_legibility: Legibility | None = None
    reconciles: bool | None = True
    amount: Decimal = Decimal("0")
    has_document: bool = False


@dataclass
class Score:
    value: float
    components: dict[str, float]

    def explain(self) -> str:
        parts = [f"{name} {delta:+.2f}" for name, delta in self.components.items()]
        return f"{self.value:.2f} = " + ", ".join(parts)


def compute_confidence(signals: Signals) -> Score:
    config = get_confidence_config()
    components: dict[str, float] = {}

    if signals.decision_method == DecisionMethod.HUMAN:
        # A person's answer is not a probability.
        return Score(value=0.0, components={"human": 0.0})

    if signals.decision_method == DecisionMethod.RULE:
        base = float(config["base"]["RULE"])
        components["learned rule"] = base
        bonus_cfg = config["rule_confirmation_bonus"]
        bonus = min(
            float(bonus_cfg["max"]),
            float(bonus_cfg["per_confirmation"]) * max(0, signals.rule_confirm_count - 1),
        )
        if bonus:
            components[f"confirmed {signals.rule_confirm_count}x"] = bonus
        score = base + bonus
    else:
        llm_cfg = config["llm"]
        base = float(config["base"]["LLM"])
        components["model"] = base
        score = base

        # Two candidates naming the same account are not a disagreement - they
        # are the same answer reached twice, which if anything is corroboration.
        # Only distinct accounts can make a call ambiguous.
        seen: set[str] = set()
        alternatives = []
        for candidate in signals.alternatives:
            if candidate.account_code in seen:
                continue
            seen.add(candidate.account_code)
            alternatives.append(candidate)

        if alternatives:
            top = alternatives[0].score
            contribution = float(llm_cfg["top_score_weight"]) * top
            components["top candidate"] = contribution
            score += contribution

            if len(alternatives) > 1:
                # A close second choice is the honest uncertainty signal: the
                # model saw two plausible answers and picked one. A wide margin
                # is a decisive call and costs nothing.
                margin = top - alternatives[1].score
                threshold = float(llm_cfg["ambiguity_margin_threshold"])
                closeness = max(0.0, (threshold - margin) / threshold)
                penalty = float(llm_cfg["ambiguity_penalty_max"]) * closeness
                if penalty > 0.001:
                    components[f"close runner-up (margin {margin:.2f})"] = -penalty
                    score -= penalty

    # A matched invoice corroborates the coding and supplies the GST figure.
    doc_cfg = config["document_match"]
    if signals.document_match_score is not None:
        if signals.document_match_score >= float(doc_cfg["min_score_to_count"]):
            boost = float(doc_cfg["boost_at_full_score"]) * signals.document_match_score
            components["supporting document"] = boost
            score += boost

    penalties = config["penalties"]

    # From phase 1: the description was partly guessed off a poor scan. The
    # categorisation call never knew this.
    legibility_penalty = {
        Legibility.INFERRED: float(penalties["legibility_inferred"]),
        Legibility.AMBIGUOUS: float(penalties["legibility_ambiguous"]),
        Legibility.UNREADABLE: float(penalties["legibility_unreadable"]),
    }.get(signals.description_legibility)
    if legibility_penalty:
        components[f"description {signals.description_legibility}"] = -legibility_penalty
        score -= legibility_penalty

    # Also from phase 1: the statement did not reconcile, or could not be
    # checked at all.
    if signals.reconciles is False:
        value = float(penalties["reconciles_false"])
        components["statement did not reconcile"] = -value
        score -= value
    elif signals.reconciles is None:
        value = float(penalties["reconciles_null"])
        components["reconciliation unverifiable"] = -value
        score -= value

    # A missing invoice is a reason to question the model's guess, not a reason
    # to doubt a rule: the accountant already decided how this merchant is
    # coded, and did so knowing no paperwork arrives for it.
    if (
        signals.decision_method != DecisionMethod.RULE
        and not signals.has_document
        and signals.amount >= Decimal(str(penalties["missing_document_above"]))
    ):
        value = float(penalties["missing_document"])
        components["no supporting document"] = -value
        score -= value

    return Score(value=max(0.0, min(1.0, round(score, 3))), components=components)


# ---------------------------------------------------------------- routing


@dataclass
class Routing:
    status: AllocationStatus
    reason: str
    gated: bool = False


def is_opaque(description: str, identifiable: bool | None = None) -> bool:
    """True when there is nothing in the description to reason from.

    ``identifiable`` is the model's own judgement, reported alongside the
    account it chose. It is trusted when present: deciding whether a string
    names a counterparty is a language question, and the alternative is a list
    of payment-rail words that needs a new entry for every bank and language
    and goes wrong quietly when it does not get one.

    The local check is only the floor - a description containing no letters at
    all names nobody, which holds regardless of format or language.
    """
    if identifiable is not None:
        return not identifiable
    return not _ANY_LETTER.search(description or "")


def route_allocation(
    *,
    account_code: str | None,
    tax_code: str | None,
    confidence: float,
    txn: BankTransaction,
    profile: ClientProfile,
    decision_method: DecisionMethod,
    is_possible_duplicate: bool = False,
    needs_split: bool = False,
    identifiable: bool | None = None,
) -> Routing:
    """Decide who has to look at this.

    Gates are checked before bands and cannot be outscored.
    """
    config = get_confidence_config()
    gates = config["gates"]

    if decision_method == DecisionMethod.HUMAN:
        return Routing(AllocationStatus.APPROVED, "set by a person")

    if account_code is None:
        return Routing(
            AllocationStatus.CLIENT_QUERY,
            "no account could be determined",
            gated=True,
        )

    if is_opaque(txn.raw_description, identifiable):
        return Routing(
            AllocationStatus.CLIENT_QUERY,
            "the description identifies neither a counterparty nor a "
            "recognisable kind of transaction, so there is nothing to reason from",
            gated=True,
        )

    if needs_split:
        return Routing(
            AllocationStatus.NEEDS_REVIEW,
            "one payment covering more than one account; the split has to be "
            "entered by a person",
            gated=True,
        )

    if get_chart_of_accounts().is_high_risk(account_code):
        account = get_chart_of_accounts().get(account_code)
        return Routing(
            AllocationStatus.NEEDS_REVIEW,
            f"{account.code} {account.name} is high risk: an error here changes "
            f"the tax computation or the balance sheet",
            gated=True,
        )

    if tax_code and get_tax_codes().requires_review(tax_code):
        return Routing(
            AllocationStatus.NEEDS_REVIEW,
            f"tax code {tax_code} is a claimability or filing decision with a "
            f"direct cash consequence",
            gated=True,
        )

    if is_possible_duplicate:
        return Routing(
            AllocationStatus.NEEDS_REVIEW,
            "same date, amount and description as another line; could be a "
            "second order or a double payment",
            gated=True,
        )

    ceiling = Decimal(str(gates["amount_above"]))
    if txn.amount >= min(ceiling, profile.materiality_threshold):
        return Routing(
            AllocationStatus.NEEDS_REVIEW,
            f"SGD {txn.amount} is above this client's materiality threshold",
            gated=True,
        )

    bands = config["bands"]
    if confidence >= float(bands["auto_post_at"]):
        return Routing(AllocationStatus.AUTO_POSTED, f"confidence {confidence:.2f}")
    if confidence >= float(bands["review_at"]):
        return Routing(AllocationStatus.NEEDS_REVIEW, f"confidence {confidence:.2f}")
    return Routing(AllocationStatus.CLIENT_QUERY, f"confidence {confidence:.2f} is too low")

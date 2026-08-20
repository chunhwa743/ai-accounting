"""What the accountant has taught the system about a client.

Three tiers, all scoped by client. "GRAB" means travel for a design agency and
a delivery cost for a restaurant, so a rule learned for one must never reach
the other.

  1. MerchantRule       an exact, deterministic answer the accountant confirmed
  2. past corrections   few-shot examples for the model, for cases no pattern
                        captures
  3. learned facts      durable context from answered clarifications

Tier 1 exists because retrieval alone is not enough. After a year a client has
hundreds of corrections and only a handful fit in a prompt, so most would
silently stop applying while the accountant believes the system learned them.
A rule applies every time until it is deleted, and can be shown as a list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rapidfuzz import fuzz

from ..config import get_settings
from ..models import MatchType
from ..db.models import BankTransaction, MerchantRule

log = logging.getLogger(__name__)


@dataclass
class RuleHit:
    rule: MerchantRule
    matched_text: str

    def explain(self) -> str:
        confirmations = (
            "confirmed once" if self.rule.confirm_count == 1
            else f"confirmed {self.rule.confirm_count} times"
        )
        return (
            f"matched the learned rule {self.rule.match_pattern!r} "
            f"({confirmations}) for this client"
        )


def find_rule(txn: BankTransaction, rules: list[MerchantRule]) -> RuleHit | None:
    """First matching rule wins, longest pattern first.

    The ordering is what stops a broad "GRAB" rule swallowing "GRAB *TRIP".
    Rules arrive pre-sorted by pattern length from the repository, but the sort
    is repeated here so the function is correct on any input.
    """
    description = txn.raw_description.upper()
    for rule in sorted(rules, key=lambda r: -len(r.match_pattern)):
        pattern = rule.match_pattern.upper()
        hit = (
            description.startswith(pattern)
            if rule.match_type == MatchType.PREFIX
            else pattern in description
        )
        if hit:
            return RuleHit(rule=rule, matched_text=pattern)
    return None


def preview_rule(pattern: str, transactions: list[BankTransaction]) -> list[BankTransaction]:
    """Which past transactions a proposed pattern would have captured.

    Shown to the accountant before a rule is saved. It is the difference
    between confirming a rule and discovering three months later that it has
    been quietly miscoding something.
    """
    needle = pattern.upper()
    return [t for t in transactions if needle in t.raw_description.upper()]


# ---------------------------------------------------------------- tier 2


@dataclass
class Example:
    description: str
    account_code: str
    tax_code: str | None
    similarity: float


def similar_corrections(
    txn: BankTransaction, corrections: list[dict], limit: int | None = None
) -> list[Example]:
    """Past corrections for this client, most similar first.

    Uses token-set similarity rather than embeddings: bank descriptions are
    short, noisy, and full of shared boilerplate, and it keeps the system
    runnable with no API key.
    """
    limit = limit or get_settings().memory_example_count
    scored: list[Example] = []
    description = txn.raw_description.upper()

    for row in corrections:
        candidate = (row.get("raw_description") or "").upper()
        if not candidate:
            continue
        similarity = fuzz.token_set_ratio(description, candidate) / 100.0
        if similarity < 0.4:
            continue
        scored.append(
            Example(
                description=row["raw_description"],
                account_code=row["to_account_id"],
                tax_code=row.get("to_tax_code"),
                similarity=round(similarity, 3),
            )
        )

    scored.sort(key=lambda e: -e.similarity)
    return scored[:limit]


def format_examples(examples: list[Example]) -> str:
    if not examples:
        return "(no previous corrections for this client yet)"
    return "\n".join(
        f'  "{e.description}" -> account {e.account_code}'
        + (f" tax {e.tax_code}" if e.tax_code else "")
        for e in examples
    )


# ---------------------------------------------------------------- tier 3


def format_facts(facts: list[str]) -> str:
    """Answers the client has given to previous clarifications."""
    if not facts:
        return "(none recorded yet)"
    return "\n".join(f"  - {fact}" for fact in facts)


def should_create_rule(txn: BankTransaction) -> tuple[bool, str]:
    """Whether a correction on this transaction may become a permanent rule.

    A merchant name the model half-read must never become one. The allocation
    itself can be approved - a human has looked at it - but a rule keyed on a
    guessed name would silently miscode every month until somebody noticed.
    """
    from ..models import Legibility

    legibility = txn.field_legibility.get("raw_description")
    if legibility in (Legibility.AMBIGUOUS, Legibility.UNREADABLE):
        return False, (
            f"the description was recorded as {legibility}, so it is not a safe "
            f"basis for a permanent rule"
        )
    return True, ""

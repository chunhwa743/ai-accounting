"""What to do when the model could not read a field cleanly.

A single confidence number for a whole extraction is uninterpretable: "0.6"
could mean the amounts were blurry, which arithmetic verifies, or that the
descriptions were, which arithmetic cannot see. So the model reports, per
field, what it could and could not read - an observation rather than a
self-assessment - and this module decides what that means.

The decision turns on how much redundancy a field carries:

  * Natural language is roughly half redundant. "SINGT?L" has one plausible
    reading, so context recovers it.
  * An identifier has none. One wrong digit in a reference number points at a
    different real thing, and no context narrows it.

FIELD_CLASS is a constant, not a table and not something the model sees.
"""

from __future__ import annotations

from ..models import FieldClass, Legibility

# Every field the extraction calls can return. Fields derived by our own code
# are absent: the model never reported on them, so there is no legibility to
# judge.
FIELD_CLASS: dict[str, FieldClass] = {
    # -- statement header --
    "bank_name": FieldClass.REDUNDANT,
    "account_holder": FieldClass.REDUNDANT,
    "account_number": FieldClass.IDENTIFIER,
    "period_start": FieldClass.VERIFIABLE,
    "period_end": FieldClass.VERIFIABLE,
    "opening_balance": FieldClass.VERIFIABLE,
    "closing_balance": FieldClass.VERIFIABLE,
    # -- statement lines --
    "raw_description": FieldClass.REDUNDANT,
    "txn_date": FieldClass.VERIFIABLE,
    "money_in": FieldClass.VERIFIABLE,
    "money_out": FieldClass.VERIFIABLE,
    "balance_after": FieldClass.VERIFIABLE,
    "bank_reference": FieldClass.IDENTIFIER,
    # -- supporting documents --
    "vendor_name": FieldClass.REDUNDANT,
    "summary": FieldClass.REDUNDANT,
    "doc_number": FieldClass.IDENTIFIER,
    "doc_date": FieldClass.VERIFIABLE,
    "total_amount": FieldClass.VERIFIABLE,
    "tax_amount": FieldClass.VERIFIABLE,
}


class Action:
    """What the harness does about one unclear field."""

    PROCEED = "PROCEED"        # nothing to do
    PENALISE = "PENALISE"      # continue, but carry a confidence penalty forward
    LET_CHECK_DECIDE = "LET_CHECK_DECIDE"  # arithmetic is stronger evidence than the model
    ESCALATE = "ESCALATE"      # a human must supply the value


# Field class x legibility -> action.
#
# IDENTIFIER + INFERRED still escalates. A serial number has no surrounding
# context that makes alternatives implausible, so a model claiming inference
# there is over-claiming rather than reasoning, and honouring it would let a
# guessed account number through.
_MATRIX: dict[tuple[FieldClass, Legibility], str] = {
    (FieldClass.REDUNDANT, Legibility.CLEAR): Action.PROCEED,
    (FieldClass.REDUNDANT, Legibility.INFERRED): Action.PROCEED,
    (FieldClass.REDUNDANT, Legibility.AMBIGUOUS): Action.PENALISE,
    (FieldClass.REDUNDANT, Legibility.UNREADABLE): Action.PENALISE,

    (FieldClass.VERIFIABLE, Legibility.CLEAR): Action.PROCEED,
    (FieldClass.VERIFIABLE, Legibility.INFERRED): Action.LET_CHECK_DECIDE,
    (FieldClass.VERIFIABLE, Legibility.AMBIGUOUS): Action.LET_CHECK_DECIDE,
    (FieldClass.VERIFIABLE, Legibility.UNREADABLE): Action.LET_CHECK_DECIDE,

    (FieldClass.IDENTIFIER, Legibility.CLEAR): Action.PROCEED,
    (FieldClass.IDENTIFIER, Legibility.INFERRED): Action.ESCALATE,
    (FieldClass.IDENTIFIER, Legibility.AMBIGUOUS): Action.ESCALATE,
    (FieldClass.IDENTIFIER, Legibility.UNREADABLE): Action.ESCALATE,
}


def classify_field(field: str) -> FieldClass:
    """Unknown fields are treated as identifiers.

    Failing safe means escalating something harmless, not accepting a guess.
    """
    return FIELD_CLASS.get(field, FieldClass.IDENTIFIER)


def decide(field: str, legibility: Legibility) -> str:
    return _MATRIX[(classify_field(field), legibility)]


def escalating_fields(legibility: dict[str, Legibility]) -> list[str]:
    """Fields that a human must supply before the run can continue."""
    return [f for f, lg in legibility.items() if decide(f, lg) == Action.ESCALATE]


def penalised_fields(legibility: dict[str, Legibility]) -> list[str]:
    """Fields that do not block, but make their transaction less trustworthy."""
    return [f for f, lg in legibility.items() if decide(f, lg) == Action.PENALISE]


def clear_identifier(legibility: dict[str, Legibility], field: str) -> dict[str, Legibility]:
    """Drop an identifier from the legibility map once something verified it.

    An unreadable document number stops mattering if amount, date and vendor
    all matched a bank transaction: the cross-check has resolved it, so the
    field is promoted out of the escalating set.
    """
    return {f: lg for f, lg in legibility.items() if f != field}


# Wording the extraction prompt must carry verbatim, because the whole policy
# depends on the model applying one specific test rather than its own sense of
# how sure it feels.
LEGIBILITY_INSTRUCTIONS = """\
For every field you extract, judge how reliably you could read it and report
only the fields that were not perfectly clear.

  clear       read directly, no ambiguity
  inferred    some characters were unclear, but only one reading is plausible
  ambiguous   several readings are plausible and you cannot choose between them
  unreadable  you cannot make it out at all

Use `inferred` only when a competent human reading the same image would arrive
at the same value with near-certainty, because context makes every alternative
implausible.

If a different reading of the unclear characters would produce a DIFFERENT
REAL-WORLD ENTITY - a different invoice, a different bank account, a different
company - use `ambiguous`, never `inferred`. Serial numbers, reference codes
and account numbers have no surrounding context to constrain them, so an
unclear character in one of those is always `ambiguous`.

Never silently substitute a plausible value for one you could not read.
"""

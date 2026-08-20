"""The chart of accounts, tax codes, and confidence policy.

The chart of accounts lives in the ``account`` table, seeded from YAML, so that
``allocation.account_id`` has a real foreign key behind it. It is loaded once
and cached: accounts change a few times a year, deliberately, and every
categorisation call needs the full list.

Tax codes and the confidence weights stay in YAML. They are configuration
rather than data - fixed by legislation in one case, tuned against ground truth
in the other - and nothing foreign-keys to either.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Any

import yaml

from .config import get_settings
from .models import RiskLevel, TaxCode


def _load_yaml(path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class ChartOfAccounts:
    def __init__(self, accounts: list[Account]) -> None:
        self._by_code = {a.code: a for a in accounts}
        self._accounts = accounts

    def __iter__(self):
        return iter(self._accounts)

    def __len__(self) -> int:
        return len(self._accounts)

    def get(self, code: str | None) -> Account | None:
        if code is None:
            return None
        return self._by_code.get(str(code).strip())

    def exists(self, code: str | None) -> bool:
        return self.get(code) is not None

    @property
    def active(self) -> list[Account]:
        return [a for a in self._accounts if a.is_active]

    def is_high_risk(self, code: str | None) -> bool:
        account = self.get(code)
        return account is not None and account.risk_level == RiskLevel.HIGH

    def default_tax_code(self, code: str | None) -> str | None:
        account = self.get(code)
        return account.default_tax_code if account else None

    def prompt_listing(self) -> str:
        """The account list as it appears in the categorisation prompt.

        Notes are included because they carry the distinctions that matter -
        subcontractor versus drawings, blocked input tax, and so on.
        """
        lines = []
        for account in self.active:
            line = f"{account.code}  {account.name}  [{account.type}]"
            if account.notes:
                note = " ".join(account.notes.split())
                line += f"  -- {note}"
            lines.append(line)
        return "\n".join(lines)


class TaxCodeSet:
    def __init__(self, codes: dict[str, TaxCode], review_required: list[str], rate: Decimal):
        self._codes = codes
        self.review_required = set(review_required)
        self.standard_rate = rate

    def get(self, code: str | None) -> TaxCode | None:
        if code is None:
            return None
        return self._codes.get(str(code).strip().upper())

    def exists(self, code: str | None) -> bool:
        return self.get(code) is not None

    def requires_review(self, code: str | None) -> bool:
        return code is not None and code.strip().upper() in self.review_required

    def rate_for(self, code: str | None) -> Decimal:
        entry = self.get(code)
        return entry.rate if entry else Decimal("0")

    def prompt_listing(self) -> str:
        lines = []
        for code, entry in self._codes.items():
            line = f"{code}  {entry.name}  ({entry.rate:.0%})"
            if entry.description:
                line += f"  -- {' '.join(entry.description.split())}"
            lines.append(line)
        return "\n".join(lines)


# Cached in a module global rather than with lru_cache, so that overriding it
# reaches every caller. Modules import this function by name, so patching the
# attribute would leave their references pointing at the original.
_chart: ChartOfAccounts | None = None


def get_chart_of_accounts() -> ChartOfAccounts:
    """The seeded chart, loaded once.

    Rows are detached from their session afterwards, so callers can hold them
    without keeping a connection open - this is reached from the scorer, the
    exporters and the API alike.
    """
    global _chart
    if _chart is not None:
        return _chart

    from .db.repo import Repositories

    repos = Repositories.open()
    try:
        accounts = repos.accounts.list_active()
        repos.session.expunge_all()
    finally:
        repos.close()

    if not accounts:
        raise RuntimeError(
            "The chart of accounts is empty. Run: python scripts/seed_db.py"
        )
    _chart = ChartOfAccounts(accounts)
    return _chart


def set_chart_of_accounts(chart: ChartOfAccounts | None) -> None:
    """Override the cached chart. Used by tests and after seeding."""
    global _chart
    _chart = chart


def refresh_chart_of_accounts() -> None:
    """Drop the cache, so the next call reloads from the database."""
    set_chart_of_accounts(None)


def load_chart_of_accounts_yaml() -> list[dict]:
    """The seed source, read by scripts/seed_db.py."""
    return _load_yaml(get_settings().chart_of_accounts_path)["accounts"]


@lru_cache(maxsize=1)
def get_tax_codes() -> TaxCodeSet:
    raw = _load_yaml(get_settings().tax_codes_path)
    codes = {
        code: TaxCode(
            code=code,
            name=entry["name"],
            rate=Decimal(str(entry["rate"])),
            claimable=entry["claimable"],
            applies_to=entry["applies_to"],
            description=entry.get("description"),
        )
        for code, entry in raw["codes"].items()
    }
    return TaxCodeSet(
        codes=codes,
        review_required=raw.get("review_required", []),
        rate=Decimal(str(raw["standard_rate"])),
    )


@lru_cache(maxsize=1)
def get_confidence_config() -> dict[str, Any]:
    return _load_yaml(get_settings().confidence_config_path)


def resolve_tax_code(account_code: str | None, gst_registered: bool) -> str | None:
    """Derive the tax code from the account.

    The chart is organised by the nature of the transaction, and GST treatment
    is determined by the same thing, so the account already encodes the answer
    for the large majority of rows. The model only overrides for imported
    services and non-registered suppliers.
    """
    if not gst_registered:
        return "NA"
    return get_chart_of_accounts().default_tax_code(account_code)

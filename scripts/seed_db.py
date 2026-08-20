#!/usr/bin/env python
"""Load master data into the database.

    python scripts/seed_db.py

Idempotent: safe to run repeatedly. Existing rows are updated rather than
duplicated, so this is also how you apply an edit to the chart of accounts or a
client profile.

What counts as master data, and why it is here rather than in a script:

  * The chart of accounts. The system selects from it and never inserts into
    it - letting it would fragment the chart into Telephone, Phone and Telco
    within a month.
  * Clients. A client is a business relationship, not something a run invents.
  * Firm staff, so corrections have someone to belong to.

Test transactions are NOT master data. They live in data/testdata/*.md and are
rendered into files by scripts/generate_test_files.py.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aiacct.config import get_settings  # noqa: E402
from aiacct.db import Account, Client, DatabaseUnavailable, Repositories, check_connection  # noqa: E402
from aiacct.db.session import get_engine  # noqa: E402
from aiacct.models import ClientProfile  # noqa: E402
from aiacct.reference import load_chart_of_accounts_yaml, refresh_chart_of_accounts  # noqa: E402

SEEDS = ROOT / "data" / "seeds"


def load(name: str) -> dict:
    return yaml.safe_load((SEEDS / name).read_text(encoding="utf-8"))


def seed_accounts(repos: Repositories) -> tuple[int, int]:
    entries = load_chart_of_accounts_yaml()
    before = len(repos.accounts.list_active())
    for entry in entries:
        repos.accounts.upsert(
            Account(
                code=str(entry["code"]),
                name=entry["name"],
                type=entry["type"],
                default_tax_code=entry["default_tax_code"],
                risk_level=entry.get("risk_level", "LOW"),
                notes=entry.get("notes"),
                is_active=True,
            )
        )
    repos.session.commit()
    return len(entries), len(repos.accounts.list_active()) - before


def seed_users(repos: Repositories, settings) -> int:
    """Create the firm's staff and give them a password.

    Accounts are seeded rather than self-registered: a firm decides who works
    on its clients' books, so there is no sign-up endpoint. The password comes
    from settings.seed_password and is the same for everyone - fine for a demo,
    and documented in the README so nobody has to guess.
    """
    from aiacct.auth import hash_password

    entries = load("users.yaml")["users"]
    password_hash = hash_password(settings.seed_password)

    for entry in entries:
        user = repos.users.get_or_create(entry["name"], entry["email"])
        # Re-applied on every seed, so a forgotten password is one command away.
        repos.users.set_password(user.id, password_hash)
    return len(entries)


def seed_clients(repos: Repositories) -> tuple[int, int]:
    entries = load("clients.yaml")["clients"]
    created = 0

    for entry in entries:
        raw = entry["profile"]
        profile = ClientProfile(
            business_description=" ".join(raw["business_description"].split()),
            gst_registered=raw["gst_registered"],
            own_bank_accounts=raw.get("own_bank_accounts", []),
            capitalisation_threshold=Decimal(str(raw["capitalisation_threshold"])),
            materiality_threshold=Decimal(str(raw["materiality_threshold"])),
            # Never seeded over: these accumulate from clarifications the client
            # answered, and overwriting them would throw that away.
            learned_facts=[],
        )

        existing = repos.clients.get_by_uen(entry["uen"])
        if existing is None:
            repos.clients.create(
                Client(name=entry["name"], uen=entry["uen"], profile=profile)
            )
            created += 1
        else:
            profile.learned_facts = existing.profile.learned_facts
            existing.name = entry["name"]
            repos.clients.update_profile(existing.id, profile)

    return len(entries), created


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", help="override DATABASE_URL, e.g. to seed the test database"
    )
    args = parser.parse_args()

    settings = get_settings()
    if args.url:
        settings.database_url = args.url

    try:
        version = check_connection(settings)
    except DatabaseUnavailable as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    print(f"\nSeeding {get_engine(settings).url.database} ({version})\n")

    repos = Repositories.open(settings.database_url)
    try:
        total, new = seed_accounts(repos)
        print(f"  chart of accounts   {total} accounts ({new} new)")

        users = seed_users(repos, settings)
        print(f"  firm staff          {users} user(s), password: {settings.seed_password!r}")

        total, new = seed_clients(repos)
        print(f"  clients             {total} ({new} new, {total - new} updated)")
    finally:
        repos.close()

    refresh_chart_of_accounts()
    print("\nDone. Test files: python scripts/generate_test_files.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared fixtures.

Tests run against a real Postgres database rather than SQLite, because the
things most likely to be wrong in a schema - JSONB round-trips, numeric
precision, CHECK and FK enforcement - are exactly the things SQLite would not
have caught.

Each test runs inside a transaction that is rolled back afterwards, so tests
neither see each other's data nor have to clean up. The schema is created once
for the session and dropped at the end.

The offline provider stays forced on, so the suite still needs no API key.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from aiacct.config import get_settings
from aiacct.db import Base, Repositories
from aiacct.db.models import Account, Client
from aiacct.db.session import get_engine, reset_engines
from aiacct.models import ClientProfile
from aiacct.reference import load_chart_of_accounts_yaml


def pytest_configure(config):
    """Fail with instructions rather than a psycopg traceback."""
    settings = get_settings()
    url = settings.resolved_test_database_url
    try:
        with get_engine(url=url).connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        name = url.rsplit("/", 1)[-1]
        raise pytest.UsageError(
            f"\n\nThe test database is not reachable.\n\n"
            f"    psql -U postgres -c 'CREATE DATABASE \"{name}\";'\n\n"
            f"Tests use a separate database so a teardown bug cannot leave "
            f"clutter next to real data.\n\n{str(exc)[:200]}"
        ) from exc


@pytest.fixture(scope="session")
def database_url() -> str:
    return get_settings().resolved_test_database_url


@pytest.fixture(scope="session", autouse=True)
def schema(database_url):
    """Build the schema once from the models, and drop it at the end.

    Built from ``Base.metadata`` rather than by running migrations: a test run
    should exercise the models as written, and a migration that has drifted
    from them is what the autogenerate check is for.
    """
    engine = get_engine(url=database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
    reset_engines()


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path, database_url):
    """No network, no key, and throwaway directories per test."""
    settings = get_settings()
    monkeypatch.setattr(settings, "use_stub_llm", True)
    monkeypatch.setattr(settings, "database_url", database_url)
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "export_dir", tmp_path / "exports")
    return settings


@pytest.fixture
def session(schema, database_url):
    """A transaction that is rolled back, so tests cannot leak into each other.

    The session joins an outer transaction on a single connection. Anything the
    code under test commits lands inside that outer transaction and disappears
    when it is rolled back.
    """
    from sqlalchemy.orm import Session

    connection = get_engine(url=database_url).connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def repos(session):
    return Repositories(session)


@pytest.fixture(autouse=True)
def chart_of_accounts(repos, monkeypatch):
    """Seed the chart, since allocations now have a real foreign key to it.

    The cached loader opens its own session, which cannot see this test's
    uncommitted transaction, so it is pointed at the seeded rows directly.
    """
    from aiacct import reference

    for entry in load_chart_of_accounts_yaml():
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
    repos.session.flush()

    chart = reference.ChartOfAccounts(repos.accounts.list_active())
    reference.set_chart_of_accounts(chart)
    yield chart
    reference.set_chart_of_accounts(None)


@pytest.fixture
def user(repos):
    return repos.users.get_or_create("Wei Ling Tan", "weiling@firm.example")


@pytest.fixture
def agency(repos):
    """A design agency. GRAB means travel here."""
    return repos.clients.create(
        Client(
            name="Lumina Design Studio Pte Ltd",
            uen="202512345A",
            profile=ClientProfile(
                business_description="Boutique design agency",
                gst_registered=True,
                own_bank_accounts=["003-88291-1", "501-44012-8"],
                capitalisation_threshold=Decimal("1000.00"),
                materiality_threshold=Decimal("5000.00"),
            ),
        )
    )


@pytest.fixture
def restaurant(repos):
    """A different client. GRAB means a delivery cost here, not travel."""
    return repos.clients.create(
        Client(
            name="Kopi & Co Pte Ltd",
            uen="202398765B",
            profile=ClientProfile(
                business_description="Coffee shop and kitchen",
                gst_registered=True,
                own_bank_accounts=["501-99120-4"],
            ),
        )
    )

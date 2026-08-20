"""Engine and session management.

Synchronous by choice. The bottleneck in this system is LLM calls, not the
database, and the LangGraph pipeline is synchronous - so async sessions would
add greenlet complexity and two session factories for no gain. FastAPI runs
``def`` endpoints in a threadpool, so sync sessions are safe there too.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

_engines: dict[str, Engine] = {}


class DatabaseUnavailable(RuntimeError):
    """Raised with instructions rather than a psycopg traceback.

    A missing database is the single most likely first-run failure, so it gets
    a message that names the fix.
    """


def get_engine(settings: Settings | None = None, url: str | None = None) -> Engine:
    settings = settings or get_settings()
    url = url or settings.database_url
    if url not in _engines:
        _engines[url] = create_engine(
            url,
            pool_pre_ping=True,   # a laptop sleeping should not poison the pool
            pool_size=5,
            max_overflow=10,
            future=True,
        )
    return _engines[url]


def get_sessionmaker(settings: Settings | None = None, url: str | None = None):
    return sessionmaker(
        bind=get_engine(settings, url), expire_on_commit=False, class_=Session
    )


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """A transaction that commits on success and rolls back on failure."""
    factory = get_sessionmaker(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_connection(settings: Settings | None = None) -> str:
    """Verify the database is reachable, or explain what to do about it."""
    settings = settings or get_settings()
    url = make_url(settings.database_url)

    try:
        with get_engine(settings).connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar_one()
            has_tables = conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            ).scalar_one()
    except OperationalError as exc:
        message = str(exc)
        if "does not exist" in message and url.database:
            raise DatabaseUnavailable(
                f'The database "{url.database}" does not exist. Create it with:\n'
                f'    psql -U {url.username or "postgres"} '
                f'-c \'CREATE DATABASE "{url.database}";\''
            ) from exc
        if "password authentication failed" in message:
            raise DatabaseUnavailable(
                f"Postgres rejected the credentials for user "
                f'"{url.username}". Check DATABASE_URL in .env.'
            ) from exc
        raise DatabaseUnavailable(
            f"Could not reach Postgres at {url.host}:{url.port or 5432}. "
            f"Is the server running? Check DATABASE_URL in .env.\n{message[:200]}"
        ) from exc

    if has_tables == 0:
        log.warning(
            'Database "%s" is empty. Run: alembic upgrade head', url.database
        )
    return version.split(",")[0]


def reset_engines() -> None:
    """Dispose every pooled engine. Used between test sessions."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()

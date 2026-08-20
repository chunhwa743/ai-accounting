"""Alembic environment.

The database URL comes from Settings rather than alembic.ini, so there is one
place that knows where the database is and .env stays the only thing to
configure.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aiacct.config import get_settings  # noqa: E402
from aiacct.db.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata


# LangGraph creates and owns these. They live in the same database, but
# autogenerate would see them as tables the models no longer declare and
# propose dropping them - which would delete the state of every paused run.
LANGGRAPH_TABLES = {
    "checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations",
}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in LANGGRAPH_TABLES:
        return False
    return True


def render_item(type_, obj, autogen_context):
    """Render the custom column types as what they actually are in Postgres.

    PydanticJSON and LegibilityMap are TypeDecorators: they change how Python
    marshals values, not what the database stores. At DDL level both are plain
    jsonb, and rendering them by class name would emit a migration that imports
    application code - which would then break the moment that code moved.
    """
    if type_ == "type" and obj.__class__.__name__ in ("PydanticJSON", "LegibilityMap"):
        autogen_context.imports.add(
            "from sqlalchemy.dialects import postgresql"
        )
        return "postgresql.JSONB(astext_type=sa.Text())"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Catch a column whose type drifted from the model, not just
            # columns that appeared or vanished.
            compare_type=True,
            render_item=render_item,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

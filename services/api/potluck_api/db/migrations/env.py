from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# ---------------------------------------------------------------------------
# Import Base and all models so that Base.metadata is populated.
# The models __init__.py re-exports every mapped class.
# ---------------------------------------------------------------------------
from potluck_api.db.base import Base
import potluck_api.models  # noqa: F401 - side-effect import to register all tables

from potluck_api.config import get_settings

# ---------------------------------------------------------------------------
# Alembic Config object - provides access to values in alembic.ini.
# ---------------------------------------------------------------------------
config = context.config

# Configure Python logging from alembic.ini if a logging section is present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The MetaData object for 'autogenerate' support.
target_metadata = Base.metadata


def get_url() -> str:
    """Return DATABASE_URL from application settings, not from alembic.ini.

    This means the ini file's sqlalchemy.url is intentionally left blank.
    The URL is always driven by the environment (12-Factor).
    """
    return get_settings().database_url


# ---------------------------------------------------------------------------
# Offline migrations (generate SQL without a live DB connection)
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine. Calls to
    context.execute() emit the SQL to the script output.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migrations (run against a live connection)
# ---------------------------------------------------------------------------


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations in 'online' mode."""
    connectable = create_async_engine(
        get_url(),
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

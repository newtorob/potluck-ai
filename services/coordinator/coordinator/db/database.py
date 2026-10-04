from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from coordinator.config import get_settings

logger = structlog.get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db() -> None:
    """Create the async engine and session factory.

    Called once from the lifespan context manager in app.py.
    Does NOT create tables — Alembic owns the schema.
    """
    global _engine, _session_factory

    settings = get_settings()

    _engine = create_async_engine(
        settings.database_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        echo=settings.env == "development",
    )

    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    logger.info("db_engine_created", url=settings.database_url.split("@")[-1])


async def close_db() -> None:
    """Dispose the engine and release all connections back to the pool."""
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("db_engine_disposed")


async def check_database(timeout: float = 2.0) -> None:
    """Probe the real pool with a deadline, including connection acquisition."""
    if _engine is None:
        raise RuntimeError("Database not initialised")
    async with asyncio.timeout(timeout):
        async with _engine.connect() as connection:
            await connection.execute(text("SELECT 1"))


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    if _session_factory is None:
        raise RuntimeError("Database not initialised — init_db() was not called")

    async with _session_factory() as session:
        yield session


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory for code paths that outlive the request
    lifecycle (e.g. background tasks, streaming-response finalizers that
    need to update DB rows AFTER the StreamingResponse has consumed the
    request-scoped session)."""
    if _session_factory is None:
        raise RuntimeError("Database not initialised — init_db() was not called")
    return _session_factory

from __future__ import annotations

from collections.abc import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from potluck_api.config import get_settings

logger = structlog.get_logger(__name__)

# Module-level globals — set by init_db(), cleared by close_db().
# Never imported directly from other modules; use get_db() as a FastAPI dependency.
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
        pool_pre_ping=True,  # validate connections before use
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


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an AsyncSession per request.

    Usage::

        @router.get("/example")
        async def handler(db: AsyncSession = Depends(get_db)):
            ...
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialised — init_db() was not called")

    async with _session_factory() as session:
        yield session

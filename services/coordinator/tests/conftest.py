"""Shared pytest fixtures for coordinator tests.

DB-level fixtures create a fresh async engine per test (required because
pytest-asyncio 0.24 with asyncio_mode=auto spins up a new event loop per
test function, and asyncpg connections are bound to the loop that created
them). Each test wraps its writes in a transaction that is rolled back
after the test, leaving the live DB clean.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_DB_URL = "postgresql+asyncpg://potluck:potluck@localhost:5432/potluck"


@pytest_asyncio.fixture()
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Async session bound to the current test's event loop.

    Creates a fresh engine + connection for each test so there are no
    cross-loop asyncpg future references. Rolls back all writes after the
    test completes so the live DB is left untouched.
    """
    engine = create_async_engine(_TEST_DB_URL, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with factory() as session:
            async with session.begin():
                yield session
                # Unconditional rollback — leave DB clean regardless of
                # pass/fail.
                await session.rollback()
    finally:
        await engine.dispose()

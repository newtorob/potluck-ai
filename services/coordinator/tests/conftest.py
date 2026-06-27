"""Shared pytest fixtures for coordinator tests.

DB-level fixtures create a fresh async engine per test (required because
pytest-asyncio 0.24 with asyncio_mode=auto spins up a new event loop per
test function, and asyncpg connections are bound to the loop that created
them). Each test wraps its writes in a transaction that is rolled back
after the test, leaving the live DB clean.

Two session fixtures are provided:

  db_session — yields an AsyncSession inside a single open transaction that
    is rolled back unconditionally at the end. Use for tests that stay within
    one session (record_canary_result, release_escrow_for_peer, etc.).

  session_factory — yields an async_sessionmaker plus a cleanup coroutine
    list. Use for tests that call functions which open their own sessions
    (run_canary_probe, probe_due_peers). Tests commit fixture data so the
    function-under-test can read it via a fresh session, then register peer
    IDs for teardown cleanup.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

import pytest_asyncio
from sqlalchemy import delete, select
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


@dataclass
class FactoryFixture:
    """Wraps a session factory and tracks peer IDs created during a test so
    teardown can delete them and leave the DB clean."""

    factory: async_sessionmaker[AsyncSession]
    _peer_ids: list[uuid.UUID] = field(default_factory=list)

    def track_peer(self, peer_id: uuid.UUID) -> None:
        """Register a peer ID for deletion during teardown."""
        self._peer_ids.append(peer_id)


@pytest_asyncio.fixture()
async def session_factory() -> AsyncGenerator[FactoryFixture, None]:
    """Yields a FactoryFixture for tests that call functions which open their
    own sessions internally (run_canary_probe, probe_due_peers).

    Tests must commit fixture data before invoking the function under test so
    that the function's fresh internal session can see it. After the test,
    all tracked peers (and their cascading rows) are deleted so the live DB
    is left clean.
    """
    from coordinator.models.peer import Peer, PeerCapability, PeerHeartbeat
    from coordinator.models.credit import CreditLedger
    from coordinator.models.job import Job

    engine = create_async_engine(_TEST_DB_URL, pool_size=5, max_overflow=0)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    fixture = FactoryFixture(factory=factory)

    try:
        yield fixture
    finally:
        # Delete all rows created for this test, cascading from outermost to
        # innermost to respect FK constraints (credit_ledger -> job -> peer).
        if fixture._peer_ids:
            async with factory() as cleanup_db:
                async with cleanup_db.begin():
                    # credit_ledger rows via job -> peer
                    job_ids_q = (
                        select(Job.id).where(Job.peer_id.in_(fixture._peer_ids))
                    )
                    await cleanup_db.execute(
                        delete(CreditLedger).where(
                            CreditLedger.job_id.in_(job_ids_q)
                        )
                    )
                    await cleanup_db.execute(
                        delete(Job).where(Job.peer_id.in_(fixture._peer_ids))
                    )
                    await cleanup_db.execute(
                        delete(PeerCapability).where(
                            PeerCapability.peer_id.in_(fixture._peer_ids)
                        )
                    )
                    await cleanup_db.execute(
                        delete(PeerHeartbeat).where(
                            PeerHeartbeat.peer_id.in_(fixture._peer_ids)
                        )
                    )
                    await cleanup_db.execute(
                        delete(Peer).where(Peer.id.in_(fixture._peer_ids))
                    )
        await engine.dispose()

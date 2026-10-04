"""Pytest harness for the cloud API.

Runs the real FastAPI app in-process (httpx ASGITransport) against a dedicated
``potluck_test`` Postgres database on the same server the app already uses. The
models are Postgres-specific (JSONB, gen_random_uuid, server_defaults), so we
test against real Postgres rather than SQLite.

Isolation: the schema is created once per session; every test truncates all
tables afterward. Each request gets its own session (like production) via a
get_db override bound to the test engine.

Run inside the api container:  ``docker compose exec api pytest -q``
(dev-only deps: see requirements-dev.txt).
"""
from __future__ import annotations

import os
import re

# Deterministic settings before the app imports get_settings() (lru_cached).
os.environ.setdefault("ENV", "development")
os.environ.setdefault(
    "MAGIC_LINK_SECRET",
    "test-secret-not-real-64-chars-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
)
# A webhook secret lets the billing tests sign events; the secret KEY stays
# unset so billing reads as "not configured" (503 on checkout/portal).
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_ci_secret")

import asyncio

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import potluck_api.models  # noqa: F401  -- registers every table on Base.metadata
from potluck_api.config import get_settings
from potluck_api.db.base import Base
from potluck_api.db.database import get_db
from potluck_api.middleware.rate_limit import limiter

# Rate limiting is a config concern, not app logic; disabling it keeps the suite
# deterministic (no cross-test 429s from a shared in-memory counter).
limiter.enabled = False

_SRC = make_url(get_settings().database_url)
_TEST_DB = os.environ.get("POTLUCK_API_TEST_DB", "potluck_test")
if not re.fullmatch(r"potluck_test(?:_[a-z0-9_]+)?", _TEST_DB):
    raise ValueError("POTLUCK_API_TEST_DB must be an isolated potluck_test database name")
TEST_URL = _SRC.set(database=_TEST_DB).render_as_string(hide_password=False)


async def _admin_conn() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=_SRC.host,
        port=_SRC.port or 5432,
        user=_SRC.username,
        password=_SRC.password,
        database="postgres",
    )


async def _bootstrap() -> None:
    conn = await _admin_conn()
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{_TEST_DB}"')
    finally:
        await conn.close()
    engine = create_async_engine(TEST_URL)
    async with engine.begin() as db_conn:
        await db_conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


async def _teardown() -> None:
    conn = await _admin_conn()
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def _db_ready():
    """Create the test database + schema once; drop it at the end.

    Synchronous on purpose: each asyncio.run() completes cleanly, sidestepping
    the session-vs-function event-loop mismatch pytest-asyncio otherwise hits.
    """
    asyncio.run(_bootstrap())
    yield
    asyncio.run(_teardown())


@pytest_asyncio.fixture
async def engine(_db_ready):
    eng = create_async_engine(TEST_URL)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker_(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(sessionmaker_):
    """A session for arranging fixtures and asserting DB state directly."""
    async with sessionmaker_() as session:
        yield session


@pytest_asyncio.fixture
async def client(sessionmaker_):
    """AsyncClient hitting the real app; get_db is bound to the test engine."""
    from potluck_api.app import create_app

    app = create_app()

    async def _override_get_db():
        async with sessionmaker_() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _truncate(engine):
    """Wipe every table after each test so cases never leak into each other."""
    yield
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

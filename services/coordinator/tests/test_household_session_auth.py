"""Auth posture for the two user-action mesh routes: POST /mesh/households
and POST /mesh/pairing-tokens.

These are member actions (a logged-in member managing their own household),
not machine/sidecar plumbing, so they are gated only by
`resolve_session_member_id` (the member's login session), not by
`require_coordinator_token` (the shared machine secret, which must not need
to live in the browser/desktop-app UI that calls these routes).

Contrast with every other /mesh/* route (enroll, peers, heartbeat,
derp-token, node-delete, the signal WS), which stay gated by the
coordinator token - those are called by the sidecar, which correctly holds
it. This suite pins:

  1. Both routes succeed with a valid session and no
     X-Potluck-Coordinator-Token header (even when a coordinator token is
     configured - proves the dependency is absent, not just unconfigured in
     the test env).
  2. Both routes 401 without a session (POTLUCK_SESSION_AUTH_ENABLED=1).
  3. A machine route (/mesh/peers, /mesh/derp-token) still 401s without the
     coordinator token - proves the session-only gate does not extend
     beyond the two intended routes.

Mounts the real router (not a thin re-implementation), like
test_mesh_peers_auth.py, so a future refactor that re-adds the dependency
(or drops session auth) trips this test.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from coordinator.db.database import get_db
from coordinator.routers.mesh import router

_COORDINATOR_TOKEN = "s3cret-coordinator-token"
# Same database the shared fixtures in conftest.py use: the dev stack by
# default, or a throwaway one named by DATABASE_URL.
_TEST_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://potluck:potluck@localhost:5432/potluck"
)


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def app() -> FastAPI:
    return _make_app()


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _asgi_client(app: FastAPI) -> AsyncClient:
    """An httpx.AsyncClient wired directly to the ASGI app via
    ASGITransport, running in-process on the current event loop.

    Deliberately not `fastapi.testclient.TestClient` for the DB-backed
    tests below: TestClient drives the ASGI app from a background thread
    with its own event loop (via anyio's `from_thread` portal), and an
    asyncpg connection is bound to the loop that created it - handing the
    pytest-asyncio-loop-bound `db_session` fixture to a different-loop
    TestClient request blows up with
    ``InterfaceError: cannot perform operation: another operation is in
    progress``. ASGITransport calls the app directly on the caller's loop,
    so `db_session` and the request share one loop and one connection.
    """
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest_asyncio.fixture()
async def commit_db_session() -> AsyncGenerator[AsyncSession, None]:
    """A real, uncached AsyncSession not wrapped in an outer
    `session.begin()` context - unlike the shared `db_session` fixture in
    conftest.py, which wraps the whole test in one transaction that the
    route handler's own explicit `await db.commit()` calls conflict with
    (`InvalidRequestError: Can't operate on closed transaction`, because
    `create_household` / `issue_pairing_token` commit mid-request).

    Cleans up afterward by deleting the member row this test created -
    `households` (owner_member_id), `pairing_tokens` (household_id FK
    CASCADE) and `sessions` (member_id) all cascade or are swept
    transitively from that one delete.
    """
    engine = create_async_engine(_TEST_DB_URL, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    member_ids: list[uuid.UUID] = []

    async with factory() as session:
        session.info["track_member"] = member_ids.append
        try:
            yield session
        finally:
            if member_ids:
                await session.execute(
                    text("DELETE FROM sessions WHERE member_id = ANY(:ids)"),
                    {"ids": member_ids},
                )
                await session.execute(
                    text("DELETE FROM households WHERE owner_member_id = ANY(:ids)"),
                    {"ids": member_ids},
                )
                await session.execute(
                    text("DELETE FROM members WHERE id = ANY(:ids)"),
                    {"ids": member_ids},
                )
                await session.commit()
    await engine.dispose()


async def _insert_member(db: AsyncSession) -> uuid.UUID:
    member_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO members (id, email) VALUES (:id, :email)"),
        {"id": member_id, "email": f"household-sess-{uuid.uuid4().hex}@test.local"},
    )
    track = db.info.get("track_member")
    if track is not None:
        track(member_id)
    return member_id


async def _insert_session(
    db: AsyncSession, member_id: uuid.UUID, raw_token: str, *, ttl_hours: float = 1.0
) -> None:
    await db.execute(
        text(
            "INSERT INTO sessions (member_id, token_hash, expires_at) "
            "VALUES (:m, :h, :e)"
        ),
        {
            "m": member_id,
            "h": hashlib.sha256(raw_token.encode()).hexdigest(),
            "e": datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
        },
    )


# ---------------------------------------------------------------------------
# No X-Potluck-Coordinator-Token header required (dev-mode passthrough -
# no live DB needed to reach this far; households route hits the DB after
# auth passes, so we only assert we get PAST the coordinator-token gate).
# ---------------------------------------------------------------------------


def test_households_no_coordinator_token_needed_dev_session(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coordinator token configured but not sent, session auth off (dev
    passthrough / STUB_MEMBER_ID) -> request is not rejected with 401 for
    lacking the coordinator token. No DB in this suite, so a downstream
    500 (DB not initialised) is the observable "got past auth" signal,
    exactly like test_mesh_peers_auth.py's passthrough tests."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)
    resp = client.post("/mesh/households", json={"name": "Casa"})
    assert resp.status_code != 401


def test_pairing_tokens_no_coordinator_token_needed_dev_session(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)
    resp = client.post("/mesh/pairing-tokens")
    assert resp.status_code != 401


# ---------------------------------------------------------------------------
# Session required (401 without one) once POTLUCK_SESSION_AUTH_ENABLED=1.
#
# Both routes ALSO declare their own `db: AsyncSession = Depends(get_db)`
# (for the household lookup after auth), so with no DB override at all the
# route trips `get_db`'s "Database not initialised" RuntimeError (500)
# before we can observe the 401 - the two dependencies (`db` and
# `resolve_session_member_id`, which itself depends on `get_db`) are
# resolved as siblings, not strictly session-first. We override `get_db`
# with the real `db_session` fixture here so the only thing under test is
# the session check.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_households_401_without_session(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with _asgi_client(app) as ac:
            resp = await ac.post("/mesh/households", json={"name": "Casa"})
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_pairing_tokens_401_without_session(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with _asgi_client(app) as ac:
            resp = await ac.post("/mesh/pairing-tokens")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Full success path: real session, real DB, session auth ON, coordinator
# token configured but never sent - proves the requirement is gone, not
# merely unconfigured.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_households_succeeds_with_session_and_no_coordinator_header(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, commit_db_session: AsyncSession
) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")

    member_id = await _insert_member(commit_db_session)
    raw_token = f"tok-{uuid.uuid4().hex}"
    await _insert_session(commit_db_session, member_id, raw_token)

    async def _override_get_db():
        yield commit_db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with _asgi_client(app) as ac:
            ac.cookies.set("potluck_session", raw_token)
            resp = await ac.post(
                "/mesh/households",
                json={"name": "Casa del Test"},
                # Deliberately no X-Potluck-Coordinator-Token header.
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["owner_member_id"] == str(member_id)
    assert body["name"] == "Casa del Test"


@pytest.mark.asyncio
async def test_pairing_tokens_succeeds_with_session_and_no_coordinator_header(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, commit_db_session: AsyncSession
) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")

    member_id = await _insert_member(commit_db_session)
    raw_token = f"tok-{uuid.uuid4().hex}"
    await _insert_session(commit_db_session, member_id, raw_token)

    async def _override_get_db():
        yield commit_db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with _asgi_client(app) as ac:
            ac.cookies.set("potluck_session", raw_token)
            # Must create the household first - pairing-tokens 404s otherwise.
            create_resp = await ac.post(
                "/mesh/households",
                json={"name": "Casa del Test"},
            )
            assert create_resp.status_code == 201, create_resp.text

            resp = await ac.post(
                "/mesh/pairing-tokens",
                # Deliberately no X-Potluck-Coordinator-Token header.
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["household_id"] == create_resp.json()["id"]
    assert len(body["token"]) > 0


# ---------------------------------------------------------------------------
# Machine routes are untouched: still 401 without the coordinator token.
# Regression guard against accidentally widening the auth relaxation
# beyond the two intended routes.
# ---------------------------------------------------------------------------


def test_mesh_peers_still_requires_coordinator_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    resp = client.get("/mesh/peers", params={"wg_public_key": "A" * 44})
    assert resp.status_code == 401


def test_derp_token_still_requires_coordinator_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    resp = client.post(
        "/mesh/derp-token",
        json={"wg_public_key": "A" * 44, "region": "us-east"},
    )
    assert resp.status_code == 401

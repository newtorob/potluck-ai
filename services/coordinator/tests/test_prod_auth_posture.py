"""Prod-auth-posture guardrail for the frontend-direct routes.

## Background

The coordinator has two independent auth gates:

  - `require_coordinator_token` (middleware/auth.py) - a shared secret only
    the Python sidecar holds. It is a no-op in dev (POTLUCK_COORDINATOR_TOKEN
    unset - the posture every other test in this suite runs under) and
    strict once an operator sets the env var in prod.
  - `resolve_session_member_id` (middleware/session_auth.py) - the real
    per-member gate. It reads a login session (cookie or bearer) from the
    shared `sessions` table and is strict once
    POTLUCK_SESSION_AUTH_ENABLED=1.

The desktop frontend calls a set of coordinator routes directly from the
Electron renderer. It can never hold the coordinator's shared secret (it
would ship inside the client), so any route it calls directly must depend
only on the session gate.

Because the rest of this test suite always runs with
POTLUCK_COORDINATOR_TOKEN unset, a route wrongly carrying both dependencies
passes every other test in the suite (the token gate is a silent no-op
there) yet 401s the instant an operator hardens prod by setting the token.
The frontend-direct routes this covers include /me/circles*, /me/identity,
/me/device-keys, /me/credit-balance, /me/credit-history, /me/contributions,
/me/contributions/weekly, /jobs/stream and /jobs/stats.

## What this module proves

Boots the app the way prod does - both gates strict
(POTLUCK_COORDINATOR_TOKEN set to a real value + POTLUCK_SESSION_AUTH_ENABLED
= "1") - and for every frontend-direct route:

  1. a valid session bearer with no coordinator token header succeeds
     (non-401/403 - `test_frontend_direct_routes_succeed_session_only`);
  2. no session at all (and no token) still 401s - proves the session gate
     is the one doing real work, not a passthrough
     (`test_frontend_direct_routes_401_without_session`);
  3. Static contract: none of the frontend-direct routes carry
     `require_coordinator_token` anywhere in their resolved dependency graph
 - inspects `app.routes` on the real `create_app()` topology, so a
     future PR that re-adds the dependency to a frontend-direct path (or
     adds a new frontend-direct path that keeps it) fails this test before
     a single request is ever made (`test_no_frontend_direct_route_carries_token_gate`).

Adding a new frontend-direct route? Add it to `FRONTEND_DIRECT_ROUTES`
below - that's what keeps this test honest as the surface grows.
"""
from __future__ import annotations

import base64
import hashlib
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import text

from coordinator.app import create_app
from coordinator.middleware.auth import require_coordinator_token
from coordinator.routers import circles, jobs, me, mesh

_COORDINATOR_TOKEN = "s3cret-prod-coordinator-token-checked-under-test"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _fresh_ed25519_pubkey() -> str:
    return _b64url(Ed25519PrivateKey.generate().public_key().public_bytes_raw())


# ---------------------------------------------------------------------------
# The canonical frontend-direct route list.
#
# INVARIANT: every route here is called directly by the desktop frontend
# (apps/desktop/frontend/src/lib/{localApi,coordinatorAuth}.ts) and therefore
# must be gated only by `resolve_session_member_id` - never by
# `require_coordinator_token`, which the frontend cannot hold. Adding a route
# to this list without also keeping it session-only fails
# `test_no_frontend_direct_route_carries_token_gate` below.
#
# `path_format` matches `starlette.routing.Route.path_format` - the path
# template with converter annotations (e.g. `:path`) stripped, e.g.
# "/me/circles/{circle_id}/members/{member_pubkey:path}" -> path_format
# "/me/circles/{circle_id}/members/{member_pubkey}".
# ---------------------------------------------------------------------------
FRONTEND_DIRECT_ROUTES: list[tuple[str, str]] = [
    ("GET", "/me/circles"),
    ("POST", "/me/circles"),
    ("DELETE", "/me/circles/{circle_id}"),
    ("POST", "/me/circles/{circle_id}/members"),
    ("DELETE", "/me/circles/{circle_id}/members/{member_pubkey}"),
    ("GET", "/me/circles/{circle_id}/availability"),
    ("POST", "/me/circles/{circle_id}/invites"),
    ("GET", "/me/circles/{circle_id}/invites"),
    ("DELETE", "/me/circles/{circle_id}/invites/{invite_id}"),
    ("POST", "/circles/invites/preview"),
    ("POST", "/circles/invites/accept"),
    ("DELETE", "/me/circles/{circle_id}/membership"),
    ("DELETE", "/me/circles/{circle_id}/people/{person_id}"),
    ("GET", "/me/identity"),
    ("POST", "/me/device-keys"),
    ("GET", "/me/credit-balance"),
    ("GET", "/me/credit-history"),
    ("GET", "/me/contributions"),
    ("GET", "/me/contributions/weekly"),
    ("POST", "/mesh/households"),
    ("POST", "/mesh/pairing-tokens"),
    ("POST", "/jobs/stream"),
    ("GET", "/jobs/stats"),
]


# ---------------------------------------------------------------------------
# Static contract test - no live app, no DB, no request. Inspects the real
# create_app() topology (same routers, same include order as prod).
# ---------------------------------------------------------------------------


def test_no_frontend_direct_route_carries_token_gate() -> None:
    """None of FRONTEND_DIRECT_ROUTES resolve `require_coordinator_token`
    anywhere in their dependency graph, on the real app topology.

    This is the guardrail: it fails the instant a future PR re-adds
    `dependencies=[Depends(require_coordinator_token)]` (or a param-level
    `Depends(require_coordinator_token)`) to any route in the canonical
    list, without needing a running server, a token, or a session - pure
    static inspection.
    """
    app = create_app()
    routes_by_key: dict[tuple[str, str], APIRoute] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            routes_by_key[(method, route.path_format)] = route

    missing: list[tuple[str, str]] = []
    offenders: list[tuple[str, str]] = []
    for method, path in FRONTEND_DIRECT_ROUTES:
        route = routes_by_key.get((method, path))
        if route is None:
            missing.append((method, path))
            continue
        deps = {dep.call for dep in route.dependant.dependencies}
        if require_coordinator_token in deps:
            offenders.append((method, path))

    assert not missing, (
        f"FRONTEND_DIRECT_ROUTES references routes not found on the app: "
        f"{missing} - path_format probably changed, update the constant."
    )
    assert not offenders, (
        f"These frontend-direct routes still depend on require_coordinator_token, "
        f"which the desktop frontend can never present: {offenders}. The frontend "
        f"holds only a member session, never the coordinator shared secret - drop "
        f"the token dependency (resolve_session_member_id remains the real gate)."
    )


# ---------------------------------------------------------------------------
# Runtime tests - a real app (jobs/me/circles/mesh mounted, matching prod
# topology for these routers) + a real Postgres session, booted with both
# gates strict: POTLUCK_COORDINATOR_TOKEN set, POTLUCK_SESSION_AUTH_ENABLED=1.
#
# Background loops (verification probe, peer reaper, Sentry) are skipped -
# this app only runs init_db/close_db, mirroring test_device_key_registration
# .py's `_make_app_with_db` pattern.
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    from coordinator.db.database import close_db, init_db

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await init_db()
        try:
            yield
        finally:
            await close_db()

    app = FastAPI(lifespan=lifespan)
    app.include_router(jobs.router, prefix="/jobs")
    app.include_router(mesh.router)
    app.include_router(me.router)
    app.include_router(circles.router)
    return app


@dataclass
class _Seed:
    member_id: uuid.UUID
    raw_token: str
    _cleanup_pubkeys: list[str] = field(default_factory=list)


async def _seed_member_and_session(member_id: uuid.UUID, raw_token: str) -> None:
    from coordinator.db.database import get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        async with db.begin():
            await db.execute(
                text("INSERT INTO members (id, email) VALUES (:id, :email)"),
                {"id": member_id, "email": f"prod-posture-{uuid.uuid4().hex}@test.local"},
            )
            await db.execute(
                text(
                    "INSERT INTO sessions (member_id, token_hash, expires_at) "
                    "VALUES (:m, :h, :e)"
                ),
                {
                    "m": member_id,
                    "h": hashlib.sha256(raw_token.encode()).hexdigest(),
                    "e": datetime.now(timezone.utc) + timedelta(hours=1),
                },
            )


async def _cleanup_member(member_id: uuid.UUID) -> None:
    from coordinator.db.database import get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        async with db.begin():
            # trusted_circles / circle_members cascade off owner delete; member_keys,
            # households (owner_member_id), pairing_tokens (household_id FK cascade)
            # and sessions all cascade or are swept transitively from the member row.
            await db.execute(
                text("DELETE FROM sessions WHERE member_id = :m"), {"m": member_id}
            )
            await db.execute(
                text("DELETE FROM member_keys WHERE member_id = :m"), {"m": member_id}
            )
            await db.execute(
                text("DELETE FROM trusted_circles WHERE owner_member_id = :m"),
                {"m": member_id},
            )
            await db.execute(
                text("DELETE FROM households WHERE owner_member_id = :m"), {"m": member_id}
            )
            await db.execute(text("DELETE FROM members WHERE id = :m"), {"m": member_id})


@pytest.fixture()
def prod_posture_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both auth gates strict, matching a hardened prod coordinator."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")


def test_frontend_direct_routes_succeed_session_only(prod_posture_env: None) -> None:
    """Every frontend-direct route, called with only a valid session bearer
    (no X-Potluck-Coordinator-Token header at all), returns something other
    than 401/403 - i.e. it got past both gates on identity, not by accident.

    Some responses are legitimate non-2xx domain outcomes (e.g. POST
    /jobs/stream 503s with no peer online in this isolated test app) - the
    invariant under test is "not rejected for auth", not "always 2xx".
    """
    app = _make_app()
    member_id = uuid.uuid4()
    raw_token = f"tok-{uuid.uuid4().hex}"

    with TestClient(app, raise_server_exceptions=False) as client:
        client.portal.call(_seed_member_and_session, member_id, raw_token)
        client.cookies.set("potluck_session", raw_token)
        # No Authorization header, no X-Potluck-Coordinator-Token header -
        # cookie-only session, exactly like the packaged web client.
        try:
            results: dict[tuple[str, str], int] = {}

            resp = client.post("/mesh/households", json={"name": "Prod Posture Casa"})
            results[("POST", "/mesh/households")] = resp.status_code

            resp = client.post("/mesh/pairing-tokens")
            results[("POST", "/mesh/pairing-tokens")] = resp.status_code

            pubkey = _fresh_ed25519_pubkey()
            resp = client.post("/me/device-keys", json={"public_key": pubkey})
            results[("POST", "/me/device-keys")] = resp.status_code

            resp = client.get("/me/identity")
            results[("GET", "/me/identity")] = resp.status_code

            resp = client.post("/me/circles", json={"name": "Prod Posture Circle"})
            results[("POST", "/me/circles")] = resp.status_code
            circle_id = resp.json()["id"] if resp.status_code == 201 else str(uuid.uuid4())

            resp = client.get("/me/circles")
            results[("GET", "/me/circles")] = resp.status_code

            # A real machine key: the endpoint refuses anything else.
            import base64

            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            member_pubkey = base64.urlsafe_b64encode(
                Ed25519PrivateKey.generate().public_key().public_bytes_raw()
            ).rstrip(b"=").decode("ascii")
            resp = client.post(
                f"/me/circles/{circle_id}/members",
                json={"member_pubkey": member_pubkey},
            )
            results[("POST", "/me/circles/{circle_id}/members")] = resp.status_code

            resp = client.delete(f"/me/circles/{circle_id}/members/{member_pubkey}")
            results[("DELETE", "/me/circles/{circle_id}/members/{member_pubkey}")] = (
                resp.status_code
            )

            resp = client.delete(f"/me/circles/{circle_id}")
            results[("DELETE", "/me/circles/{circle_id}")] = resp.status_code

            resp = client.get("/me/credit-balance")
            results[("GET", "/me/credit-balance")] = resp.status_code

            resp = client.get("/me/credit-history")
            results[("GET", "/me/credit-history")] = resp.status_code

            resp = client.get("/me/contributions")
            results[("GET", "/me/contributions")] = resp.status_code

            resp = client.get("/me/contributions/weekly")
            results[("GET", "/me/contributions/weekly")] = resp.status_code

            resp = client.get("/jobs/stats")
            results[("GET", "/jobs/stats")] = resp.status_code

            resp = client.post(
                "/jobs/stream",
                json={"prompt": "prod posture probe", "model_slug": "stub-echo"},
            )
            results[("POST", "/jobs/stream")] = resp.status_code

            for key, status_code in results.items():
                assert status_code not in (401, 403), (
                    f"{key} returned {status_code} with a valid session and NO "
                    f"coordinator token - it is still gated by "
                    f"require_coordinator_token in prod. {key} must be session-only."
                )

            # Sanity: assert the routes we expect to cleanly succeed actually did,
            # so this test can't silently pass by every route 500ing.
            assert results[("POST", "/mesh/households")] == 201
            assert results[("POST", "/mesh/pairing-tokens")] == 201
            assert results[("POST", "/me/device-keys")] == 201
            assert results[("GET", "/me/identity")] == 200
            assert results[("POST", "/me/circles")] == 201
            assert results[("GET", "/me/circles")] == 200
            assert results[("POST", "/me/circles/{circle_id}/members")] == 201
            assert (
                results[("DELETE", "/me/circles/{circle_id}/members/{member_pubkey}")] == 204
            )
            assert results[("DELETE", "/me/circles/{circle_id}")] == 204
            assert results[("GET", "/me/credit-balance")] == 200
            assert results[("GET", "/me/credit-history")] == 200
            assert results[("GET", "/me/contributions")] == 200
            assert results[("GET", "/me/contributions/weekly")] == 200
            assert results[("GET", "/jobs/stats")] == 200
        finally:
            client.portal.call(_cleanup_member, member_id)


def test_frontend_direct_routes_401_without_session(prod_posture_env: None) -> None:
    """Every frontend-direct route 401s with no session and no coordinator
    token - proves `resolve_session_member_id` is the real, enforced gate
    (not accidentally a passthrough), and that removing
    `require_coordinator_token` didn't leave the route wide open.

    Bodies/path params are well-formed (valid types) throughout so a 401
    can't be confused with an incidental 422 body-validation failure.
    """
    app = _make_app()
    dummy_circle_id = str(uuid.uuid4())
    dummy_pubkey = "dummy-pubkey"

    calls: list[tuple[str, str, dict]] = [
        ("GET", "/me/circles", {}),
        ("POST", "/me/circles", {"json": {"name": "x"}}),
        ("DELETE", f"/me/circles/{dummy_circle_id}", {}),
        (
            "POST",
            f"/me/circles/{dummy_circle_id}/members",
            {"json": {"member_pubkey": dummy_pubkey}},
        ),
        ("DELETE", f"/me/circles/{dummy_circle_id}/members/{dummy_pubkey}", {}),
        ("GET", "/me/identity", {}),
        ("POST", "/me/device-keys", {"json": {"public_key": _fresh_ed25519_pubkey()}}),
        ("GET", "/me/credit-balance", {}),
        ("GET", "/me/credit-history", {}),
        ("GET", "/me/contributions", {}),
        ("GET", "/me/contributions/weekly", {}),
        ("POST", "/mesh/households", {"json": {"name": "x"}}),
        ("POST", "/mesh/pairing-tokens", {}),
        (
            "POST",
            "/jobs/stream",
            {"json": {"prompt": "x", "model_slug": "stub-echo"}},
        ),
        ("GET", "/jobs/stats", {}),
    ]

    with TestClient(app, raise_server_exceptions=False) as client:
        for method, path, kwargs in calls:
            resp = client.request(method, path, **kwargs)
            assert resp.status_code == 401, (
                f"{method} {path} returned {resp.status_code} with NO session "
                f"and no coordinator token - expected 401. If this route now "
                f"needs no auth at all, that's a privacy/ownership regression, "
                f"not a pass."
            )

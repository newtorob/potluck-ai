"""Coordinator-token gate on GET /mesh/peers (LAUNCH-BLOCKER fix).

Previously `list_peers` was the one /mesh/* route missing
`dependencies=[Depends(require_coordinator_token)]` — every sibling route
(heartbeat, enroll, households, pairing-tokens, derp-token, node delete) had
it. That meant anyone holding ANY registered wg_public_key could enumerate a
full household roster from the internet-facing coordinator with no token at
all. This pins the fix.

Mounts the real router (not a thin re-implementation) so a future refactor
that drops the dependency trips this test. GET /mesh/peers requires a DB
lookup once past the auth gate, so this only exercises the auth-rejection
paths (401) — a live-DB roster-returns-data check belongs in the
mesh-enroll-smoke integration script alongside the other /mesh/peers
coverage, per test_mesh_schemas.py's docstring convention.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coordinator.routers.mesh import router

_TOKEN = "s3cret-coordinator-token"


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(_make_app(), raise_server_exceptions=False)


def test_mesh_peers_rejects_missing_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _TOKEN)
    resp = client.get("/mesh/peers", params={"wg_public_key": "A" * 44})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


def test_mesh_peers_rejects_wrong_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _TOKEN)
    resp = client.get(
        "/mesh/peers",
        params={"wg_public_key": "A" * 44},
        headers={"X-Potluck-Coordinator-Token": "wrong"},
    )
    assert resp.status_code == 401


def test_mesh_peers_dev_mode_passthrough_reaches_db_dependency(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No token configured -> dev passthrough. We don't have a live DB in
    this suite, so getting PAST the auth gate shows up as the DB-not-
    initialised RuntimeError (500) rather than 401 — proving the gate did
    NOT block the request, without requiring Postgres."""
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    resp = client.get("/mesh/peers", params={"wg_public_key": "A" * 44})
    assert resp.status_code != 401


def test_mesh_peers_correct_token_reaches_db_dependency(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Right token -> gate passes -> falls through to the DB dependency
    (which raises RuntimeError/500 here since no Postgres is initialised in
    this suite). Confirms the gate authorizes correct callers, not just
    rejects incorrect ones."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _TOKEN)
    resp = client.get(
        "/mesh/peers",
        params={"wg_public_key": "A" * 44},
        headers={"X-Potluck-Coordinator-Token": _TOKEN},
    )
    assert resp.status_code != 401

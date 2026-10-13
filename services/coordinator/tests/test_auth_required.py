"""Tests for the coordinator bearer-token auth dependency.

Covers four scenarios per protected endpoint:
1. No token configured (dev mode) → all requests pass through.
2. Token configured, correct header → 2xx / not 401.
3. Token configured, wrong header → 401.
4. Token configured, header absent → 401.

Also verifies /health is always reachable without auth.

Uses FastAPI's TestClient with a minimal app that mounts the real auth
dependency but mocks out database + domain logic so no Postgres is
required. For targeted auth tests we build a thin test app rather than
importing the full coordinator app (which requires DB connection on
startup).
"""
from __future__ import annotations

import os

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from coordinator.middleware.auth import require_coordinator_token

# ---------------------------------------------------------------------------
# Minimal test app - exercises the dependency in isolation
# ---------------------------------------------------------------------------


def _make_test_app() -> FastAPI:
    """Build a minimal FastAPI app with one protected and one unprotected endpoint."""
    app = FastAPI()

    @app.post("/protected", dependencies=[Depends(require_coordinator_token)])
    async def protected_endpoint() -> dict[str, str]:
        return {"result": "ok"}

    @app.get("/health")
    async def health_endpoint() -> dict[str, str]:
        return {"status": "ok"}

    return app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(_make_test_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Dev mode - env var unset
# ---------------------------------------------------------------------------


def test_dev_mode_no_token_env_allows_all(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """When POTLUCK_COORDINATOR_TOKEN is unset, every request is allowed."""
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    resp = client.post("/protected")
    assert resp.status_code == 200


def test_dev_mode_empty_token_env_allows_all(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """When POTLUCK_COORDINATOR_TOKEN is set to empty string, passthrough applies."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "")
    resp = client.post("/protected")
    assert resp.status_code == 200


def test_dev_mode_whitespace_only_env_allows_all(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace-only env var is treated as unset (stripped to empty)."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "   ")
    resp = client.post("/protected")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth enforced - correct token
# ---------------------------------------------------------------------------


def test_correct_token_passes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Correct token in header allows the request."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "supersecret")
    resp = client.post("/protected", headers={"X-Potluck-Coordinator-Token": "supersecret"})
    assert resp.status_code == 200


def test_correct_token_with_leading_whitespace_passes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token is stripped before comparison; leading/trailing spaces are ignored."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "  supersecret  ")
    resp = client.post("/protected", headers={"X-Potluck-Coordinator-Token": "supersecret"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth enforced - wrong or missing token
# ---------------------------------------------------------------------------


def test_wrong_token_returns_401(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrong token in header returns 401."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "supersecret")
    resp = client.post("/protected", headers={"X-Potluck-Coordinator-Token": "wrongtoken"})
    assert resp.status_code == 401


def test_missing_header_returns_401(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent header returns 401 when token is configured."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "supersecret")
    resp = client.post("/protected")
    assert resp.status_code == 401


def test_empty_header_value_returns_401(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty header value returns 401 when token is configured."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "supersecret")
    resp = client.post("/protected", headers={"X-Potluck-Coordinator-Token": ""})
    assert resp.status_code == 401


def test_partial_token_returns_401(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Partial token (prefix of expected) returns 401 - no substring match."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "supersecret")
    resp = client.post("/protected", headers={"X-Potluck-Coordinator-Token": "super"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /health is always reachable - no auth, any env var state
# ---------------------------------------------------------------------------


def test_health_reachable_dev_mode(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Health endpoint is reachable without auth when token env var is unset."""
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_reachable_with_token_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Health endpoint is reachable without auth even when token env var is set.

    /health deliberately has no auth dependency - it's a liveness probe.
    """
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "supersecret")
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_reachable_without_header_when_token_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Health is unauthenticated - no header needed even with token configured."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "supersecret")
    resp = client.get("/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Error response shape
# ---------------------------------------------------------------------------


def test_401_response_shape(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """401 response has the expected detail field."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "supersecret")
    resp = client.post("/protected")
    assert resp.status_code == 401
    body = resp.json()
    assert "detail" in body
    assert body["detail"] == "unauthorized"

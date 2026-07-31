"""Regression test — 2026-07-31 P1 security audit, finding #2.

peer_access_middleware (potluck_local/app.py) exempted every /peer/* path
from the sidecar-token check unconditionally. That exemption is only valid
for a genuine mesh peer, whose credential is "reachable only from a source
IP inside the WireGuard mesh CIDR" (enforced by the mesh-CIDR gate a few
lines above, which runs ONLY for non-loopback callers). A LOOPBACK caller
hitting /peer/* never goes through that mesh-CIDR gate at all (loopback is
exempt from it), so combined with the unconditional /peer/* token exemption,
a loopback caller got the full peer surface — including
POST /peer/memory/search, which returns full memory content — with NO token
and with an INCORRECT token. That's the private-memory leak the auditor
reproduced.

The fix: the /peer/* exemption from the sidecar-token gate applies only to
non-loopback callers (who already proved mesh membership above). A loopback
caller must present the sidecar token like every other loopback route.

Isolated: no real ~/.potluck touched (these are pure middleware/auth checks,
no filesystem I/O), and the app is rebuilt fresh (create_app()) per test with
POTLUCK_SIDECAR_TOKEN set via monkeypatch BEFORE construction (the token is
read once, at create_app() time).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app

TOKEN = "s3cret-sidecar-token"
MESH_PEER = ("100.64.0.9", 40000)  # inside the default mesh CIDR (100.64.0.0/10)
LOCAL = ("127.0.0.1", 50000)


@pytest.fixture()
def app_with_token(monkeypatch):
    monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", TOKEN)
    monkeypatch.setenv("POTLUCK_PEER_MEMORY_ENABLED", "1")
    return create_app()


@pytest.fixture()
def loopback_client(app_with_token):
    return TestClient(app_with_token, client=LOCAL, raise_server_exceptions=False)


@pytest.fixture()
def mesh_client(app_with_token):
    return TestClient(app_with_token, client=MESH_PEER, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# The launch-blocker: loopback + /peer/* + token configured.
# ---------------------------------------------------------------------------


def test_loopback_peer_memory_search_with_no_token_is_rejected(loopback_client):
    """The auditor's exact repro #1: no Authorization header at all."""
    resp = loopback_client.post("/peer/memory/search", json={"query": "test"})
    assert resp.status_code == 401
    # And no memory content leaked in the rejection body.
    assert "memories" not in resp.json()


def test_loopback_peer_memory_search_with_wrong_token_is_rejected(loopback_client):
    """The auditor's exact repro #2: an incorrect bearer token."""
    resp = loopback_client.post(
        "/peer/memory/search",
        json={"query": "test"},
        headers={"Authorization": "Bearer not-the-real-token"},
    )
    assert resp.status_code == 401
    assert "memories" not in resp.json()


def test_loopback_peer_memory_search_with_correct_token_is_not_blocked_by_auth(
    loopback_client,
):
    """A loopback caller that DOES present the right token is not 401'd by
    the auth gate (it may still get a different status from the router
    depending on other settings, but never 401)."""
    resp = loopback_client.post(
        "/peer/memory/search",
        json={"query": "test"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code != 401


def test_loopback_peer_capabilities_requires_token(loopback_client):
    """Not just the memory-search route — the whole /peer/* surface must
    require the token from a loopback caller once one is configured."""
    resp = loopback_client.get("/peer/capabilities")
    assert resp.status_code == 401

    resp_ok = loopback_client.get(
        "/peer/capabilities", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert resp_ok.status_code != 401


# ---------------------------------------------------------------------------
# Must NOT regress: a genuine mesh peer keeps its pre-existing token-free
# path (its credential is mesh-CIDR membership, proven above the token gate).
# ---------------------------------------------------------------------------


def test_mesh_peer_still_reaches_peer_routes_without_a_token(mesh_client):
    resp = mesh_client.post("/peer/memory/search", json={"query": "test"})
    assert resp.status_code != 401

    resp2 = mesh_client.get("/peer/capabilities")
    assert resp2.status_code != 401


# ---------------------------------------------------------------------------
# Must NOT regress: loopback still reaches non-/peer routes exactly as
# before (token required, /health and /shutdown exempt).
# ---------------------------------------------------------------------------


def test_loopback_health_stays_exempt_from_token(loopback_client):
    assert loopback_client.get("/health").status_code == 200


def test_loopback_non_peer_route_still_requires_token(loopback_client):
    resp = loopback_client.get("/memory/projects")
    assert resp.status_code == 401

    resp_ok = loopback_client.get(
        "/memory/projects", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert resp_ok.status_code != 401

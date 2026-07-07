"""Locks the sidecar's #1 security guarantee.

The sidecar binds 0.0.0.0 so household peers can dial it over the WireGuard
mesh, so peer_access_middleware (potluck_local/app.py) is the ENTIRE access
boundary: a non-loopback caller may reach only /peer/* and /health, never the
local user's memory / tasks / chat / agents / settings / vault.

That boundary was previously unlocked by tests (every other test uses a loopback
client to get PAST the guard). These tests impersonate a peer by handing the
TestClient a non-loopback client address, and assert the deny/allow split — so a
future refactor of the middleware can't silently open the private surface.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app

# TestClient lets us set the simulated socket peer address; the middleware keys
# its trust decision off exactly this.
PEER = ("10.0.0.9", 40000)  # a household mesh peer (non-loopback)
LOCAL = ("127.0.0.1", 50000)  # the local user (Electron / dev tools)

# A representative sample of the private, loopback-only surface. The middleware
# runs before routing, so 403 is returned whether or not the route exists.
PRIVATE_PATHS = ["/memory/projects", "/tasks", "/agents", "/settings", "/vault"]


@pytest.fixture(scope="module")
def app():
    return create_app()


@pytest.fixture()
def peer_client(app):
    return TestClient(app, client=PEER, raise_server_exceptions=False)


@pytest.fixture()
def local_client(app):
    return TestClient(app, client=LOCAL, raise_server_exceptions=False)


@pytest.mark.parametrize("path", PRIVATE_PATHS)
def test_peer_cannot_reach_private_paths(peer_client, path):
    assert peer_client.get(path).status_code == 403


def test_peer_can_reach_health(peer_client):
    assert peer_client.get("/health").status_code == 200


def test_peer_can_reach_peer_surface(peer_client):
    # May be 200 or 503 depending on hardware/serving state, but crucially the
    # boundary does NOT block it.
    assert peer_client.get("/peer/capabilities").status_code != 403


def test_peer_cannot_escape_via_path_traversal(peer_client):
    # /peer/../memory must canonicalize to /memory and be denied, rather than
    # slipping through the startswith("/peer/") check. Guards the middleware's
    # posixpath.normpath step (Gate 0).
    assert peer_client.get("/peer/../memory/projects").status_code == 403


def test_peer_rejects_null_byte_path(peer_client):
    assert peer_client.get("/memory/%00").status_code in (400, 403, 404)


@pytest.mark.parametrize("path", PRIVATE_PATHS)
def test_local_caller_is_not_blocked_by_peer_gate(local_client, path):
    # The same paths are not peer-403'd for loopback. They may return their own
    # status (200 / 401 / 404 / 500), but never the peer boundary's 403.
    assert local_client.get(path).status_code != 403

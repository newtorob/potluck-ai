"""Locks the sidecar's #1 security guarantee.

The sidecar binds 0.0.0.0 so household peers can dial it over the WireGuard
mesh, so peer_access_middleware (potluck_local/app.py) is the entire access
boundary: a non-loopback caller may reach only /peer/* and /health, never the
local user's memory / tasks / chat / agents / settings / vault.

Every other test uses a loopback client to get past the guard. These tests
impersonate a peer by handing the TestClient a non-loopback client address, and
assert the deny/allow split - so a future refactor of the middleware can't
silently open the private surface.

"Non-loopback" is not the same claim as "on the WireGuard mesh": 0.0.0.0 binds
the bare socket to every interface, so a bystander on the same LAN/Wi-Fi is also
"non-loopback" without ever having joined the household's mesh. /peer/* requires
a source IP inside the mesh CIDR (default 100.64.0.0/10, see app.py's
_resolve_mesh_network / _DEFAULT_MESH_CIDR) and household membership as this
machine's mesh sidecar reports it (services/peer_trust.py). These tests cover
the member allow case and the LAN deny case; tests/test_peer_trust.py covers
the membership rules in depth. The `fake_mesh` fixture stands in for the sidecar.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app

# TestClient lets us set the simulated socket peer address; the middleware keys
# its trust decision off exactly this.
MESH_PEER = ("100.64.0.9", 40000)  # inside the default mesh CIDR (100.64.0.0/10) -- a real household peer
LAN_PEER = ("192.168.1.50", 40000)  # non-loopback, but not on the mesh -- e.g. a coffee-shop/dorm neighbor
LOCAL = ("127.0.0.1", 50000)  # the local user (Electron / dev tools)

# A representative sample of the private, loopback-only surface. The middleware
# runs before routing, so 403 is returned whether or not the route exists.
PRIVATE_PATHS = ["/memory/projects", "/tasks", "/agents", "/settings", "/vault", "/usage/dashboard"]

# The peer-facing surface that must be mesh-CIDR gated (not merely non-loopback
# gated) -- capabilities probe, stateless inference dispatch, and (opt-in)
# full-content memory search are the exact endpoints that would be exposed to
# any device on the LAN without the gate.
PEER_SURFACE_PATHS = ["/peer/capabilities"]


@pytest.fixture(scope="module")
def app():
    return create_app()


@pytest.fixture()
def mesh_peer_client(app, fake_mesh):
    # A household peer: listed as reachable by this machine's sidecar and
    # dialing this machine's own mesh address.
    fake_mesh.add_peer(MESH_PEER[0])
    return TestClient(app, client=MESH_PEER, base_url=fake_mesh.base_url, raise_server_exceptions=False)


@pytest.fixture()
def lan_peer_client(app, fake_mesh):
    # The sidecar is up and knows the household; the LAN device is not in it.
    fake_mesh.add_peer(MESH_PEER[0])
    return TestClient(app, client=LAN_PEER, raise_server_exceptions=False)


@pytest.fixture()
def local_client(app):
    return TestClient(app, client=LOCAL, raise_server_exceptions=False)


@pytest.mark.parametrize("path", PRIVATE_PATHS)
def test_mesh_peer_cannot_reach_private_paths(mesh_peer_client, path):
    assert mesh_peer_client.get(path).status_code == 403


@pytest.mark.parametrize("path", PRIVATE_PATHS)
def test_lan_peer_cannot_reach_private_paths(lan_peer_client, path):
    assert lan_peer_client.get(path).status_code == 403


def test_mesh_peer_can_reach_health(mesh_peer_client):
    resp = mesh_peer_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok" and "version" in resp.json()


def test_lan_peer_gets_only_liveness_from_health(lan_peer_client):
    # /health stays open to any caller as a liveness probe, but a caller that
    # is not a household member gets nothing else: no version, no loaded model,
    # no other machines' mesh addresses (loaded_rpc_servers).
    resp = lan_peer_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_mesh_peer_can_reach_peer_surface(mesh_peer_client):
    # May be 200 or 503 depending on hardware/serving state, but crucially the
    # boundary does not block it -- the source IP is inside the mesh CIDR.
    assert mesh_peer_client.get("/peer/capabilities").status_code != 403


@pytest.mark.parametrize("path", PEER_SURFACE_PATHS)
def test_lan_peer_cannot_reach_peer_surface(lan_peer_client, path):
    # A non-mesh LAN caller (dorm/apartment/coffee-shop neighbor) must be
    # rejected before reaching any /peer/* handler, even though it's
    # non-loopback and would pass a plain "not loopback" check.
    assert lan_peer_client.get(path).status_code == 403


def test_lan_peer_cannot_reach_peer_memory_search(lan_peer_client, monkeypatch):
    # Full-content memory search is the highest-severity leak if the gate
    # fails (peer.py returns m.model_dump() -- complete memory rows). Confirm
    # the mesh-CIDR gate 403s it in the middleware, before the router even runs
    # (so this must 403 regardless of POTLUCK_PEER_MEMORY_ENABLED).
    monkeypatch.setenv("POTLUCK_PEER_MEMORY_ENABLED", "1")
    resp = lan_peer_client.post("/peer/memory/search", json={"query": "test"})
    assert resp.status_code == 403


def test_mesh_peer_memory_search_not_blocked_by_mesh_gate(mesh_peer_client, monkeypatch):
    # A genuine mesh peer must not be 403'd by the mesh-CIDR gate itself. It may
    # still get a different status from the router (e.g. 403 disabled-memory,
    # 503 opted out) depending on other settings, but never the mesh-boundary 403.
    monkeypatch.setenv("POTLUCK_PEER_MEMORY_ENABLED", "1")
    resp = mesh_peer_client.post("/peer/memory/search", json={"query": "test"})
    assert resp.status_code != 403 or "household's mesh" not in resp.json().get("detail", "")


def test_peer_cannot_escape_via_path_traversal(mesh_peer_client):
    # /peer/../memory must canonicalize to /memory and be denied, rather than
    # slipping through the startswith("/peer/") check. Guards the middleware's
    # posixpath.normpath step (Gate 0).
    assert mesh_peer_client.get("/peer/../memory/projects").status_code == 403


def test_peer_rejects_null_byte_path(mesh_peer_client):
    assert mesh_peer_client.get("/memory/%00").status_code in (400, 403, 404)


@pytest.mark.parametrize("path", PRIVATE_PATHS)
def test_local_caller_is_not_blocked_by_peer_gate(local_client, path):
    # The same paths are not peer-403'd for loopback. They may return their own
    # status (200 / 401 / 404 / 500), but never the peer boundary's 403.
    assert local_client.get(path).status_code != 403


def test_local_caller_reaches_peer_surface_regardless_of_mesh_cidr(local_client):
    # Loopback callers (Electron, dev tools) are exempt from the mesh-CIDR check
    # entirely -- the check only applies to non-loopback callers.
    assert local_client.get("/peer/capabilities").status_code != 403


def test_mesh_cidr_env_override(monkeypatch, fake_mesh):
    # POTLUCK_MESH_CIDR lets an operator point the gate at a non-default mesh
    # allocation. A member inside the overridden CIDR passes; the same kind of
    # address would have been rejected under the default 100.64.0.0/10 pool,
    # and a default-range address is no longer eligible even if it is listed.
    monkeypatch.setenv("POTLUCK_MESH_CIDR", "10.42.0.0/16")
    fake_mesh.own_ip = "10.42.0.1"
    fake_mesh.add_peer("10.42.0.9")
    fake_mesh.add_peer("100.64.0.9")
    app = create_app()
    custom_mesh_client = TestClient(
        app, client=("10.42.0.9", 40000), base_url="http://10.42.0.1:8321", raise_server_exceptions=False
    )
    default_mesh_client = TestClient(
        app, client=("100.64.0.9", 40000), base_url="http://10.42.0.1:8321", raise_server_exceptions=False
    )

    assert custom_mesh_client.get("/peer/capabilities").status_code != 403
    # Under the override, the default mesh CIDR is no longer trusted.
    assert default_mesh_client.get("/peer/capabilities").status_code == 403


def test_every_engine_server_ignores_forwarded_headers():
    # uvicorn's default takes the caller's address from X-Forwarded-For when the
    # connection comes from 127.0.0.1. With that on, a local process sending
    # "X-Forwarded-For: 100.64.0.9" over loopback looked like a mesh peer and
    # skipped the token that loopback /peer/* requires. Every server the engine
    # starts must turn it off.
    import ast
    import inspect

    import potluck_local.app as app_module

    servers = [
        node
        for node in ast.walk(ast.parse(inspect.getsource(app_module)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
        and node.func.attr in ("run", "Config")
    ]
    assert len(servers) == 2
    for call in servers:
        options = {keyword.arg: keyword.value for keyword in call.keywords}
        setting = options.get("proxy_headers")
        assert isinstance(setting, ast.Constant) and setting.value is False

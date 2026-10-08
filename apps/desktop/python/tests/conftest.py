"""Suite-wide isolation.

The engine defaults to the public coordinator and reads the member's sign-in
from the data directory. A unit test must never do either with the real ones,
so every test starts with an unroutable coordinator and an empty data
directory. Tests that need something else set it themselves.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _isolated_engine_environment(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("POTLUCK_COORDINATOR_HTTP_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path_factory.mktemp("potluck-data")))


@pytest.fixture(autouse=True)
def _no_real_mesh_sidecar(monkeypatch, tmp_path_factory):
    """No test reads the developer's own mesh sidecar or a cached roster.

    The peer gate asks the local mesh sidecar who is in the household. A test
    that does not set up a roster gets "no sidecar", and the shared roster
    cache starts empty and is dropped afterwards.
    """
    from potluck_local.services import peer_trust

    monkeypatch.setenv(
        "POTLUCK_LOCAL_API_SOCKET", str(tmp_path_factory.mktemp("no-mesh") / "absent.sock")
    )
    peer_trust.reset()
    yield
    peer_trust.reset()


class FakeMesh:
    """Stands in for this machine's mesh sidecar (`/status` and `/peers`)."""

    def __init__(self) -> None:
        self.own_ip: object = "100.64.0.4"
        # Where a household peer's request is accepted: this machine's own mesh
        # address. Pass it to TestClient(base_url=...) so scope["server"] is it.
        self.base_url = "http://100.64.0.4:8321"
        self.peers: list[object] = []
        self.status_body: object = ...  # set to replace the whole /status body
        self.peers_body: object = ...  # set to replace the whole /peers body
        self.down = False
        self.delay = 0.0  # seconds each read takes, so concurrent requests overlap
        self.calls: list[str] = []

    def add_peer(self, ip: str, *, reachable: object = True, **extra: object) -> None:
        self.peers.append(
            {"node_id": f"node-{ip}", "node_name": f"machine-{ip}", "mesh_ip": ip,
             "wg_public_key": "key", "reachable": reachable, **extra}
        )

    def remove_peer(self, ip: str) -> None:
        self.peers = [p for p in self.peers if not (isinstance(p, dict) and p.get("mesh_ip") == ip)]

    async def get(self, path: str, *, timeout: float = 2.0) -> object:
        from potluck_local.services.mesh_ipc import MeshUnavailable

        self.calls.append(path)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.down:
            raise MeshUnavailable("mesh sidecar is not running")
        if path == "/status":
            if self.status_body is not ...:
                return self.status_body
            return {"mesh_status": "connected", "mesh_ip": self.own_ip}
        if path == "/peers":
            return self.peers_body if self.peers_body is not ... else {"peers": self.peers}
        raise MeshUnavailable("unexpected path")

    def reads(self) -> int:
        """How many times the roster was read (each read asks /status first)."""
        return self.calls.count("/status")


@pytest.fixture()
def fake_mesh(monkeypatch):
    from potluck_local.services import mesh_ipc

    mesh = FakeMesh()
    monkeypatch.setattr(mesh_ipc, "mesh_get", mesh.get)
    return mesh

"""WS coordinator-token gate on /mesh/signal.

The signaling channel requires the coordinator token (when configured),
mirroring /peers/connect - without it anyone with any enrolled WG pubkey could
join. These exercise the gate via a minimal app mounting the real handler. Both
the rejection path and the hello-validation path run before any DB access, so
no Postgres is needed: a bad/absent token is rejected at the gate, and a
malformed hello (used to prove the gate was passed) fails at hello-validation.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from coordinator.routers.mesh import mesh_signal

_TOKEN = "s3cret-coordinator-token"


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_api_websocket_route("/mesh/signal", mesh_signal)
    return app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(_make_app())


def test_rejects_when_token_configured_but_absent(client, monkeypatch):
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _TOKEN)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/mesh/signal") as ws:
            msg = ws.receive_json()
            assert msg["code"] == "unauthorized"
            ws.receive_json()  # server closed 1008 -> disconnect


def test_rejects_when_token_configured_but_wrong(client, monkeypatch):
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _TOKEN)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/mesh/signal", headers={"X-Potluck-Coordinator-Token": "wrong"}
        ) as ws:
            msg = ws.receive_json()
            assert msg["code"] == "unauthorized"
            ws.receive_json()


def test_correct_token_passes_gate(client, monkeypatch):
    """With the right token the gate passes; a malformed hello then fails at
    hello-validation (not 'unauthorized'), proving we got past the gate."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _TOKEN)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/mesh/signal", headers={"X-Potluck-Coordinator-Token": _TOKEN}
        ) as ws:
            ws.send_json({"type": "hello"})  # missing wg_public_key
            msg = ws.receive_json()
            assert msg["code"] == "hello_invalid"  # gate passed; hello rejected
            ws.receive_json()


def test_dev_mode_passthrough_when_token_unset(client, monkeypatch):
    """No token configured -> dev passthrough; a malformed hello reaches
    hello-validation, confirming the gate did not block."""
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/mesh/signal") as ws:
            ws.send_json({"type": "hello"})  # malformed
            msg = ws.receive_json()
            assert msg["code"] == "hello_invalid"  # not unauthorized -> passthrough
            ws.receive_json()

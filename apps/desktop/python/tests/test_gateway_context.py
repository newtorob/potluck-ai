"""Context discovery follows owned-machine chat routing without running a model."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from potluck_local.routers import gateway
from potluck_local.services.model_identity import ModelIdentityError

SHA = "a" * 64


class Engine:
    def is_loaded(self): return True
    def current_model_id(self): return "local-model"
    def loaded_n_ctx(self): return 8192
    def template_traits(self): return None

    def assert_model_identity(self, model, digest):
        if model != "local-model" or digest != SHA:
            raise ModelIdentityError("Model does not match")


@pytest.fixture
def setup(monkeypatch):
    runtime = Engine()
    monkeypatch.setattr(gateway, "get_runtime", lambda: runtime)
    peer = AsyncMock(return_value=None)
    monkeypatch.setattr(gateway.gateway_routing, "peer_for_model", peer)
    app = FastAPI()
    app.include_router(gateway.router, prefix="/v1")
    return TestClient(app), runtime, peer


def test_local_window_is_loaded_capacity_not_model_maximum(setup):
    client, _, peer = setup
    response = client.get("/v1/potluck/context", params={"model": "local-model", "scope": "mine"})
    assert response.status_code == 200
    assert response.json() == {"model": "local-model", "scope": "mine", "context_window": 8192, "source": "local"}
    peer.assert_not_awaited()


def test_household_window_uses_selected_model_and_omits_machine_identity(setup):
    client, _, peer = setup
    peer.return_value = {"node_name": "private-name", "mesh_ip": "100.64.0.4", "_capabilities": {
        "loaded_model_detail": {"context_size": 16384},
    }}
    response = client.get("/v1/potluck/context", params={"model": "remote-model", "scope": "mine"})
    assert response.status_code == 200
    assert response.json() == {"model": "remote-model", "scope": "mine", "context_window": 16384, "source": "household"}
    assert "private-name" not in response.text and "100.64" not in response.text
    peer.assert_awaited_once_with("remote-model")


@pytest.mark.parametrize("window", [None, True, 0, -1, "8192"])
def test_unknown_or_invalid_window_stays_unknown(setup, window):
    client, runtime, _ = setup
    runtime.loaded_n_ctx = lambda: window
    response = client.get("/v1/potluck/context", params={"model": "local-model"})
    assert response.status_code == 200
    assert response.json()["context_window"] is None


def test_local_scope_never_looks_for_a_household_model(setup):
    client, _, peer = setup
    assert client.get("/v1/potluck/context", params={"model": "remote-model", "scope": "local"}).status_code == 409
    peer.assert_not_awaited()


@pytest.mark.parametrize("scope", ["pool", "circle", "anything"])
def test_context_lookup_never_broadens_routing_scope(setup, scope):
    client, _, peer = setup
    assert client.get("/v1/potluck/context", params={"model": "local-model", "scope": scope}).status_code == 422
    peer.assert_not_awaited()


def test_exact_model_lock_is_checked_before_reporting_a_window(setup):
    client, _, peer = setup
    response = client.get("/v1/potluck/context", params={
        "model": "remote-model", "scope": "mine", "potluck_model_sha256": SHA,
    })
    assert response.status_code == 409
    peer.assert_awaited_once_with("remote-model", SHA)


def test_older_household_without_context_advertisement_stays_unknown(setup):
    client, _, peer = setup
    peer.return_value = {"_capabilities": {}}
    response = client.get("/v1/potluck/context", params={"model": "remote-model", "scope": "mine"})
    assert response.status_code == 200 and response.json()["context_window"] is None


def test_locked_household_route_reports_its_window(setup):
    client, _, peer = setup
    peer.return_value = {"_capabilities": {"loaded_model_detail": {"context_size": 32768, "sha256": SHA}}}
    response = client.get("/v1/potluck/context", params={
        "model": "remote-model", "scope": "mine", "potluck_model_sha256": SHA,
    })
    assert response.status_code == 200 and response.json()["context_window"] == 32768
    peer.assert_awaited_once_with("remote-model", SHA)

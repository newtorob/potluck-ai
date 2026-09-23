import os

import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app
from potluck_local.services import settings_manager as settings


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", "control-token")
    return TestClient(create_app(), client=("127.0.0.1", 1234),
                      headers={"Authorization": "Bearer control-token"})


def test_enable_creates_one_key_and_preserves_all_other_settings(client):
    initial = settings.PotluckSettings()
    initial.gateway.pool_routing = True
    initial.ui = {"theme": "dark"}
    settings.save_settings(initial)
    first = client.post("/settings/gateway/enable")
    assert first.status_code == 200
    assert "api_key" not in first.json()
    enabled = settings.get_settings()
    assert enabled.gateway.enabled
    assert enabled.gateway.api_key
    key = enabled.gateway.api_key
    assert client.post("/settings/gateway/enable").status_code == 200
    again = settings.get_settings()
    assert again.gateway.api_key == key
    assert again.gateway.pool_routing
    assert again.ui == {"theme": "dark"}
    assert again.network == initial.network  # No contribution or sharing opt-ins.


def test_enable_reuses_disabled_key_but_rotate_invalidates_it(client):
    initial = settings.PotluckSettings()
    initial.gateway.api_key = "keep-key"
    initial.gateway.enabled = False
    settings.save_settings(initial)
    assert client.post("/settings/gateway/enable").status_code == 200
    assert settings.get_settings().gateway.api_key == "keep-key"
    assert client.post("/settings/gateway/rotate").status_code == 200
    assert settings.get_settings().gateway.api_key != "keep-key"


def test_gateway_key_cannot_authorize_management(client):
    assert client.post("/settings/gateway/enable", headers={"Authorization": "Bearer gateway-key"}).status_code == 401


def test_failed_atomic_save_keeps_previous_config(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    original = settings.PotluckSettings(ui={"theme": "dark"})
    settings.save_settings(original, path)
    before = path.read_bytes()
    def fail(*_args):
        raise OSError("simulated interrupted replacement")
    monkeypatch.setattr(settings.os, "replace", fail)
    with pytest.raises(OSError):
        settings.save_settings(settings.PotluckSettings(ui={"theme": "light"}), path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".config-*"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600

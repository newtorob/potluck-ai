"""Tests for the ui dict field on PotluckSettings and the PATCH merge behavior.

The ui dict is a generic string→string bag owned by the frontend (theme,
routing scope, sidebar state, etc.). Two invariants are tested:

1. Service layer: the field exists, defaults to {}, and round-trips through
   save/get without data loss.
2. Router layer: PATCH {ui: {k: v}} merges into the existing dict - it must
   never replace the whole dict, so writing one preference never silently
   drops another.
"""

from __future__ import annotations

from pathlib import Path

import potluck_local.routers.settings as router_mod
import potluck_local.services.settings_manager as sm
from fastapi.testclient import TestClient
from potluck_local.app import create_app
from potluck_local.services.settings_manager import PotluckSettings, get_settings, save_settings


# ---------------------------------------------------------------------------
# Service-layer (settings_manager) tests
# ---------------------------------------------------------------------------


def test_potluck_settings_ui_defaults_to_empty_dict() -> None:
    """PotluckSettings().ui is an empty dict, not None."""
    s = PotluckSettings()
    assert s.ui == {}


def test_ui_round_trips_through_save_and_get(tmp_path: Path) -> None:
    """Values written to ui survive a save→get cycle."""
    cfg = tmp_path / "config.json"
    s = PotluckSettings(ui={"potluck.theme": "light", "potluck.routing_scope": "household"})
    save_settings(s, cfg)
    loaded = get_settings(cfg)
    assert loaded.ui["potluck.theme"] == "light"
    assert loaded.ui["potluck.routing_scope"] == "household"


def test_ui_keys_merge_across_multiple_saves(tmp_path: Path) -> None:
    """Two sequential saves with different keys both survive."""
    cfg = tmp_path / "config.json"
    s1 = PotluckSettings(ui={"potluck.theme": "dark"})
    save_settings(s1, cfg)
    s2 = get_settings(cfg)
    s2.ui["potluck.sidebar_collapsed"] = "true"
    save_settings(s2, cfg)
    final = get_settings(cfg)
    assert final.ui["potluck.theme"] == "dark"
    assert final.ui["potluck.sidebar_collapsed"] == "true"


# ---------------------------------------------------------------------------
# Router (PATCH /settings) tests - the critical merge assertion
# ---------------------------------------------------------------------------


# TestClient client= tuple that satisfies the peer_access_middleware loopback check.
_LOOPBACK = ("127.0.0.1", 50000)


def _make_client(tmp_path: Path, monkeypatch: object) -> TestClient:
    """Create a TestClient whose GET/PATCH /settings use a tmp config file.

    Passes client=_LOOPBACK so peer_access_middleware treats the request as a
    loopback caller and does not return 403 on non-peer routes.
    """
    cfg = tmp_path / "config.json"
    # Patch at the router module level (names imported from settings_manager).
    monkeypatch.setattr(router_mod, "get_settings", lambda: get_settings(cfg))
    monkeypatch.setattr(router_mod, "save_settings", lambda s: save_settings(s, cfg))
    # Clear the mtime cache so stale entries from other tests do not bleed in.
    monkeypatch.setattr(sm, "_settings_cache", {})
    return TestClient(create_app(), client=_LOOPBACK)


def test_patch_ui_dict_does_not_wipe_existing_keys(tmp_path: Path, monkeypatch: object) -> None:
    """Two sequential PATCHes to different ui keys both survive.

    This guards against a shallow replace, which would make the second PATCH
    overwrite the entire ui dict with only its key.
    """
    client = _make_client(tmp_path, monkeypatch)
    with client:
        r1 = client.patch("/settings", json={"ui": {"potluck.theme": "light"}})
        assert r1.status_code == 200
        assert r1.json()["ui"] == {"potluck.theme": "light"}

        r2 = client.patch("/settings", json={"ui": {"potluck.routing_scope": "household"}})
        assert r2.status_code == 200
        body = r2.json()
        # Both keys must coexist - the theme must not have been dropped.
        assert body["ui"].get("potluck.theme") == "light", (
            "second PATCH wiped potluck.theme from the ui dict"
        )
        assert body["ui"].get("potluck.routing_scope") == "household"


def test_patch_ui_key_overwrites_existing_value(tmp_path: Path, monkeypatch: object) -> None:
    """Patching the same key twice updates the value, not duplicates it."""
    client = _make_client(tmp_path, monkeypatch)
    with client:
        client.patch("/settings", json={"ui": {"potluck.theme": "dark"}})
        r = client.patch("/settings", json={"ui": {"potluck.theme": "light"}})
        assert r.status_code == 200
        assert r.json()["ui"]["potluck.theme"] == "light"


def test_get_settings_includes_ui_after_patches(tmp_path: Path, monkeypatch: object) -> None:
    """GET /settings reflects all previously patched ui keys."""
    client = _make_client(tmp_path, monkeypatch)
    with client:
        client.patch("/settings", json={"ui": {"potluck.theme": "light"}})
        client.patch("/settings", json={"ui": {"potluck.sidebar_collapsed": "true"}})

        r = client.get("/settings")
        assert r.status_code == 200
        ui = r.json()["ui"]
        assert ui.get("potluck.theme") == "light"
        assert ui.get("potluck.sidebar_collapsed") == "true"


def test_patch_ui_alongside_other_sections(tmp_path: Path, monkeypatch: object) -> None:
    """PATCH that touches both ui and another section leaves both correct."""
    client = _make_client(tmp_path, monkeypatch)
    with client:
        r = client.patch(
            "/settings",
            json={
                "ui": {"potluck.theme": "dark"},
                "network": {"coordinator_url": "http://10.0.0.1:8400"},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ui"].get("potluck.theme") == "dark"
        assert body["network"]["coordinator_url"] == "http://10.0.0.1:8400"

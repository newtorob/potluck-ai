"""Tests for GET /contributor/device-key.

Covers the desktop-side half of the device-key registration gap: the
sidecar must expose the SAME Ed25519 public key `contributor.client`
presents in its WS attestation handshake, so the frontend can pre-register
it with the coordinator's session-authed POST /me/device-keys before
REQUIRE_REGISTERED_KEYS=true rejects a fresh machine's attestation.
"""
from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from potluck_local.app import create_app

# TestClient's default host isn't a loopback literal, which trips
# peer_access_middleware's non-loopback gate. Match the rest of the suite
# (see test_settings_ui.py / test_readiness_gpu.py) with an explicit
# loopback client address.
_LOOPBACK = ("127.0.0.1", 54321)


def _decode(pubkey_b64url: str) -> bytes:
    padded = pubkey_b64url + "=" * (-len(pubkey_b64url) % 4)
    return base64.urlsafe_b64decode(padded)


def test_device_key_endpoint_returns_valid_ed25519_pubkey(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("POTLUCK_KEY_DIR", str(tmp_path / "contributor"))
    # Settings is lru_cache'd — clear so the env var above actually takes effect.
    from potluck_local.contributor.config import get_settings as get_contributor_settings

    get_contributor_settings.cache_clear()

    app = create_app()
    with TestClient(app, client=_LOOPBACK) as client:
        resp = client.get("/contributor/device-key")
        assert resp.status_code == 200
        body = resp.json()
        assert "public_key" in body
        raw = _decode(body["public_key"])
        assert len(raw) == 32  # raw Ed25519 public key length

    get_contributor_settings.cache_clear()


def test_device_key_is_stable_across_calls(tmp_path, monkeypatch) -> None:
    """Same key_dir -> same key on every call (peer identity must be stable)."""
    monkeypatch.setenv("POTLUCK_KEY_DIR", str(tmp_path / "contributor"))
    from potluck_local.contributor.config import get_settings as get_contributor_settings

    get_contributor_settings.cache_clear()

    app = create_app()
    with TestClient(app, client=_LOOPBACK) as client:
        first = client.get("/contributor/device-key").json()["public_key"]
        second = client.get("/contributor/device-key").json()["public_key"]
        assert first == second

    get_contributor_settings.cache_clear()


def test_device_key_matches_contributor_client_identity(tmp_path, monkeypatch) -> None:
    """The key this endpoint returns is exactly what `ensure_keypair` on the
    same key_dir would hand `contributor.client` — i.e. registering THIS key
    with the coordinator actually unblocks THAT attestation handshake."""
    key_dir = tmp_path / "contributor"
    monkeypatch.setenv("POTLUCK_KEY_DIR", str(key_dir))
    from potluck_local.contributor.config import get_settings as get_contributor_settings
    from potluck_local.contributor.keys import ensure_keypair, public_key_b64

    get_contributor_settings.cache_clear()

    app = create_app()
    with TestClient(app, client=_LOOPBACK) as client:
        endpoint_pubkey = client.get("/contributor/device-key").json()["public_key"]

    direct_priv = ensure_keypair(key_dir)
    assert public_key_b64(direct_priv.public_key()) == endpoint_pubkey

    get_contributor_settings.cache_clear()

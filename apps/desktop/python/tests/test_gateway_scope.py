"""Tests for the X-Potluck-Scope routing header on /v1/chat/completions
and /v1/completions.

Contract:
  "local"  -> serve this node only; 503 when no local model is loaded.
  "mine"   -> local first, then household peer; pool is NEVER reached.
  absent / any other value -> existing fall-through (local->household->pool).

All tests use TestClient with a loopback client address so peer_access_middleware
passes. Gateway auth is disabled via monkeypatched settings (same pattern as
test_gateway_compat.py). No real model is loaded; inference paths are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

_LOOPBACK = ("127.0.0.1", 50000)


# ---------------------------------------------------------------------------
# Shared fixtures (mirrors test_gateway_compat.py setup)
# ---------------------------------------------------------------------------

def _make_settings_no_gateway_key():
    s = MagicMock()
    s.gateway.enabled = False
    s.gateway.api_key = None
    s.gateway.pool_routing = False
    s.network.coordinator_url = None
    return s


@pytest.fixture()
def app(monkeypatch):
    import potluck_local.services.settings_manager as sm
    monkeypatch.setattr(sm, "get_settings", lambda: _make_settings_no_gateway_key())
    from potluck_local.app import create_app
    return create_app()


@pytest.fixture()
def client(app):
    return TestClient(app, client=_LOOPBACK, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fake runtimes
# ---------------------------------------------------------------------------

class _NoModelRuntime:
    """Simulates a sidecar with no model loaded."""
    def is_loaded(self): return False
    def current_model_id(self): return None


class _LocalRuntime:
    """Simulates a sidecar with a model loaded and working inference."""
    def is_loaded(self): return True
    def current_model_id(self): return "local-model"

    async def chat(self, messages, **kw):
        return {
            "choices": [{"message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def stream_chat_raw(self, messages, **kw):
        async def _gen():
            yield {"choices": [{"delta": {"content": "tok"}, "finish_reason": None}]}
        return _gen()


# ---------------------------------------------------------------------------
# Scope absent — existing behavior unchanged
# ---------------------------------------------------------------------------

class TestScopeAbsent:
    """No X-Potluck-Scope header: the existing routing logic runs unchanged."""

    def test_no_model_returns_503(self, client, monkeypatch) -> None:
        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _NoModelRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.household_model_map", lambda: {})
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)

        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 503

    def test_local_model_serves(self, client, monkeypatch) -> None:
        async def _no_peer(model):
            return None

        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _LocalRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", _no_peer)
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)

        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "pong"

    def test_unknown_scope_value_falls_through_to_default(self, client, monkeypatch) -> None:
        """Unrecognised scope values (e.g. 'cooperative') use existing behavior."""
        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _NoModelRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.household_model_map", lambda: {})
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)

        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Potluck-Scope": "cooperative"},
        )
        # Default path with no model -> 503, same as absent header.
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Scope "local"
# ---------------------------------------------------------------------------

class TestScopeLocal:
    def test_no_local_model_returns_503_with_guidance(self, client, monkeypatch) -> None:
        """503 detail includes actionable guidance when scope=local and no model."""
        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _NoModelRuntime())

        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Potluck-Scope": "local"},
        )
        assert resp.status_code == 503
        detail = resp.json().get("detail", "")
        # Must mention "local" or "load" so the caller knows what to do.
        assert "local" in detail.lower() or "load" in detail.lower(), f"unhelpful detail: {detail!r}"

    def test_local_model_serves_without_peer_lookup(self, client, monkeypatch) -> None:
        """With scope=local, peer_for_model is never called even when pool is enabled."""
        peer_calls: list[str] = []
        pool_calls: list[int] = []

        async def _track_peer(model):
            peer_calls.append(model)
            return None

        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _LocalRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", _track_peer)
        monkeypatch.setattr(
            "potluck_local.services.pool_routing.pool_enabled",
            lambda: (pool_calls.append(1) or True),  # pool is "on" but must not be reached
        )

        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Potluck-Scope": "local"},
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "pong"
        assert not peer_calls, f"peer_for_model called despite local scope: {peer_calls}"
        assert not pool_calls, f"pool_enabled called despite local scope: {pool_calls}"

    def test_local_model_specific_request_no_peer_lookup(self, client, monkeypatch) -> None:
        """scope=local + explicit model field: still served locally, no peer lookup."""
        peer_calls: list[str] = []

        async def _track_peer(model):
            peer_calls.append(model)
            return None

        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _LocalRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", _track_peer)
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "local-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Potluck-Scope": "local"},
        )
        assert resp.status_code == 200
        assert not peer_calls, f"peer_for_model called despite local scope: {peer_calls}"

    def test_completions_local_no_model_returns_503(self, client, monkeypatch) -> None:
        """/v1/completions also respects scope=local."""
        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _NoModelRuntime())

        resp = client.post(
            "/v1/completions",
            json={"prompt": "hello"},
            headers={"X-Potluck-Scope": "local"},
        )
        assert resp.status_code == 503
        detail = resp.json().get("detail", "")
        assert "local" in detail.lower() or "load" in detail.lower()

    def test_completions_local_model_serves(self, client, monkeypatch) -> None:
        """/v1/completions with scope=local and a loaded model returns 200."""
        peer_calls: list[str] = []

        async def _track_peer(model):
            peer_calls.append(model)
            return None

        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _LocalRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", _track_peer)
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)

        resp = client.post(
            "/v1/completions",
            json={"prompt": "hello"},
            headers={"X-Potluck-Scope": "local"},
        )
        assert resp.status_code == 200
        assert resp.json()["object"] == "text_completion"
        assert not peer_calls, f"peer_for_model called despite local scope: {peer_calls}"


# ---------------------------------------------------------------------------
# Scope "mine"
# ---------------------------------------------------------------------------

class TestScopeMine:
    def test_mine_uses_local_when_available(self, client, monkeypatch) -> None:
        """scope=mine with a local model loaded: served locally."""
        async def _no_peer(model):
            return None

        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _LocalRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", _no_peer)
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)

        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Potluck-Scope": "mine"},
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "pong"

    def test_mine_never_reaches_pool(self, client, monkeypatch) -> None:
        """scope=mine never falls through to the open pool even when pool is enabled."""
        pool_calls: list[int] = []

        async def _no_peer(model):
            return None

        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _NoModelRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", _no_peer)
        # Pool is "enabled" but must never be reached.
        monkeypatch.setattr(
            "potluck_local.services.pool_routing.pool_enabled",
            lambda: (pool_calls.append(1) or True),
        )

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-pool-only-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"X-Potluck-Scope": "mine"},
        )
        # No local, no peer -> 404 (model not found on household).
        assert resp.status_code in (503, 404), f"unexpected status: {resp.status_code}"
        assert not pool_calls, f"pool_enabled was reached {len(pool_calls)} time(s) despite 'mine' scope"

    def test_mine_no_local_no_peer_returns_error(self, client, monkeypatch) -> None:
        """scope=mine, no local model, no matching peer -> 503."""
        async def _no_peer(model):
            return None

        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _NoModelRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", _no_peer)
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)

        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Potluck-Scope": "mine"},
        )
        assert resp.status_code == 503

    def test_mine_completions_never_reaches_pool(self, client, monkeypatch) -> None:
        """/v1/completions with scope=mine also blocks pool routing."""
        pool_calls: list[int] = []

        async def _no_peer(model):
            return None

        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _NoModelRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", _no_peer)
        monkeypatch.setattr(
            "potluck_local.services.pool_routing.pool_enabled",
            lambda: (pool_calls.append(1) or True),
        )

        resp = client.post(
            "/v1/completions",
            json={"model": "pool-only-model", "prompt": "hello"},
            headers={"X-Potluck-Scope": "mine"},
        )
        assert resp.status_code in (503, 404)
        assert not pool_calls, f"pool_enabled reached {len(pool_calls)} time(s) despite 'mine' scope"

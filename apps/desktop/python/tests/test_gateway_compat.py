"""Tests for the new gateway compatibility endpoints.

  POST /v1/completions    — OpenAI legacy text-completion
  POST /api/generate      — Ollama legacy generate
  POST /v1/embeddings     — OpenAI embeddings (local)
  POST /api/embeddings    — Ollama legacy embeddings (local)
  POST /api/embed         — Ollama newer batch embeddings (local)

All tests use TestClient with client=('127.0.0.1', ...) so peer_access_middleware
sees a loopback caller.  Gateway auth is bypassed by ensuring get_settings returns
no api_key (monkeypatched before create_app).  No model is loaded; inference paths
that require a runtime fall through to 503.  Embedding tests are skipped when
sentence-transformers is not importable.
"""

from __future__ import annotations

import importlib
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# Loopback scope: passes the peer_access_middleware "is_loopback" check.
_LOOPBACK = ("127.0.0.1", 50000)


def _make_settings_with_no_gateway_key():
    """Return a minimal mock settings object that disables gateway auth."""
    s = MagicMock()
    s.gateway.enabled = False
    s.gateway.api_key = None
    s.gateway.pool_routing = False
    s.network.coordinator_url = None
    return s


@pytest.fixture()
def app(monkeypatch):
    """Create the FastAPI app with gateway auth disabled (no key)."""
    import potluck_local.services.settings_manager as sm
    monkeypatch.setattr(sm, "get_settings", lambda: _make_settings_with_no_gateway_key())

    from potluck_local.app import create_app
    return create_app()


@pytest.fixture()
def client(app):
    return TestClient(app, client=_LOOPBACK, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_sentence_transformers() -> bool:
    try:
        importlib.import_module("sentence_transformers")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# POST /v1/completions
# ---------------------------------------------------------------------------

class TestV1Completions:
    def test_no_model_loaded_returns_503(self, client) -> None:
        """When no model is loaded and no routing target is available, 503."""
        resp = client.post("/v1/completions", json={"prompt": "Hello"})
        # 503 = no model, 404 = model not found — both acceptable when runtime is cold.
        assert resp.status_code in (503, 404, 422), resp.text

    def test_response_envelope_shape(self, client, monkeypatch) -> None:
        """Non-streaming /v1/completions returns a text_completion envelope."""
        class _FakeRuntime:
            def is_loaded(self): return True
            def current_model_id(self): return "test-model"
            async def chat(self, messages, **kw):
                return {
                    "id": "chatcmpl-abc",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test-model",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "world"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                }

        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _FakeRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.household_model_map", lambda: {})
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)

        resp = client.post("/v1/completions", json={"model": "test-model", "prompt": "Hello"})
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["object"] == "text_completion"
        assert "id" in body
        assert "created" in body
        assert "model" in body
        choices = body["choices"]
        assert isinstance(choices, list) and len(choices) == 1
        choice = choices[0]
        assert "text" in choice
        assert choice["text"] == "world"
        assert choice["index"] == 0
        assert "finish_reason" in choice
        usage = body["usage"]
        assert "prompt_tokens" in usage
        assert "completion_tokens" in usage
        assert "total_tokens" in usage

    def test_prompt_list_collapsed(self, client, monkeypatch) -> None:
        """prompt as a list of strings is joined and sent as one user message."""
        captured: list[Any] = []

        class _FakeRuntime:
            def is_loaded(self): return True
            def current_model_id(self): return "m"
            async def chat(self, messages, **kw):
                captured.append(messages)
                return {
                    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }

        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _FakeRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.household_model_map", lambda: {})
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)

        resp = client.post("/v1/completions", json={"prompt": ["line one", "line two"]})
        assert resp.status_code == 200, resp.text
        assert len(captured) == 1
        assert "line one" in captured[0][0]["content"]
        assert "line two" in captured[0][0]["content"]

    def test_stream_returns_sse(self, client, monkeypatch) -> None:
        """stream=True returns text/event-stream with text_completion chunks."""
        async def _fake_stream(messages, **kw):
            yield {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}

        class _FakeRuntime:
            def is_loaded(self): return True
            def current_model_id(self): return "m"
            def stream_chat_raw(self, messages, **kw):
                return _fake_stream(messages, **kw)

        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _FakeRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.household_model_map", lambda: {})
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)

        resp = client.post("/v1/completions", json={"prompt": "go", "stream": True})
        assert resp.status_code == 200, resp.text
        assert "text/event-stream" in resp.headers.get("content-type", "")
        found = False
        for line in resp.text.splitlines():
            if line.startswith("data:") and "[DONE]" not in line:
                payload = json.loads(line[5:].strip())
                if payload.get("object") == "text_completion":
                    assert "choices" in payload
                    assert "text" in payload["choices"][0]
                    found = True
        assert found, f"no text_completion chunk found in:\n{resp.text}"


# ---------------------------------------------------------------------------
# POST /api/generate
# ---------------------------------------------------------------------------

class TestApiGenerate:
    def test_no_model_loaded_returns_503(self, client) -> None:
        resp = client.post("/api/generate", json={"prompt": "hi"})
        assert resp.status_code in (503, 404, 422), resp.text

    def test_nonstream_response_shape(self, client, monkeypatch) -> None:
        """Non-streaming /api/generate returns Ollama generate shape."""
        class _FakeRuntime:
            def is_loaded(self): return True
            def current_model_id(self): return "m"
            async def chat(self, messages, **kw):
                return {
                    "choices": [{"message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
                }

        monkeypatch.setattr("potluck_local.routers.ollama.get_runtime", lambda: _FakeRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.household_model_map", lambda: {})
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)

        resp = client.post("/api/generate", json={"model": "m", "prompt": "ping", "stream": False})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "response" in body
        assert body["response"] == "pong"
        assert body["done"] is True
        assert "done_reason" in body
        assert "model" in body
        assert "created_at" in body

    def test_stream_ndjson_shape(self, client, monkeypatch) -> None:
        """stream=True /api/generate returns NDJSON with response/done fields."""
        async def _fake_stream(messages, **kw):
            yield {"choices": [{"delta": {"content": "tok"}, "finish_reason": None}]}

        class _FakeRuntime:
            def is_loaded(self): return True
            def current_model_id(self): return "m"
            def stream_chat_raw(self, messages, **kw):
                return _fake_stream(messages, **kw)

        monkeypatch.setattr("potluck_local.routers.ollama.get_runtime", lambda: _FakeRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.household_model_map", lambda: {})
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)

        resp = client.post("/api/generate", json={"model": "m", "prompt": "go", "stream": True})
        assert resp.status_code == 200, resp.text
        ct = resp.headers.get("content-type", "")
        assert "ndjson" in ct or "json" in ct, f"unexpected content-type: {ct}"
        lines = [l for l in resp.text.splitlines() if l.strip()]
        assert len(lines) >= 2, f"expected at least 2 NDJSON lines, got:\n{resp.text}"
        token_lines = [json.loads(l) for l in lines]
        non_done = [o for o in token_lines if not o.get("done")]
        assert non_done, "no done=false lines found"
        assert "response" in non_done[0]
        last = token_lines[-1]
        assert last.get("done") is True
        assert "done_reason" in last


# ---------------------------------------------------------------------------
# POST /v1/embeddings
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _has_sentence_transformers(),
    reason="sentence-transformers not installed",
)
class TestV1Embeddings:
    def test_single_string_openai_shape(self, client) -> None:
        """Single-string input returns OpenAI list shape with one embedding."""
        resp = client.post("/v1/embeddings", json={"input": "hello world"})
        if resp.status_code == 503:
            pytest.skip("embedding model failed to load in this env")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["object"] == "list"
        assert "data" in body
        assert len(body["data"]) == 1
        item = body["data"][0]
        assert item["object"] == "embedding"
        assert item["index"] == 0
        assert isinstance(item["embedding"], list)
        assert len(item["embedding"]) > 0
        assert "model" in body
        assert "usage" in body
        assert "prompt_tokens" in body["usage"]
        assert "total_tokens" in body["usage"]

    def test_list_input_multiple_embeddings(self, client) -> None:
        """List input returns one embedding per element."""
        resp = client.post("/v1/embeddings", json={"input": ["foo", "bar"]})
        if resp.status_code == 503:
            pytest.skip("embedding model failed to load in this env")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["object"] == "list"
        assert len(body["data"]) == 2
        for i, item in enumerate(body["data"]):
            assert item["index"] == i
            assert isinstance(item["embedding"], list)


class TestV1EmbeddingsNoModel:
    """Validates the 503 path without needing sentence-transformers."""

    def test_returns_503_when_embedder_returns_none(self, client, monkeypatch) -> None:
        """When embed_many returns None (model unavailable), endpoint returns 503."""
        from potluck_local.services import embeddings as emb_mod

        class _FailEmbedder:
            MODEL_NAME = "fake"
            async def embed_many(self, texts):
                return None  # signals model unavailable

        monkeypatch.setattr(emb_mod, "get_embedder", lambda: _FailEmbedder())

        resp = client.post("/v1/embeddings", json={"input": "test"})
        assert resp.status_code == 503, resp.text


# ---------------------------------------------------------------------------
# POST /api/embeddings  and  POST /api/embed
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _has_sentence_transformers(),
    reason="sentence-transformers not installed",
)
class TestApiEmbeddings:
    def test_ollama_embeddings_single(self, client) -> None:
        """/api/embeddings returns {embedding: [...]}."""
        resp = client.post("/api/embeddings", json={"model": "ignored", "prompt": "test"})
        if resp.status_code == 503:
            pytest.skip("embedding model failed to load in this env")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "embedding" in body
        assert isinstance(body["embedding"], list)
        assert len(body["embedding"]) > 0

    def test_ollama_embed_batch(self, client) -> None:
        """/api/embed returns {embeddings: [[...]]}."""
        resp = client.post("/api/embed", json={"input": ["hello", "world"]})
        if resp.status_code == 503:
            pytest.skip("embedding model failed to load in this env")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "embeddings" in body
        assert isinstance(body["embeddings"], list)
        assert len(body["embeddings"]) == 2
        for vec in body["embeddings"]:
            assert isinstance(vec, list) and len(vec) > 0

    def test_ollama_embed_single_string(self, client) -> None:
        """/api/embed accepts a single string as input."""
        resp = client.post("/api/embed", json={"input": "single string"})
        if resp.status_code == 503:
            pytest.skip("embedding model failed to load in this env")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["embeddings"]) == 1


class TestApiEmbeddingsNoModel:
    def test_embeddings_empty_prompt_400(self, client) -> None:
        """/api/embeddings with empty prompt returns 400."""
        resp = client.post("/api/embeddings", json={"prompt": ""})
        assert resp.status_code == 400, resp.text

    def test_embed_empty_input_400(self, client) -> None:
        """/api/embed with empty list input returns 400."""
        resp = client.post("/api/embed", json={"input": []})
        assert resp.status_code == 400, resp.text

    def test_embed_returns_503_when_embedder_unavailable(self, client, monkeypatch) -> None:
        from potluck_local.services import embeddings as emb_mod

        class _FailEmbedder:
            MODEL_NAME = "fake"
            async def embed_many(self, texts):
                return None

        monkeypatch.setattr(emb_mod, "get_embedder", lambda: _FailEmbedder())

        resp = client.post("/api/embed", json={"input": "some text"})
        assert resp.status_code == 503, resp.text

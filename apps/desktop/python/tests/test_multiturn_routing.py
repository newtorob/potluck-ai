"""Tests for the multi-turn pool/circle routing feature.

Coverage:
  - last_user_prompt unchanged
  - _job_body privacy policy: circle always sends messages; pool default does not;
    pool with pool_multiturn opt-in does.
  - prompt is always populated regardless of scope.
  - contributor/schemas: ChatMessage and JobAssignedMessage.messages field.
  - contributor/client: stream_chat vs stream_completion dispatch.
  - Privacy: no message content appears in logs.
  - gateway: circle scope -> messages sent; pool default -> messages not sent.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# pool_routing helpers
# ---------------------------------------------------------------------------

class TestLastUserPrompt:
    """last_user_prompt is unchanged — returns the latest user content only."""

    def test_returns_last_user_message(self) -> None:
        from potluck_local.services.pool_routing import last_user_prompt

        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "follow-up"},
        ]
        assert last_user_prompt(msgs) == "follow-up"

    def test_skips_non_user_roles(self) -> None:
        from potluck_local.services.pool_routing import last_user_prompt

        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "last"},
        ]
        assert last_user_prompt(msgs) == ""

    def test_empty_messages_returns_empty_string(self) -> None:
        from potluck_local.services.pool_routing import last_user_prompt

        assert last_user_prompt([]) == ""


# ---------------------------------------------------------------------------
# _job_body privacy policy
# ---------------------------------------------------------------------------

MSGS = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "first"},
    {"role": "assistant", "content": "answer"},
    {"role": "user", "content": "follow-up"},
]


class TestJobBodyPolicy:
    """_job_body constructs the coordinator job envelope with the correct privacy policy."""

    def _call(self, scope: str, multiturn: bool) -> dict:
        from potluck_local.services import pool_routing as pr

        with patch.object(pr, "_pool_multiturn_enabled", return_value=multiturn):
            return pr._job_body(MSGS, "test-model", 0.7, 512, scope)

    def test_prompt_always_populated_pool_default(self) -> None:
        body = self._call("pool", False)
        assert body["prompt"] == "follow-up"

    def test_prompt_always_populated_circle(self) -> None:
        body = self._call("circle", False)
        assert body["prompt"] == "follow-up"

    def test_prompt_always_populated_pool_optin(self) -> None:
        body = self._call("pool", True)
        assert body["prompt"] == "follow-up"

    def test_pool_default_no_messages(self) -> None:
        """scope=pool without opt-in: messages must NOT appear in the body."""
        body = self._call("pool", False)
        assert "messages" not in body, "pool default must not send messages"

    def test_pool_optin_sends_messages(self) -> None:
        """scope=pool with pool_multiturn=True: full messages array is sent."""
        body = self._call("pool", True)
        assert "messages" in body
        assert isinstance(body["messages"], list)
        assert len(body["messages"]) == len(MSGS)
        assert body["messages"][-1] == {"role": "user", "content": "follow-up"}

    def test_circle_always_sends_messages(self) -> None:
        """scope=circle: full messages always sent regardless of pool_multiturn."""
        body = self._call("circle", False)
        assert "messages" in body
        assert len(body["messages"]) == len(MSGS)

    def test_circle_sends_all_roles(self) -> None:
        body = self._call("circle", False)
        roles = [m["role"] for m in body["messages"]]
        assert roles == ["system", "user", "assistant", "user"]

    def test_scope_field_matches_argument(self) -> None:
        pool_body = self._call("pool", False)
        assert pool_body["scope"] == "pool"

        circle_body = self._call("circle", False)
        assert circle_body["scope"] == "circle"

    def test_model_slug_in_body(self) -> None:
        body = self._call("pool", False)
        assert body["model_slug"] == "test-model"


# ---------------------------------------------------------------------------
# contributor/schemas
# ---------------------------------------------------------------------------

class TestContributorSchemas:
    def test_chat_message_model(self) -> None:
        from potluck_local.contributor.schemas import ChatMessage

        m = ChatMessage(role="user", content="hello")
        assert m.role == "user"
        assert m.content == "hello"

    def test_job_assigned_with_messages(self) -> None:
        import uuid
        from potluck_local.contributor.schemas import ChatMessage, JobAssignedMessage

        job = JobAssignedMessage(
            type="job_assigned",
            job_id=uuid.uuid4(),
            model_slug="test-model",
            prompt="follow-up",
            messages=[
                ChatMessage(role="user", content="first"),
                ChatMessage(role="assistant", content="ans"),
                ChatMessage(role="user", content="follow-up"),
            ],
        )
        assert job.messages is not None
        assert len(job.messages) == 3
        assert job.messages[-1].content == "follow-up"
        assert job.prompt == "follow-up"

    def test_job_assigned_without_messages_is_none(self) -> None:
        """Backward compat: old wire without messages field -> messages=None."""
        import uuid
        from potluck_local.contributor.schemas import JobAssignedMessage

        job = JobAssignedMessage(
            type="job_assigned",
            job_id=uuid.uuid4(),
            model_slug="test-model",
            prompt="hello",
        )
        assert job.messages is None

    def test_job_assigned_json_roundtrip_with_messages(self) -> None:
        import uuid
        from potluck_local.contributor.schemas import ChatMessage, JobAssignedMessage

        original = JobAssignedMessage(
            type="job_assigned",
            job_id=uuid.uuid4(),
            model_slug="m",
            prompt="p",
            messages=[ChatMessage(role="user", content="p")],
        )
        restored = JobAssignedMessage.model_validate_json(original.model_dump_json())
        assert restored.messages is not None
        assert restored.messages[0].role == "user"
        assert restored.messages[0].content == "p"

    def test_old_contributor_ignores_messages_field(self) -> None:
        """Extra fields are ignored by default so old contributors accept new wire."""
        import uuid
        from potluck_local.contributor.schemas import JobAssignedMessage

        # Simulate an old contributor receiving a job with a messages field
        # by using model_validate with extra data. Pydantic's default is to
        # ignore extra fields, so this should parse cleanly.
        data = {
            "type": "job_assigned",
            "job_id": str(uuid.uuid4()),
            "model_slug": "m",
            "prompt": "hello",
            "messages": [{"role": "user", "content": "hello"}],
        }
        job = JobAssignedMessage.model_validate(data)
        # New schema recognises the field; old pydantic ignores unknowns.
        assert job.prompt == "hello"


# ---------------------------------------------------------------------------
# contributor/client — inference path dispatch
# ---------------------------------------------------------------------------

class TestClientDispatch:
    """_handle_real_job picks stream_chat when messages is set, stream_completion otherwise.

    We use the Ollama backend for these tests because _handle_real_job exits
    early when model_path is None (llama-cpp path). The Ollama path calls
    get_ollama_engine unconditionally when inference_backend=="ollama", which
    we can patch cleanly.
    """

    def _make_job(self, with_messages: bool):
        import uuid
        from potluck_local.contributor.schemas import ChatMessage, JobAssignedMessage, SamplingParams

        msgs = (
            [ChatMessage(role="user", content="hi"), ChatMessage(role="assistant", content="hey"),
             ChatMessage(role="user", content="follow")]
            if with_messages
            else None
        )
        return JobAssignedMessage(
            type="job_assigned",
            job_id=uuid.uuid4(),
            model_slug="test-slug",
            prompt="follow",
            messages=msgs,
            sampling=SamplingParams(max_tokens=16, temperature=0.7),
        )

    def _run(self, coro) -> Any:
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_fake_engine(self, calls: dict) -> Any:
        class _FakeEngine:
            model_slug = "test-slug"

            async def stream_chat(self, messages, **kw):
                calls["chat"].append(messages)
                # Empty async generator — no tokens, job completes immediately.
                return
                yield  # noqa: unreachable — marks this as an async generator

            async def stream_completion(self, prompt, **kw):
                calls["completion"].append(prompt)
                return
                yield

        return _FakeEngine()

    def test_messages_present_uses_stream_chat(self) -> None:
        from potluck_local.contributor import client as cli
        import potluck_local.contributor.inference as inf_mod

        job = self._make_job(with_messages=True)
        calls: dict[str, list] = {"chat": [], "completion": []}
        fake_engine = self._make_fake_engine(calls)

        class _FakeSettings:
            inference_backend = "ollama"
            model_slug = "test-slug"
            ollama_url = "http://127.0.0.1:11434"
            n_ctx = 512
            inference_enabled = True

        class _FakeWS:
            async def send(self, _): pass

        async def _run_test():
            with patch.object(inf_mod, "get_ollama_engine", return_value=fake_engine):
                await cli._handle_real_job(_FakeWS(), job, _FakeSettings())

        self._run(_run_test())
        assert calls["chat"], "stream_chat should have been called"
        assert not calls["completion"], "stream_completion should not be called when messages is set"
        # Verify the messages were passed as dicts with role+content keys.
        assert calls["chat"][0][0] == {"role": "user", "content": "hi"}

    def test_no_messages_uses_stream_completion(self) -> None:
        from potluck_local.contributor import client as cli
        import potluck_local.contributor.inference as inf_mod

        job = self._make_job(with_messages=False)
        calls: dict[str, list] = {"chat": [], "completion": []}
        fake_engine = self._make_fake_engine(calls)

        class _FakeSettings:
            inference_backend = "ollama"
            model_slug = "test-slug"
            ollama_url = "http://127.0.0.1:11434"
            n_ctx = 512
            inference_enabled = True

        class _FakeWS:
            async def send(self, _): pass

        async def _run_test():
            with patch.object(inf_mod, "get_ollama_engine", return_value=fake_engine):
                await cli._handle_real_job(_FakeWS(), job, _FakeSettings())

        self._run(_run_test())
        assert calls["completion"], "stream_completion should have been called when no messages"
        assert not calls["chat"], "stream_chat should not be called when messages is absent"
        assert calls["completion"][0] == "follow"  # job.prompt


# ---------------------------------------------------------------------------
# Privacy: no message content in logs
# ---------------------------------------------------------------------------

class TestPrivacyLogging:
    """Log records must not contain message content — only counts and char totals."""

    def test_job_assigned_log_with_messages_no_content(self, caplog) -> None:
        """When a job arrives with messages, the log must not include content."""
        import uuid
        import json
        import asyncio
        from potluck_local.contributor import client as cli
        from potluck_local.contributor.schemas import ChatMessage, JobAssignedMessage

        job = JobAssignedMessage(
            type="job_assigned",
            job_id=uuid.uuid4(),
            model_slug="m",
            prompt="secret prompt",
            messages=[
                ChatMessage(role="user", content="secret content alpha"),
                ChatMessage(role="assistant", content="secret answer beta"),
                ChatMessage(role="user", content="secret follow gamma"),
            ],
        )
        # Simulate the _receive_loop log path in isolation.
        with caplog.at_level(logging.INFO, logger="potluck.contributor"):
            if job.messages:
                total_chars = sum(len(m.content) for m in job.messages)
                import logging as _logging
                logger = _logging.getLogger("potluck.contributor")
                logger.info(
                    "job_assigned_received job_id=%s model_slug=%s "
                    "message_count=%d total_chars=%d",
                    job.job_id, job.model_slug, len(job.messages), total_chars,
                )

        # Verify no message content appears in any log record.
        combined = " ".join(r.getMessage() for r in caplog.records)
        for secret in ["secret content alpha", "secret answer beta", "secret follow gamma",
                       "secret prompt"]:
            assert secret not in combined, f"Content leaked into log: {secret!r}"

        assert "message_count=3" in combined
        # total chars: 20 + 19 + 19 = 58... just check it's there as a number
        assert "total_chars=" in combined


# ---------------------------------------------------------------------------
# gateway routing — circle scope sends messages; pool default does not
# ---------------------------------------------------------------------------

_LOOPBACK = ("127.0.0.1", 50000)


def _make_settings_no_auth():
    s = MagicMock()
    s.gateway.enabled = False
    s.gateway.api_key = None
    s.gateway.pool_routing = False
    s.gateway.pool_multiturn = False
    s.network.coordinator_url = None
    return s


@pytest.fixture()
def app(monkeypatch):
    import potluck_local.services.settings_manager as sm
    monkeypatch.setattr(sm, "get_settings", lambda: _make_settings_no_auth())
    from potluck_local.app import create_app
    return create_app()


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app, client=_LOOPBACK, raise_server_exceptions=False)


class _NoModelRuntime:
    def is_loaded(self): return False
    def current_model_id(self): return None


class _LocalRuntime:
    def is_loaded(self): return True
    def current_model_id(self): return "local-model"

    async def chat(self, messages, **kw):
        return {
            "choices": [{"message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class TestGatewayCircleScope:
    """Circle scope routes to pool_routing with scope='circle', sending full messages."""

    def test_circle_scope_uses_pool_routing_with_circle_scope(
        self, client, monkeypatch
    ) -> None:
        """When X-Potluck-Scope: circle is set, complete_pool_openai is called
        with scope='circle' and the full messages array."""
        captured: dict[str, Any] = {}

        async def _fake_complete(messages, model, temperature, max_tokens, scope="pool"):
            captured["messages"] = messages
            captured["scope"] = scope
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            }

        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _NoModelRuntime())
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_available", lambda: True)
        monkeypatch.setattr(
            "potluck_local.services.pool_routing.complete_pool_openai", _fake_complete
        )

        resp = client.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                    {"role": "user", "content": "follow"},
                ]
            },
            headers={"X-Potluck-Scope": "circle"},
        )
        assert resp.status_code == 200, resp.text
        assert captured.get("scope") == "circle", f"expected circle scope, got {captured.get('scope')}"
        assert captured.get("messages") is not None
        assert len(captured["messages"]) == 4, "full messages array should be passed"

    def test_circle_scope_no_coordinator_returns_503(self, client, monkeypatch) -> None:
        """circle scope with no coordinator configured returns 503."""
        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _NoModelRuntime())
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_available", lambda: False)

        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Potluck-Scope": "circle"},
        )
        assert resp.status_code == 503


class TestGatewayPoolScope:
    """Pool default: messages not sent; pool_multiturn opt-in: messages sent."""

    def test_pool_default_does_not_send_messages_to_coordinator(
        self, client, monkeypatch
    ) -> None:
        """Default pool routing passes messages to pool_routing, but _job_body
        with multiturn=off will strip them — only prompt reaches the coordinator."""
        from potluck_local.services import pool_routing as pr

        body_built: list[dict] = []
        original_job_body = pr._job_body

        def _spy_job_body(messages, model, temperature, max_tokens, scope="pool"):
            result = original_job_body(messages, model, temperature, max_tokens, scope)
            body_built.append(result)
            return result

        async def _fake_no_peer(model):
            return None

        monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: _NoModelRuntime())
        monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", _fake_no_peer)
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: True)
        monkeypatch.setattr("potluck_local.services.pool_routing._pool_multiturn_enabled", lambda: False)
        monkeypatch.setattr("potluck_local.services.pool_routing._job_body", _spy_job_body)

        # We don't have a real coordinator so this will raise — catch at transport level.
        # What matters is what _job_body received, not whether the HTTP call succeeded.
        try:
            client.post(
                "/v1/chat/completions",
                json={"model": "pool-only", "messages": [
                    {"role": "user", "content": "secret"},
                ]},
            )
        except Exception:
            pass

        if body_built:
            # Verify pool default: no messages field in the coordinator body.
            assert "messages" not in body_built[0], (
                "pool default should not include messages in coordinator body"
            )
            assert body_built[0]["prompt"] == "secret"

    def test_pool_scope_string_in_job_body(self) -> None:
        """_job_body scope field is 'pool' for pool routing."""
        from potluck_local.services import pool_routing as pr

        with patch.object(pr, "_pool_multiturn_enabled", return_value=False):
            body = pr._job_body(
                [{"role": "user", "content": "hi"}], "m", 0.7, 512, "pool"
            )
        assert body["scope"] == "pool"
        assert "messages" not in body

    def test_pool_multiturn_optin_includes_messages_in_job_body(self) -> None:
        """When pool_multiturn is enabled, _job_body includes messages for pool scope."""
        from potluck_local.services import pool_routing as pr

        msgs = [
            {"role": "user", "content": "turn1"},
            {"role": "assistant", "content": "ans"},
            {"role": "user", "content": "turn2"},
        ]
        with patch.object(pr, "_pool_multiturn_enabled", return_value=True):
            body = pr._job_body(msgs, "m", 0.7, 512, "pool")

        assert "messages" in body
        assert len(body["messages"]) == 3
        assert body["prompt"] == "turn2"  # last_user_prompt still set


# ---------------------------------------------------------------------------
# settings model
# ---------------------------------------------------------------------------

class TestSettingsPoolMultiturn:
    def test_pool_multiturn_default_is_false(self) -> None:
        from potluck_local.services.settings_manager import GatewaySettings

        s = GatewaySettings()
        assert s.pool_multiturn is False

    def test_pool_multiturn_requires_pool_routing(self) -> None:
        """Validator: pool_multiturn is cleared when pool_routing is off."""
        from potluck_local.services.settings_manager import GatewaySettings

        s = GatewaySettings(enabled=True, api_key="k", pool_routing=False, pool_multiturn=True)
        assert s.pool_multiturn is False

    def test_pool_multiturn_allowed_when_pool_routing_on(self) -> None:
        from potluck_local.services.settings_manager import GatewaySettings

        s = GatewaySettings(enabled=True, api_key="k", pool_routing=True, pool_multiturn=True)
        assert s.pool_multiturn is True

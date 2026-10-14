"""Finding a household machine that has a model, when a machine is slow to answer.

Seen live: right after one machine's mesh service restarted, its /peer/capabilities
reply took longer than the first limit, the gateway treated it as absent, and the
caller got a 404 "model not found". The next request worked.

Contract:
  * every machine's capabilities are read at the same time;
  * a machine that answered and does not have the model is told apart from one that
    did not answer. When nothing that answered has the model, the silent machines
    are asked once more with a longer limit before the gateway decides;
  * still not found and a machine still silent: 503, in plain words, without the
    machine's address. Every machine answered and none has it: 404;
  * GET /v1/models keeps the short limit and never asks twice.

The roster and the capabilities calls are fakes. The only sleeps are a few hundredths
of a second, to let the overlap of the reads be seen.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from potluck_local.services import gateway_routing as routing
from potluck_local.services.model_traits import TemplateTraits

_LOOPBACK = ("127.0.0.1", 50000)
A, B, C = "100.64.0.2", "100.64.0.3", "100.64.0.4"
USER = {"role": "user", "content": "hi"}
SHA = "a" * 64


def caps(model: str | None, **extra) -> dict:
    return {"available_for_inference": True, "loaded_model": model, **extra}


class Household:
    """A scripted household. Each machine answers its asks from a list, in order; None
    is silence, and the last entry repeats. Records every ask with the limit it was
    given, and how many reads were running at once."""

    def __init__(self, monkeypatch, machines: dict[str, list]):
        self.script = {ip: list(replies) for ip, replies in machines.items()}
        self.asks: list[tuple[str, float]] = []
        self.running = 0
        self.peak_at: dict[float, int] = {}  # most reads running at once, by the limit they were given
        monkeypatch.setattr(routing, "household_peers", self._peers)
        monkeypatch.setattr(routing, "_peer_caps", self._caps)

    async def _peers(self) -> list[dict]:
        return [{"node_name": f"node-{ip}", "mesh_ip": ip} for ip in self.script]

    async def _caps(self, client: httpx.AsyncClient, peer: dict) -> dict | None:
        ip = peer["mesh_ip"]
        limit = client.timeout.read
        self.asks.append((ip, limit))
        self.running += 1
        self.peak_at[limit] = max(self.peak_at.get(limit, 0), self.running)
        try:
            await asyncio.sleep(0.02)
            replies = self.script[ip]
            return replies.pop(0) if len(replies) > 1 else replies[0]
        finally:
            self.running -= 1

    def asked(self) -> Counter:
        return Counter(self.asks)


# ---------------------------------------------------------------------------
# The lookup
# ---------------------------------------------------------------------------


class TestTheReadsRunTogether:
    def test_the_model_map_reads_every_machine_at_the_same_time(self, monkeypatch):
        home = Household(monkeypatch, {A: [caps("a")], B: [caps("b")], C: [caps("c")]})
        found = asyncio.run(routing.household_model_map())
        assert set(found) == {"a", "b", "c"} and home.peak_at == {2.0: 3}

    def test_the_lookup_reads_every_machine_at_the_same_time(self, monkeypatch):
        home = Household(monkeypatch, {A: [caps("a")], B: [caps("b")], C: [caps("c")]})
        assert asyncio.run(routing.peer_for_model("c"))["mesh_ip"] == C
        assert home.peak_at == {2.0: 3}

    def test_the_second_ask_goes_to_the_silent_machines_at_the_same_time(self, monkeypatch):
        home = Household(monkeypatch, {A: [None, None], B: [None, None], C: [caps("c")]})
        with pytest.raises(routing.HouseholdSilent):
            asyncio.run(routing.peer_for_model("x"))
        assert home.peak_at == {2.0: 3, 6.0: 2}

    def test_the_model_map_keeps_the_first_machine_for_a_model_in_roster_order(self, monkeypatch):
        Household(monkeypatch, {A: [caps("m", node="first")], B: [caps("m", node="second")]})
        found = asyncio.run(routing.household_model_map())
        assert found["m"]["mesh_ip"] == A and found["m"]["_capabilities"]["node"] == "first"


class TestAskingSilentMachinesAgain:
    def test_every_machine_answers_and_none_has_the_model(self, monkeypatch):
        home = Household(monkeypatch, {A: [caps("a")], B: [caps(None)]})
        assert asyncio.run(routing.peer_for_model("x")) is None
        assert home.asked() == Counter({(A, 2.0): 1, (B, 2.0): 1})  # no second ask

    def test_a_machine_with_the_model_ends_the_search_even_if_another_is_silent(self, monkeypatch):
        home = Household(monkeypatch, {A: [None, None], B: [caps("m")]})
        assert asyncio.run(routing.peer_for_model("m"))["mesh_ip"] == B
        assert home.asked() == Counter({(A, 2.0): 1, (B, 2.0): 1})

    def test_a_silent_machine_that_answers_the_second_ask_is_used(self, monkeypatch):
        home = Household(monkeypatch, {A: [None, caps("m")], B: [caps("other")]})
        found = asyncio.run(routing.peer_for_model("m"))
        assert found["mesh_ip"] == A and found["_capabilities"]["loaded_model"] == "m"
        # one extra ask, to the machine that was silent, with the longer limit; no third
        assert home.asked() == Counter({(A, 2.0): 1, (B, 2.0): 1, (A, 6.0): 1})

    def test_only_the_silent_machines_are_asked_again(self, monkeypatch):
        home = Household(monkeypatch, {A: [caps("a")], B: [None, caps("m")], C: [None, None]})
        assert asyncio.run(routing.peer_for_model("m"))["mesh_ip"] == B
        assert home.asked() == Counter({(A, 2.0): 1, (B, 2.0): 1, (C, 2.0): 1, (B, 6.0): 1, (C, 6.0): 1})

    def test_a_machine_that_answers_the_second_ask_without_the_model_is_a_plain_not_found(self, monkeypatch):
        home = Household(monkeypatch, {A: [None, caps("other")], B: [caps("b")]})
        assert asyncio.run(routing.peer_for_model("m")) is None
        assert home.asked()[(A, 6.0)] == 1

    def test_a_machine_silent_twice_raises_and_the_message_is_plain(self, monkeypatch):
        home = Household(monkeypatch, {A: [None], B: [caps("b")]})
        with pytest.raises(routing.HouseholdSilent) as info:
            asyncio.run(routing.peer_for_model("m"))
        message = str(info.value)
        assert "did not answer" in message and "tried again" in message and "'m'" in message
        assert A not in message and B not in message and "node-" not in message
        assert home.asked() == Counter({(A, 2.0): 1, (B, 2.0): 1, (A, 6.0): 1})

    def test_a_machine_that_answered_with_nothing_to_offer_is_not_silent(self, monkeypatch):
        # Potluck closed on that machine, or a version too old to say: asking again changes nothing.
        home = Household(monkeypatch, {A: [{}, {}], B: [caps("other")]})
        assert asyncio.run(routing.peer_for_model("m")) is None
        assert home.asked() == Counter({(A, 2.0): 1, (B, 2.0): 1})

    def test_no_machines_means_not_found_not_silence(self, monkeypatch):
        home = Household(monkeypatch, {})
        assert asyncio.run(routing.peer_for_model("m")) is None and home.asks == []

    def test_an_exact_model_lock_is_asked_again_too(self, monkeypatch):
        locked = caps("m", model_lock_version=1, loaded_model_detail={"sha256": SHA})
        home = Household(monkeypatch, {A: [None, locked]})
        assert asyncio.run(routing.peer_for_model("m", SHA))["mesh_ip"] == A
        assert home.asked() == Counter({(A, 2.0): 1, (A, 6.0): 1})
        Household(monkeypatch, {A: [None, None]})
        with pytest.raises(routing.HouseholdSilent):
            asyncio.run(routing.peer_for_model("m", SHA))

    def test_find_peer_hands_the_silence_back_instead_of_raising_it(self, monkeypatch):
        Household(monkeypatch, {A: [None]})
        peer, silent = asyncio.run(routing.find_peer("m"))
        assert peer is None and isinstance(silent, routing.HouseholdSilent)
        Household(monkeypatch, {A: [caps("m")]})
        peer, silent = asyncio.run(routing.find_peer("m"))
        assert peer["mesh_ip"] == A and silent is None

    def test_a_retry_in_the_middle_of_a_stream_does_not_wait_for_a_silent_machine(self, monkeypatch):
        home = Household(monkeypatch, {A: [None, caps("m")], B: [None, None]})
        assert asyncio.run(routing._replacement_peer("m", {C})) is None  # noqa: SLF001
        assert home.asked() == Counter({(A, 2.0): 1, (B, 2.0): 1})


class TestTheModelListStaysQuick:
    def test_it_keeps_the_short_limit_and_never_asks_a_silent_machine_again(self, monkeypatch):
        home = Household(monkeypatch, {A: [None, caps("late")], B: [caps("b")]})
        assert set(asyncio.run(routing.household_model_map())) == {"b"}
        assert home.asked() == Counter({(A, 2.0): 1, (B, 2.0): 1})


class TestWhatAMachineSaysIsOnlyUsedWhenItIsAnObject:
    def _caps_from(self, handler) -> dict | None:
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await routing._peer_caps(client, {"mesh_ip": A})  # noqa: SLF001
        return asyncio.run(run())

    def test_an_object_is_taken(self):
        assert self._caps_from(lambda r: httpx.Response(200, json={"loaded_model": "m"})) == {"loaded_model": "m"}

    @pytest.mark.parametrize("response", [
        httpx.Response(200, json=["m"]), httpx.Response(200, text="not json"), httpx.Response(503, json={"loaded_model": "m"}),
    ])
    def test_any_other_answer_is_an_answer_with_nothing_to_offer(self, response):
        assert self._caps_from(lambda r: response) == {}

    def test_a_refused_connection_is_an_answer_too(self):
        def refuse(request):
            raise httpx.ConnectError("refused")
        assert self._caps_from(refuse) == {}

    @pytest.mark.parametrize("error", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout])
    def test_only_running_out_of_time_is_silence(self, error):
        def slow(request):
            raise error("too slow")
        assert self._caps_from(slow) is None


# ---------------------------------------------------------------------------
# Through the routes
# ---------------------------------------------------------------------------


class Local:
    """This machine, with a model loaded (or not)."""

    def __init__(self, model: str | None = "local-model"):
        self.model = model
        self.calls: list[dict] = []

    def is_loaded(self): return self.model is not None
    def current_model_id(self): return self.model
    def template_traits(self): return TemplateTraits(supports_tools=True)

    def assert_model_identity(self, model, digest=None):
        from potluck_local.services.model_identity import ModelIdentityError
        raise ModelIdentityError("not this machine's model")

    async def chat(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        return {"choices": [{"message": {"role": "assistant", "content": "local"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


@pytest.fixture(autouse=True)
def no_usage_rows(monkeypatch):
    async def record(*args, **kwargs):
        pass

    monkeypatch.setattr("potluck_local.routers.gateway._record_consumed", record)


@pytest.fixture()
def client(monkeypatch):
    import potluck_local.services.settings_manager as sm

    s = MagicMock()
    s.gateway.enabled = False
    s.gateway.api_key = None
    s.gateway.pool_routing = False
    s.network.coordinator_url = None
    monkeypatch.setattr(sm, "get_settings", lambda: s)
    from potluck_local.app import create_app

    monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)
    return TestClient(create_app(), client=_LOOPBACK, raise_server_exceptions=False)


@pytest.fixture()
def serving(monkeypatch):
    """A peer reply, for a request the lookup sends to a household machine."""
    sent: list[str] = []

    async def complete_peer_openai(peer, messages, model, temperature, max_tokens, **kw):
        sent.append(peer["mesh_ip"])
        return {"id": "x", "created": 1, "model": model, "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "from peer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}

    monkeypatch.setattr(routing, "complete_peer_openai", complete_peer_openai)
    return sent


def use_local(monkeypatch, model: str | None = "local-model") -> Local:
    runtime = Local(model)
    monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: runtime)
    return runtime


def chat(client, **fields):
    headers = fields.pop("headers", {})
    return client.post("/v1/chat/completions", json={"messages": [USER], "model": "m", **fields}, headers=headers)


def completions(client, **headers):
    return client.post("/v1/completions", json={"prompt": "hi", "model": "m"}, headers=headers)


def messages_route(client, **headers):
    return client.post("/v1/messages", json={"model": "m", "max_tokens": 5, "messages": [USER]}, headers=headers)


NOT_FOUND = "model 'm' is not loaded here or on a reachable household peer"


class TestEveryAnsweringMachineLacksTheModel:
    def test_it_is_a_404_on_every_route(self, client, monkeypatch):
        use_local(monkeypatch)
        home = Household(monkeypatch, {A: [caps("a")], B: [caps(None)]})
        for resp in (chat(client), completions(client), chat(client, headers={"X-Potluck-Scope": "mine"}),
                     completions(client, **{"X-Potluck-Scope": "mine"})):
            assert resp.status_code == 404 and resp.json() == {"detail": NOT_FOUND}
        assert home.asked() == Counter({(A, 2.0): 4, (B, 2.0): 4})  # never a second ask

    def test_the_messages_route_says_not_found_in_its_own_shape(self, client, monkeypatch):
        use_local(monkeypatch)
        Household(monkeypatch, {A: [caps("a")]})
        resp = messages_route(client)
        assert resp.status_code == 404 and resp.json()["error"]["type"] == "not_found_error"
        assert resp.json()["error"]["message"] == NOT_FOUND


class TestASlowMachineIsAskedAgain:
    def test_the_request_is_served_when_the_slow_machine_answers_the_second_ask(self, client, monkeypatch, serving):
        use_local(monkeypatch)
        home = Household(monkeypatch, {A: [None, caps("m")], B: [caps("b")]})
        resp = chat(client)
        assert resp.status_code == 200 and resp.json()["choices"][0]["message"]["content"] == "from peer"
        assert resp.headers["x-potluck-served-by"] == "household" and serving == [A]
        assert home.asked() == Counter({(A, 2.0): 1, (B, 2.0): 1, (A, 6.0): 1})

    def test_the_other_routes_do_the_same(self, client, monkeypatch, serving):
        use_local(monkeypatch)
        Household(monkeypatch, {A: [None, caps("m")]})
        assert chat(client, headers={"X-Potluck-Scope": "mine"}).status_code == 200
        Household(monkeypatch, {A: [None, caps("m")]})
        assert completions(client).status_code == 200
        Household(monkeypatch, {A: [None, caps("m")]})
        assert messages_route(client).status_code == 200
        assert serving == [A, A, A]

    def test_a_model_locked_request_is_served_too(self, client, monkeypatch, serving):
        use_local(monkeypatch)
        locked = caps("m", model_lock_version=1, loaded_model_detail={"sha256": SHA})
        home = Household(monkeypatch, {A: [None, locked]})
        resp = chat(client, potluck_model_sha256=SHA, headers={"X-Potluck-Scope": "mine"})
        assert resp.status_code == 200 and serving == [A]
        assert home.asked() == Counter({(A, 2.0): 1, (A, 6.0): 1})


class TestAMachineThatStaysSilent:
    def _assert_retry_later(self, resp, home):
        assert resp.status_code == 503, resp.text
        text = resp.text
        assert "did not answer" in text and "tried again" in text
        assert A not in text and B not in text and "node-" not in text
        assert home.asked() == Counter({(A, 2.0): 1, (B, 2.0): 1, (A, 6.0): 1})

    def test_the_openai_route_refuses_with_a_503(self, client, monkeypatch):
        use_local(monkeypatch)
        home = Household(monkeypatch, {A: [None], B: [caps("b")]})
        resp = chat(client)
        self._assert_retry_later(resp, home)
        assert resp.json()["detail"].startswith("A household machine did not answer")

    def test_so_do_the_scoped_and_legacy_routes(self, client, monkeypatch):
        use_local(monkeypatch)
        for call in (lambda: chat(client, headers={"X-Potluck-Scope": "mine"}), lambda: completions(client),
                     lambda: completions(client, **{"X-Potluck-Scope": "mine"})):
            home = Household(monkeypatch, {A: [None], B: [caps("b")]})
            self._assert_retry_later(call(), home)

    def test_the_messages_route_answers_in_its_own_shape(self, client, monkeypatch):
        use_local(monkeypatch)
        home = Household(monkeypatch, {A: [None], B: [caps("b")]})
        resp = messages_route(client)
        self._assert_retry_later(resp, home)
        assert resp.json()["type"] == "error" and resp.json()["error"]["type"] == "overloaded_error"

    def test_a_model_locked_request_is_a_503_not_a_conflict(self, client, monkeypatch):
        use_local(monkeypatch)
        home = Household(monkeypatch, {A: [None]})
        resp = chat(client, potluck_model_sha256=SHA, headers={"X-Potluck-Scope": "mine"})
        assert resp.status_code == 503 and "did not answer" in resp.text
        assert home.asked() == Counter({(A, 2.0): 1, (A, 6.0): 1})

    def test_a_model_locked_request_with_every_machine_answering_is_still_a_conflict(self, client, monkeypatch):
        use_local(monkeypatch)
        Household(monkeypatch, {A: [caps("other")]})
        assert chat(client, potluck_model_sha256=SHA, headers={"X-Potluck-Scope": "mine"}).status_code == 409

    def test_the_readiness_check_says_not_ready(self, client, monkeypatch):
        use_local(monkeypatch)
        Household(monkeypatch, {A: [None]})
        resp = client.post("/v1/potluck/readiness", json={"model": "m", "sha256": SHA, "scope": "mine"})
        assert resp.status_code == 200 and resp.json()["ready"] is False

    def test_the_local_model_is_never_held_up_by_a_silent_machine(self, client, monkeypatch):
        runtime = use_local(monkeypatch)
        home = Household(monkeypatch, {A: [None]})
        assert chat(client, model="local-model").status_code == 200
        assert home.asks == [] and len(runtime.calls) == 1

    def test_when_the_pool_is_on_a_silent_machine_does_not_stop_the_pool(self, client, monkeypatch):
        use_local(monkeypatch)
        Household(monkeypatch, {A: [None]})
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: True)

        async def complete_pool_openai(messages, model, temperature, max_tokens, scope=None, circle_id=None):
            return {"id": "x", "created": 1, "model": model, "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "from pool"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

        monkeypatch.setattr("potluck_local.services.pool_routing.complete_pool_openai", complete_pool_openai)
        resp = chat(client)
        assert resp.status_code == 200 and resp.headers["x-potluck-served-by"] == "pool"

    def test_the_ollama_routes_refuse_the_same_way(self, client, monkeypatch):
        runtime = use_local(monkeypatch)
        monkeypatch.setattr("potluck_local.routers.ollama.get_runtime", lambda: runtime)
        Household(monkeypatch, {A: [None]})
        chat_resp = client.post("/api/chat", json={"model": "m", "messages": [USER], "stream": False})
        generate = client.post("/api/generate", json={"model": "m", "prompt": "hi", "stream": False})
        for resp in (chat_resp, generate):
            assert resp.status_code == 503 and "did not answer" in resp.json()["detail"] and A not in resp.text
        Household(monkeypatch, {A: [caps("a")]})
        for resp in (client.post("/api/chat", json={"model": "m", "messages": [USER], "stream": False}),
                     client.post("/api/generate", json={"model": "m", "prompt": "hi", "stream": False})):
            assert resp.status_code == 404


class TestTheModelListRoute:
    def test_it_asks_each_machine_once_with_the_short_limit(self, client, monkeypatch):
        use_local(monkeypatch)
        monkeypatch.setattr("potluck_local.routers.gateway.list_models", lambda: [])
        home = Household(monkeypatch, {A: [None, caps("late")], B: [caps("b")]})
        resp = client.get("/v1/models")
        ids = {e["id"] for e in resp.json()["data"]}
        assert resp.status_code == 200 and ids == {"local-model", "b"}
        assert home.asked() == Counter({(A, 2.0): 1, (B, 2.0): 1})


def test_the_logs_name_counts_and_never_an_address(monkeypatch, caplog):
    import logging

    Household(monkeypatch, {A: [None]})
    with caplog.at_level(logging.INFO, logger="potluck_local.services.gateway_routing"):
        with pytest.raises(routing.HouseholdSilent):
            asyncio.run(routing.peer_for_model("secret-model-name"))
    lines = [r.getMessage() for r in caplog.records if r.name == "potluck_local.services.gateway_routing"]
    assert lines == ["household_caps_second_ask silent=1 asked=1", "household_caps_unanswered silent=1 asked=1"]
    assert json.dumps(lines).count(A) == 0

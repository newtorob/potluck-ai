"""POST /v1/messages and /v1/messages/count_tokens, through the real app and a fake runtime.

Same setup as test_gateway_reasoning_tools.py: TestClient with a loopback client
address, the gateway key off unless a test sets one, no real model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from unittest.mock import MagicMock

import pytest
from anthropic_support import assert_well_formed, blocks, names, parse_sse, sse_events
from fastapi.testclient import TestClient

from potluck_local.services.inference_errors import InferenceFailure
from potluck_local.services.model_identity import ModelIdentityError
from potluck_local.services.model_traits import TemplateTraits
from potluck_local.services.remote_stream import RemoteStreamError

_LOOPBACK = ("127.0.0.1", 50000)

TOOLS_OK = TemplateTraits(supports_tools=True)
WEATHER = {"name": "get_weather", "description": "Weather", "input_schema": {
    "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}
TIME = {"name": "get_time", "input_schema": {"type": "object"}}
USER = {"role": "user", "content": "What's the weather in Paris right now? Use the tool."}
ANSWER = "A mesh network is a network where every node relays data for the others."
THINKING = "<think>\nOkay, the user wants a definition. Every node relays.\n</think>\n\n" + ANSWER
HERMES = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>'
TWO_CALLS = HERMES + '\n<tool_call>\n{"name": "get_time", "arguments": {}}\n</tool_call>'
COUNTED = {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}
ERROR_ITEM = {"__error__": "the engine broke", "__category__": "inference_error"}
OVERFLOW_ITEM = {"__error__": "Requested tokens (5000) exceed context window of 4096", "__category__": "inference_error"}
PROMPT_TOO_LONG = "prompt is too long: 5000 tokens > 4095 maximum"


def body(**fields):
    return {"model": "local-model", "max_tokens": 100, "messages": [{"role": "user", "content": "ping"}], **fields}


def llama_chunks(pieces, finish="stop", usage=None):
    base = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "local-model"}
    yield {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "logprobs": None, "finish_reason": None}]}
    for piece in pieces:
        yield {**base, "choices": [{"index": 0, "delta": {"content": piece}, "logprobs": None, "finish_reason": None}]}
    last = {**base, "choices": [{"index": 0, "delta": {}, "logprobs": None, "finish_reason": finish}]}
    if usage:
        last["usage"] = usage
    yield last


def completion(text, finish="stop", usage=COUNTED):
    result = {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "local-model",
              "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "logprobs": None, "finish_reason": finish}]}
    if usage is not None:
        result["usage"] = usage
    return result


def pieces_of(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)]


class FakeRuntime:
    """A loaded llama.cpp model. `reply` is what it produces, `pieces` how it arrives when streamed.
    `items` replaces the streamed chunks outright (to script errors)."""

    def __init__(self, reply="pong", *, pieces=None, finish="stop", traits=TOOLS_OK, usage=COUNTED, tokens=None,
                 items=None, first_delay=0.0, chat_error=None, stream_error=None):
        self.reply, self.finish, self.traits, self.usage, self.tokens = reply, finish, traits, usage, tokens
        self.pieces = pieces if pieces is not None else [reply]
        self.items, self.first_delay, self.chat_error, self.stream_error = items, first_delay, chat_error, stream_error
        self.calls: list[dict] = []
        self.counted: list[str] = []

    def is_loaded(self): return True
    def current_model_id(self): return "local-model"
    def template_traits(self): return self.traits

    async def count_tokens(self, text):
        self.counted.append(text)
        return self.tokens

    async def chat(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        if self.chat_error:
            raise self.chat_error
        return completion(self.reply, self.finish, self.usage)

    def stream_chat_raw(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})

        async def gen():
            if self.first_delay:
                await asyncio.sleep(self.first_delay)
            if self.stream_error:
                raise self.stream_error
            for chunk in (self.items if self.items is not None else llama_chunks(self.pieces, self.finish)):
                yield chunk
        return gen()


@pytest.fixture()
def settings(monkeypatch):
    import potluck_local.services.settings_manager as sm

    s = MagicMock()
    s.gateway.enabled = False
    s.gateway.api_key = None
    s.gateway.pool_routing = False
    s.network.coordinator_url = None
    monkeypatch.setattr(sm, "get_settings", lambda: s)
    return s


@pytest.fixture()
def client(monkeypatch, settings):
    from potluck_local.app import create_app

    async def no_peer(model, sha256=None):
        return None

    monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", no_peer)
    monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)
    return TestClient(create_app(), client=_LOOPBACK, raise_server_exceptions=False)


def serve(monkeypatch, runtime):
    monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: runtime)
    return runtime


def post(client, payload, query="?beta=true", **headers):
    return client.post(f"/v1/messages{query}", json=payload, headers=headers)


def sse(resp):
    assert resp.status_code == 200, resp.text
    return parse_sse(sse_events(resp.text))


def assert_error(resp, status, kind, message=None):
    assert resp.status_code == status, resp.text
    payload = resp.json()
    assert set(payload) == {"type", "error"} and payload["type"] == "error"
    assert set(payload["error"]) == {"type", "message"} and payload["error"]["type"] == kind
    if message is not None:
        assert payload["error"]["message"] == message
    return payload["error"]["message"]


# ---------------------------------------------------------------------------
# A whole reply
# ---------------------------------------------------------------------------


class TestNonStreaming:
    def test_a_plain_message(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime("pong"))
        resp = post(client, body(system="be brief"))
        assert resp.status_code == 200
        reply = resp.json()
        assert reply.pop("id").startswith("msg_")
        assert reply == {"type": "message", "role": "assistant", "model": "local-model",
                         "content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn", "stop_sequence": None,
                         "usage": {"input_tokens": 7, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        assert resp.headers["x-potluck-served-by"] == "local" and "x-potluck-usage" not in resp.headers
        (call,) = rt.calls
        assert call["messages"] == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "ping"}]
        assert call["temperature"] == 0.7 and call["max_tokens"] == 100 and "stop" not in call
        assert call["expected_model"] == "local-model" and "tools" not in call

    def test_the_query_string_is_ignored(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        assert post(client, body(), query="").status_code == 200
        assert post(client, body(), query="?beta=true&x=1").status_code == 200

    def test_without_a_model_the_loaded_one_answers_and_is_named(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        payload = body()
        del payload["model"]
        assert post(client, payload).json()["model"] == "local-model"

    def test_options_reach_the_model(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime())
        post(client, body(temperature=0.2, max_tokens=9, stop_sequences=["END"], top_p=0.5, top_k=4))
        assert rt.calls[0]["temperature"] == 0.2 and rt.calls[0]["max_tokens"] == 9 and rt.calls[0]["stop"] == ["END"]
        assert "top_p" not in rt.calls[0] and "top_k" not in rt.calls[0]

    def test_a_reply_cut_off_by_length(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("cut", finish="length"))
        assert post(client, body()).json()["stop_reason"] == "max_tokens"

    def test_reasoning_is_hidden_unless_asked_for(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(THINKING))
        for thinking in (None, {"type": "disabled"}, {"type": "adaptive", "display": "omitted"}):
            payload = body() if thinking is None else body(thinking=thinking)
            assert post(client, payload).json()["content"] == [{"type": "text", "text": ANSWER}]

    @pytest.mark.parametrize("thinking", [{"type": "enabled", "budget_tokens": 1024}, {"type": "adaptive"}])
    def test_reasoning_is_a_thinking_block_when_asked_for(self, client, monkeypatch, thinking):
        serve(monkeypatch, FakeRuntime(THINKING))
        content = post(client, body(thinking=thinking)).json()["content"]
        assert content == [{"type": "thinking", "thinking": "Okay, the user wants a definition. Every node relays.", "signature": ""},
                           {"type": "text", "text": ANSWER}]

    def test_usage_is_estimated_when_the_engine_gave_none(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("a" * 40, usage=None))
        resp = post(client, body())
        usage = resp.json()["usage"]
        assert resp.headers["x-potluck-usage"] == "estimated"
        assert usage["output_tokens"] == 10 and type(usage["input_tokens"]) is int and usage["input_tokens"] >= 1


class TestToolCalls:
    def test_a_call_is_a_tool_use_block_with_the_engines_id(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime(HERMES))
        reply = post(client, body(messages=[USER], tools=[WEATHER])).json()
        (block,) = reply["content"]
        assert block["type"] == "tool_use" and block["name"] == "get_weather" and block["input"] == {"city": "Paris"}
        assert len(block["id"]) == 9 and block["id"].isalnum()
        assert reply["stop_reason"] == "tool_use"
        assert rt.calls[0]["tools"] == [{"type": "function", "function": {
            "name": "get_weather", "description": "Weather", "parameters": WEATHER["input_schema"]}}]
        assert rt.calls[0]["tool_choice"] == "auto"

    def test_the_id_comes_back_unchanged_in_a_later_tool_result(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime(HERMES))
        first = post(client, body(messages=[USER], tools=[WEATHER])).json()
        call_id = first["content"][0]["id"]
        rt.reply = "It is sunny in Paris."
        history = [USER, {"role": "assistant", "content": first["content"]},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "sunny, 21C"}]}]
        second = post(client, body(messages=history, tools=[WEATHER])).json()
        assert second["content"] == [{"type": "text", "text": "It is sunny in Paris."}] and second["stop_reason"] == "end_turn"
        sent = rt.calls[1]["messages"]
        assert sent[1]["tool_calls"][0]["id"] == call_id and sent[1]["tool_calls"][0]["function"]["arguments"] == {"city": "Paris"}
        assert sent[2] == {"role": "tool", "content": "sunny, 21C", "tool_call_id": call_id}

    @pytest.mark.parametrize("choice, want", [
        ({"type": "auto"}, "auto"), ({"type": "any"}, "required"),
        ({"type": "tool", "name": "get_weather"}, {"type": "function", "function": {"name": "get_weather"}}),
    ])
    def test_tool_choice_reaches_the_model(self, client, monkeypatch, choice, want):
        rt = serve(monkeypatch, FakeRuntime(HERMES))
        post(client, body(messages=[USER], tools=[WEATHER], tool_choice=choice))
        assert rt.calls[0]["tool_choice"] == want

    def test_tool_choice_none_offers_no_tools(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime("hello"))
        post(client, body(messages=[USER], tools=[WEATHER], tool_choice={"type": "none"}))
        assert "tools" not in rt.calls[0]

    def test_parallel_calls_are_kept_unless_turned_off(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(TWO_CALLS))
        both = post(client, body(messages=[USER], tools=[WEATHER, TIME])).json()
        assert [b["name"] for b in both["content"]] == ["get_weather", "get_time"]
        one = post(client, body(messages=[USER], tools=[WEATHER, TIME], tool_choice={"type": "auto", "disable_parallel_tool_use": True})).json()
        assert [b["name"] for b in one["content"]] == ["get_weather"] and one["stop_reason"] == "tool_use"

    def test_a_model_without_tool_support_is_a_clear_400(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(traits=TemplateTraits()))
        resp = post(client, body(messages=[USER], tools=[WEATHER]))
        assert "does not support tool calls" in assert_error(resp, 400, "invalid_request_error")
        assert resp.headers["x-potluck-error"] == "tools_not_supported"


# ---------------------------------------------------------------------------
# A streamed reply
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_a_plain_stream_is_exactly_the_anthropic_events(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("Hello there", pieces=["Hello", " there"]))
        resp = post(client, body(stream=True))
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["x-potluck-served-by"] == "local" and resp.headers["x-potluck-usage"] == "estimated"
        assert resp.headers["cache-control"] == "no-cache"
        events = sse(resp)
        assert_well_formed(events)
        assert names(events) == ["message_start", "ping", "content_block_start", "content_block_delta", "content_block_delta",
                                 "content_block_stop", "message_delta", "message_stop"]
        assert events[0][1]["message"]["model"] == "local-model" and events[0][1]["message"]["usage"]["output_tokens"] == 0
        assert blocks(events)[0]["text"] == "Hello there"
        assert events[-2][1]["delta"] == {"stop_reason": "end_turn", "stop_sequence": None}
        assert type(events[-2][1]["usage"]["output_tokens"]) is int
        assert resp.text.startswith("event: message_start\ndata: {")

    def test_input_tokens_come_from_the_tokenizer_of_the_model_that_answers(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime("ok", tokens=4321))
        events = sse(post(client, body(stream=True, system="SYSTEM-TEXT")))
        assert events[0][1]["message"]["usage"]["input_tokens"] == 4321
        assert "SYSTEM-TEXT" in rt.counted[0] and "ping" in rt.counted[0]

    def test_input_tokens_are_estimated_when_the_tokenizer_cannot_say(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("ok", tokens=None))
        events = sse(post(client, body(stream=True, system="x" * 400)))
        assert events[0][1]["message"]["usage"]["input_tokens"] >= 100

    @pytest.mark.parametrize("size", [1, 3, 7, 1000])
    def test_thinking_then_text_at_any_chunk_size(self, client, monkeypatch, size):
        serve(monkeypatch, FakeRuntime(THINKING, pieces=pieces_of(THINKING, size)))
        events = sse(post(client, body(stream=True, thinking={"type": "enabled", "budget_tokens": 1024})))
        assert_well_formed(events)
        thinking, answer = blocks(events)
        assert thinking["block"]["type"] == "thinking" and thinking["text"] == "Okay, the user wants a definition. Every node relays."
        assert thinking["signature"] == ""
        assert answer["block"]["type"] == "text" and answer["text"] == ANSWER

    def test_hidden_thinking_leaves_only_the_answer(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(THINKING, pieces=pieces_of(THINKING, 5)))
        events = sse(post(client, body(stream=True, thinking={"type": "adaptive", "display": "omitted"})))
        assert_well_formed(events)
        assert [b["text"] for b in blocks(events)] == [ANSWER]
        assert "Every node relays" not in json.dumps(events)

    def test_a_tool_call_only(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(HERMES, pieces=pieces_of(HERMES, 6)))
        events = sse(post(client, body(stream=True, messages=[USER], tools=[WEATHER])))
        assert_well_formed(events)
        (call,) = blocks(events)
        assert call["block"]["type"] == "tool_use" and call["block"]["input"] == {} and call["block"]["name"] == "get_weather"
        assert json.loads(call["json"]) == {"city": "Paris"} and len(call["block"]["id"]) == 9
        assert events[-2][1]["delta"]["stop_reason"] == "tool_use"

    def test_text_then_two_tool_calls(self, client, monkeypatch):
        text = "Let me check both.\n" + TWO_CALLS
        serve(monkeypatch, FakeRuntime(text, pieces=pieces_of(text, 4)))
        events = sse(post(client, body(stream=True, messages=[USER], tools=[WEATHER, TIME])))
        assert_well_formed(events)
        said, first, second = blocks(events)
        assert said["text"] == "Let me check both."
        assert (first["block"]["name"], second["block"]["name"]) == ("get_weather", "get_time")
        assert json.loads(first["json"]) == {"city": "Paris"} and json.loads(second["json"]) == {}
        assert [b["index"] for n, b in events if n == "content_block_start"] == [0, 1, 2]

    def test_only_the_first_call_streams_when_parallel_calls_are_off(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(TWO_CALLS, pieces=pieces_of(TWO_CALLS, 9)))
        events = sse(post(client, body(stream=True, messages=[USER], tools=[WEATHER, TIME],
                                       tool_choice={"type": "any", "disable_parallel_tool_use": True})))
        assert_well_formed(events)
        assert [b["block"]["name"] for b in blocks(events)] == ["get_weather"]

    def test_stop_sequences_reach_the_model(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime("hi"))
        sse(post(client, body(stream=True, stop_sequences=["END"])))
        assert rt.calls[0]["stop"] == ["END"]

    def test_length_is_max_tokens(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("cut", finish="length"))
        assert sse(post(client, body(stream=True)))[-2][1]["delta"]["stop_reason"] == "max_tokens"


class TestStreamFailures:
    def test_an_engine_error_before_the_first_event_is_an_http_error(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(items=[ERROR_ITEM]))
        resp = post(client, body(stream=True))
        assert_error(resp, 500, "api_error", "the engine broke")
        assert resp.headers["content-type"].startswith("application/json") and resp.headers["x-potluck-error"] == "inference_error"

    def test_gpu_out_of_memory_is_overloaded(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(items=[{"__error__": "The GPU stopped this reply.", "__category__": "gpu_out_of_memory"}]))
        assert_error(post(client, body(stream=True)), 503, "overloaded_error")

    def test_a_model_that_changed_under_the_request_is_a_409(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(stream_error=ModelIdentityError("Requested model is no longer loaded.")))
        assert_error(post(client, body(stream=True)), 409, "invalid_request_error", "Requested model is no longer loaded.")

    def test_an_error_after_the_first_event_is_an_error_event(self, client, monkeypatch):
        first = {"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]}
        serve(monkeypatch, FakeRuntime(items=[first, ERROR_ITEM]))
        resp = post(client, body(stream=True))
        assert resp.status_code == 200
        events = parse_sse(sse_events(resp.text))
        assert names(events) == ["message_start", "ping", "content_block_start", "content_block_delta", "error"]
        assert events[-1][1] == {"type": "error", "error": {"type": "api_error", "message": "the engine broke"}}

    def test_a_quiet_engine_gets_the_stream_started_and_pings(self, client, monkeypatch):
        from potluck_local.routers import anthropic

        monkeypatch.setattr(anthropic, "COMMIT_WINDOW_S", 0.02)
        monkeypatch.setattr(anthropic, "PING_INTERVAL_S", 0.04)
        serve(monkeypatch, FakeRuntime("late", first_delay=0.3))
        events = sse(post(client, body(stream=True)))
        assert_well_formed(events)
        before = names(events)[: names(events).index("content_block_start")]
        assert before.count("ping") >= 3 and blocks(events)[0]["text"] == "late"

    def test_a_failure_after_a_quiet_start_is_an_error_event(self, client, monkeypatch):
        from potluck_local.routers import anthropic

        monkeypatch.setattr(anthropic, "COMMIT_WINDOW_S", 0.02)
        serve(monkeypatch, FakeRuntime(items=[OVERFLOW_ITEM], first_delay=0.2))
        resp = post(client, body(stream=True))
        assert resp.status_code == 200
        events = parse_sse(sse_events(resp.text))
        assert names(events) == ["message_start", "ping", "error"]
        assert events[-1][1]["error"] == {"type": "invalid_request_error", "message": PROMPT_TOO_LONG}


class TestPromptTooLong:
    def test_streamed(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(items=[OVERFLOW_ITEM]))
        resp = post(client, body(stream=True))
        assert_error(resp, 400, "invalid_request_error", PROMPT_TOO_LONG)
        assert resp.headers["x-potluck-error"] == "prompt_too_long"

    def test_not_streamed(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(chat_error=ValueError("Requested tokens (5000) exceed context window of 4096")))
        resp = post(client, body())
        assert_error(resp, 400, "invalid_request_error", PROMPT_TOO_LONG)
        assert resp.headers["x-potluck-error"] == "prompt_too_long"

    def test_a_prompt_exactly_as_long_as_the_window_is_refused_by_the_engine_and_says_so_truthfully(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(chat_error=ValueError("Requested tokens (4096) exceed context window of 4096")))
        assert_error(post(client, body()), 400, "invalid_request_error", "prompt is too long: 4096 tokens > 4095 maximum")

    def test_other_engine_errors_are_not_mistaken_for_it(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(chat_error=ValueError("something else")))
        assert_error(post(client, body()), 500, "api_error", "inference failed: ValueError")

    def test_the_engines_wording_is_still_the_one_this_route_reads(self):
        llama_cpp = pytest.importorskip("llama_cpp")
        import inspect

        from potluck_local.services import inference_errors

        source = inspect.getsource(llama_cpp.Llama)
        assert 'f"Requested tokens ({len(prompt_tokens)}) exceed context window of {llama_cpp.llama_n_ctx(self.ctx)}"' in source
        assert inference_errors.prompt_overflow("Requested tokens (5000) exceed context window of 4096") == (5000, 4096)

    def test_a_model_on_another_machine_reports_only_that_inference_failed(self, client, monkeypatch):
        # The household route sends fixed text, so the reason does not arrive.
        use_peer(monkeypatch, error=RemoteStreamError("Inference failed", "inference_error"))
        assert_error(post(client, body(model="remote-model")), 502, "api_error", "Inference failed")
        assert_error(post(client, body(model="remote-model", stream=True)), 502, "api_error", "Inference failed")


# ---------------------------------------------------------------------------
# Routing: the scope headers, a household peer, a circle, the pool
# ---------------------------------------------------------------------------

PEER = {"node_name": "peer", "mesh_ip": "100.64.0.2", "_capabilities": {"supports_tools": True}}
REMOTE_USAGE = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}


def use_peer(monkeypatch, reply="hello from the peer", *, error=None, usage=REMOTE_USAGE, supports_tools=True):
    seen = []

    async def peer_for(model, sha256=None):
        return {**PEER, "_capabilities": {"supports_tools": supports_tools}}

    async def stream_peer(peer, messages, model, temperature, max_tokens, **kw):
        seen.append({"messages": messages, "model": model, **kw})
        if error:
            raise error
        base = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": model}
        yield {**base, "choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": None}]}
        yield {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}

    async def complete_peer(peer, messages, model, temperature, max_tokens, **kw):
        seen.append({"messages": messages, "model": model, **kw})
        if error:
            raise error
        return {"id": "c", "created": 1, "model": model, "object": "chat.completion", "usage": usage,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}]}

    monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", peer_for)
    monkeypatch.setattr("potluck_local.services.gateway_routing.stream_peer_openai", stream_peer)
    monkeypatch.setattr("potluck_local.services.gateway_routing.complete_peer_openai", complete_peer)
    return seen


def use_pool(monkeypatch, reply="hello from the pool"):
    seen = []

    async def stream_pool(messages, model, temperature, max_tokens, scope=None, circle_id=None):
        seen.append({"scope": scope, "circle_id": circle_id, "model": model})
        base = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": model}
        yield {**base, "choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": None}]}
        yield {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": REMOTE_USAGE}

    async def complete_pool(messages, model, temperature, max_tokens, scope=None, circle_id=None):
        seen.append({"scope": scope, "circle_id": circle_id, "model": model})
        return {"id": "c", "created": 1, "model": model, "object": "chat.completion", "usage": REMOTE_USAGE,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}]}

    async def resolve(selector):
        return "11111111-1111-1111-1111-111111111111"

    monkeypatch.setattr("potluck_local.services.pool_routing.stream_pool_openai", stream_pool)
    monkeypatch.setattr("potluck_local.services.pool_routing.complete_pool_openai", complete_pool)
    monkeypatch.setattr("potluck_local.services.pool_routing.resolve_circle_id", resolve)
    return seen


class TestRouting:
    def test_a_household_peer_answers_and_its_missing_counts_are_estimated(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        seen = use_peer(monkeypatch)
        resp = post(client, body(model="remote-model"))
        assert resp.status_code == 200
        reply = resp.json()
        assert reply["model"] == "remote-model" and reply["content"] == [{"type": "text", "text": "hello from the peer"}]
        assert resp.headers["x-potluck-served-by"] == "household" and resp.headers["x-potluck-usage"] == "estimated"
        assert reply["usage"]["output_tokens"] == 5 and type(reply["usage"]["input_tokens"]) is int  # ceil(19 / 4)
        assert seen[0]["model"] == "remote-model"

    def test_a_household_peer_streams(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        use_peer(monkeypatch)
        resp = post(client, body(model="remote-model", stream=True))
        events = sse(resp)
        assert_well_formed(events)
        assert resp.headers["x-potluck-served-by"] == "household" and blocks(events)[0]["text"] == "hello from the peer"
        assert events[0][1]["message"]["model"] == "remote-model"
        assert events[-2][1]["usage"] == {"output_tokens": 5}

    def test_tools_go_to_a_peer_that_says_it_supports_them_and_not_to_one_that_does_not(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        seen = use_peer(monkeypatch, HERMES)
        reply = post(client, body(model="remote-model", messages=[USER], tools=[WEATHER])).json()
        assert reply["content"][0]["type"] == "tool_use" and seen[0]["tools"][0]["function"]["name"] == "get_weather"
        use_peer(monkeypatch, HERMES, supports_tools=False)
        assert "household machine" in assert_error(post(client, body(model="remote-model", messages=[USER], tools=[WEATHER])), 400, "invalid_request_error")

    def test_scope_local_refuses_another_machines_model(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        use_peer(monkeypatch)
        assert_error(post(client, body(model="remote-model"), **{"X-Potluck-Scope": "local"}), 409, "invalid_request_error",
                     "Requested model is not loaded locally. Load that model or use mine scope.")

    def test_scope_mine_with_an_unknown_model_is_a_404(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        assert "not loaded here" in assert_error(post(client, body(model="nobody-has-this"), **{"X-Potluck-Scope": "mine"}), 404, "not_found_error")

    def test_an_unknown_model_is_a_404_without_the_pool(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        assert_error(post(client, body(model="nobody-has-this")), 404, "not_found_error")

    def test_no_model_loaded_is_a_503(self, client, monkeypatch):
        class Empty:
            def is_loaded(self): return False
            def current_model_id(self): return None
        serve(monkeypatch, Empty())
        nameless = body()
        del nameless["model"]
        assert_error(post(client, nameless), 503, "overloaded_error", "No model loaded on this node, and no household peer matched.")
        assert_error(post(client, {**nameless, "stream": True}), 503, "overloaded_error")
        assert_error(post(client, body()), 404, "not_found_error")  # a named model that is not loaded

    def test_the_open_pool_when_opted_in(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: True)
        seen = use_pool(monkeypatch)
        resp = post(client, body(model="pool-model"))
        assert resp.headers["x-potluck-served-by"] == "pool" and resp.json()["content"][0]["text"] == "hello from the pool"
        assert seen[0]["scope"] == "pool" and seen[0]["circle_id"] is None

    def test_a_trusted_circle_by_header(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        seen = use_pool(monkeypatch)
        resp = post(client, body(model="circle-model", stream=True), **{"X-Potluck-Scope": "circle", "X-Potluck-Circle": "friends"})
        assert resp.headers["x-potluck-served-by"] == "circle" and blocks(sse(resp))[0]["text"] == "hello from the pool"
        assert seen[0]["scope"] == "circle" and seen[0]["circle_id"] == "11111111-1111-1111-1111-111111111111"

    def test_tools_are_refused_on_a_circle_request_with_a_400(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        use_pool(monkeypatch)
        resp = post(client, body(model="circle-model", messages=[USER], tools=[WEATHER]), **{"X-Potluck-Scope": "circle"})
        assert "not available on circle requests" in assert_error(resp, 400, "invalid_request_error")

    def test_a_coordinator_refusal_keeps_its_status(self, client, monkeypatch):
        from potluck_local.services import pool_routing

        serve(monkeypatch, FakeRuntime())
        use_pool(monkeypatch)

        async def refuse(*a, **kw):
            raise pool_routing.PoolDispatchError(503, '{"detail": "no peer serves this model"}')

        monkeypatch.setattr("potluck_local.services.pool_routing.complete_pool_openai", refuse)
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: True)
        assert_error(post(client, body(model="pool-model")), 503, "overloaded_error", "no peer serves this model")

    def test_a_gpu_failure_on_this_machine_is_a_503(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(chat_error=InferenceFailure("gpu_out_of_memory", "The GPU stopped this reply.")))
        resp = post(client, body())
        assert_error(resp, 503, "overloaded_error", "The GPU stopped this reply.")
        assert resp.headers["x-potluck-error"] == "gpu_out_of_memory"


# ---------------------------------------------------------------------------
# Errors in Anthropic's shape
# ---------------------------------------------------------------------------


class TestErrorShape:
    def test_bad_json(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        resp = client.post("/v1/messages", content=b"{not json", headers={"content-type": "application/json"})
        assert_error(resp, 400, "invalid_request_error", "The request body is not valid JSON.")
        assert resp.headers["x-potluck-error"] == "invalid_json"

    def test_an_empty_body(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        assert_error(client.post("/v1/messages"), 400, "invalid_request_error")

    @pytest.mark.parametrize("payload", [[1, 2], "text", 5, "null"])
    def test_a_body_of_the_wrong_shape(self, client, monkeypatch, payload):
        serve(monkeypatch, FakeRuntime())
        assert_error(post(client, payload), 400, "invalid_request_error", "The request body must be a JSON object.")

    @pytest.mark.parametrize("payload, code", [
        ({"model": "local-model"}, "invalid_messages"),
        (body(messages=[]), "invalid_messages"),
        (body(max_tokens="many"), "invalid_max_tokens"),
        (body(stream="yes"), "invalid_stream"),
        (body(messages=[{"role": "user", "content": [{"type": "image", "source": {}}]}]), "unsupported_content_block"),
        (body(messages=[{"role": "user", "content": [{"type": "document", "source": {}}]}]), "unsupported_content_block"),
        (body(tools=[{"type": "web_search_20250305", "name": "web_search"}]), "unsupported_tool"),
        (body(tools=[{"type": "bash_20250124", "name": "bash"}]), "unsupported_tool"),
        (body(output_config={"format": {"type": "json_schema", "schema": {}}}), "unsupported_output_format"),
        (body(tools=[WEATHER], tool_choice={"type": "tool", "name": "other"}), "invalid_tool_choice"),
    ])
    def test_refusals(self, client, monkeypatch, payload, code):
        rt = serve(monkeypatch, FakeRuntime())
        resp = post(client, payload)
        assert_error(resp, 400, "invalid_request_error")
        assert resp.headers["x-potluck-error"] == code
        assert rt.calls == []

    def test_a_server_tool_is_named_by_type(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        message = assert_error(post(client, body(tools=[{"type": "web_search_20250305", "name": "web_search"}])), 400, "invalid_request_error")
        assert "web_search_20250305" in message

    def test_a_body_that_is_too_large(self, client, monkeypatch):
        from potluck_local.services import anthropic_messages

        serve(monkeypatch, FakeRuntime())
        monkeypatch.setattr(anthropic_messages, "MAX_BODY_BYTES", 200)
        assert_error(post(client, body(system="x" * 500)), 413, "request_too_large", "The request body is too large.")

    def test_output_config_without_a_format_is_accepted(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        assert post(client, body(output_config={"effort": "high"})).status_code == 200

    def test_an_unexpected_failure_is_an_api_error_that_leaks_nothing(self, client, monkeypatch):
        async def boom(*a, **kw):
            raise RuntimeError("secret internal detail")

        serve(monkeypatch, FakeRuntime())
        monkeypatch.setattr("potluck_local.routers.gateway.plan_chat", boom)
        message = assert_error(post(client, body()), 500, "api_error")
        assert "secret" not in message

    def test_the_other_routes_keep_the_old_error_shape(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        resp = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}], "model": "nobody-has-this"})
        assert resp.status_code == 404 and set(resp.json()) == {"detail"}


# ---------------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------------


class TestCountTokens:
    def count(self, client, payload, **headers):
        return client.post("/v1/messages/count_tokens?beta=true", json=payload, headers=headers)

    def test_the_loaded_models_tokenizer_counts_the_system_messages_and_tools(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime(tokens=987))
        resp = self.count(client, {"model": "local-model", "system": "SYS", "messages": [USER], "tools": [WEATHER]})
        assert resp.status_code == 200 and resp.json() == {"input_tokens": 987} and "x-potluck-usage" not in resp.headers
        assert "SYS" in rt.counted[0] and "Paris" in rt.counted[0] and '"get_weather"' in rt.counted[0]

    def test_no_max_tokens_is_needed_and_no_model_means_the_loaded_one(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime(tokens=11))
        assert self.count(client, {"messages": [USER]}).json() == {"input_tokens": 11} and len(rt.counted) == 1

    def test_another_model_gets_a_length_estimate(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime(tokens=987))
        resp = self.count(client, {"model": "claude-sonnet-4", "messages": [{"role": "user", "content": "x" * 400}]})
        assert resp.json()["input_tokens"] >= 100 and resp.json()["input_tokens"] != 987
        assert resp.headers["x-potluck-usage"] == "estimated" and rt.counted == []

    def test_an_engine_that_cannot_tokenize_gets_a_length_estimate(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(tokens=None))
        resp = self.count(client, {"model": "local-model", "messages": [{"role": "user", "content": "x" * 400}]})
        assert resp.json()["input_tokens"] >= 100 and resp.headers["x-potluck-usage"] == "estimated"

    def test_it_works_with_no_model_loaded_and_loads_nothing(self, client, monkeypatch):
        class Empty:
            def is_loaded(self): return False
            def current_model_id(self): return None
        serve(monkeypatch, Empty())
        resp = self.count(client, {"model": "local-model", "messages": [USER]})
        assert resp.status_code == 200 and resp.json()["input_tokens"] >= 1

    def test_it_never_runs_the_model(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime(tokens=5))
        self.count(client, {"model": "local-model", "messages": [USER]})
        assert rt.calls == []

    @pytest.mark.parametrize("payload", [
        {"model": "m", "messages": [{"role": "user", "content": [{"type": "image", "source": {}}]}]},
        {"model": "m", "messages": []}, {"model": "m"}, [1],
    ])
    def test_a_bad_body_is_an_anthropic_error(self, client, monkeypatch, payload):
        serve(monkeypatch, FakeRuntime())
        assert_error(self.count(client, payload), 400, "invalid_request_error")

    def test_bad_json(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        assert_error(client.post("/v1/messages/count_tokens", content=b"nope"), 400, "invalid_request_error")


# ---------------------------------------------------------------------------
# The gateway key
# ---------------------------------------------------------------------------


@pytest.fixture()
def keyed(monkeypatch, settings):
    monkeypatch.setenv("POTLUCK_GATEWAY_API_KEY", "gw-secret")
    from potluck_local.app import create_app

    async def no_peer(model, sha256=None):
        return None

    monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", no_peer)
    serve(monkeypatch, FakeRuntime())
    return TestClient(create_app(), client=_LOOPBACK, raise_server_exceptions=False)


class TestAuth:
    def test_authorization_bearer(self, keyed):
        assert post(keyed, body(), authorization="Bearer gw-secret").status_code == 200

    def test_x_api_key(self, keyed):
        assert post(keyed, body(), **{"x-api-key": "gw-secret"}).status_code == 200

    def test_x_api_key_on_count_tokens(self, keyed):
        assert keyed.post("/v1/messages/count_tokens", json=body(), headers={"x-api-key": "gw-secret"}).status_code == 200

    def test_either_header_may_carry_the_key(self, keyed):
        assert post(keyed, body(), authorization="Bearer something-else", **{"x-api-key": "gw-secret"}).status_code == 200
        assert post(keyed, body(), authorization="Bearer gw-secret", **{"x-api-key": "wrong"}).status_code == 200

    @pytest.mark.parametrize("headers", [
        {}, {"x-api-key": "wrong"}, {"authorization": "Bearer wrong"}, {"x-api-key": ""}, {"authorization": "Bearer "},
        {"authorization": "gw-secret"}, {"x-api-key": "gw-secre"}, {"x-api-key": "gw-secret-and-more"},
    ])
    def test_a_missing_or_wrong_key_is_a_401_in_anthropic_shape(self, keyed, headers):
        assert_error(post(keyed, body(), **headers), 401, "authentication_error", "missing or invalid gateway API key")
        assert_error(keyed.post("/v1/messages/count_tokens", json=body(), headers=headers), 401, "authentication_error")

    def test_a_key_with_non_ascii_text_is_a_401_and_not_a_crash(self, keyed):
        resp = keyed.post("/v1/messages", content=json.dumps(body()).encode(),
                          headers={"x-api-key": "gw-sécret".encode("latin-1"), "content-type": "application/json"})
        assert_error(resp, 401, "authentication_error")

    def test_the_other_gateway_routes_accept_x_api_key_and_keep_their_error_shape(self, keyed):
        assert keyed.get("/v1/models", headers={"x-api-key": "gw-secret"}).status_code == 200
        assert keyed.get("/v1/models", headers={"authorization": "Bearer gw-secret"}).status_code == 200
        refused = keyed.get("/v1/models")
        assert refused.status_code == 401 and refused.json() == {"detail": "missing or invalid gateway API key"}

    def test_the_gateway_switched_off_is_a_404_in_anthropic_shape(self, monkeypatch, settings):
        monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", "sidecar-secret")
        from potluck_local.app import create_app

        off = TestClient(create_app(), client=_LOOPBACK, raise_server_exceptions=False)
        assert_error(post(off, body()), 404, "not_found_error", "gateway disabled; enable it in Settings -> Connect tools")
        assert_error(off.post("/v1/messages/count_tokens", json=body()), 404, "not_found_error")
        assert off.get("/v1/models").json() == {"detail": "gateway disabled; enable it in Settings -> Connect tools"}

    def test_a_settings_key_works_as_x_api_key(self, monkeypatch, settings):
        settings.gateway.enabled = True
        settings.gateway.api_key = "from-settings"
        from potluck_local.app import create_app

        serve(monkeypatch, FakeRuntime())
        app = TestClient(create_app(), client=_LOOPBACK, raise_server_exceptions=False)
        assert post(app, body(), **{"x-api-key": "from-settings"}).status_code == 200
        assert_error(post(app, body(), **{"x-api-key": "nope"}), 401, "authentication_error")

    def test_a_caller_off_this_machine_is_a_403_in_anthropic_shape(self, monkeypatch, settings):
        from potluck_local.app import create_app

        remote = TestClient(create_app(), client=("203.0.113.9", 4000), raise_server_exceptions=False)
        assert_error(post(remote, body()), 403, "permission_error")
        assert "detail" in remote.get("/v1/models").json()


# ---------------------------------------------------------------------------
# What Claude Code sends
# ---------------------------------------------------------------------------


def claude_code_shaped_request():
    """The same fields and nesting as the first request Claude Code sends, with short made-up text."""
    return {
        "model": "local-model",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "<reminder>made-up context</reminder>"},
                                         {"type": "text", "text": "list the files here"}]},
            {"role": "system", "content": [{"type": "text", "text": "made-up late rule", "cache_control": {"type": "ephemeral", "ttl": "5m"}}]},
        ],
        "system": [{"type": "text", "text": "made-up billing line"},
                   {"type": "text", "text": "You are a made-up agent.", "cache_control": {"type": "ephemeral"}},
                   {"type": "text", "text": "Made-up long instructions.", "cache_control": {"type": "ephemeral", "scope": "global"}}],
        "tools": [
            {"name": "Bash", "description": "Run a command", "input_schema": {
                "$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}},
                "required": ["command"], "additionalProperties": False}},
            {"name": "Read", "description": "Read a file", "input_schema": {
                "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}},
        ],
        "metadata": {"user_id": "{\"device_id\":\"x\",\"session_id\":\"y\"}"},
        "max_tokens": 32000,
        "thinking": {"type": "adaptive", "display": "omitted"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "safeguards": [{"type": "dangerous_tool_use", "classifier_context": {"v": 1, "permission_mode": "auto"}}],
        "output_config": {"effort": "high"},
        "stream": True,
    }


CLAUDE_CODE_HEADERS = {
    "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14,context-management-2025-06-27,effort-2025-11-24",
    "anthropic-version": "2023-06-01", "x-api-key": "gw-secret", "x-app": "cli", "user-agent": "claude-cli/2.1.295 (external, sdk-cli)",
    "anthropic-dangerous-direct-browser-access": "true", "x-stainless-timeout": "600",
}


class TestClaudeCodeShape:
    def test_the_request_is_accepted_and_the_model_sees_one_system_message_first(self, keyed, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime("Here are the files.", pieces=["Here are ", "the files."]))
        resp = post(keyed, claude_code_shaped_request(), **CLAUDE_CODE_HEADERS)
        events = sse(resp)
        assert_well_formed(events)
        assert blocks(events)[0]["text"] == "Here are the files."
        (call,) = rt.calls
        assert call["messages"][0] == {"role": "system", "content": "made-up billing line\n\nYou are a made-up agent.\n\n"
                                                                   "Made-up long instructions.\n\nmade-up late rule"}
        assert call["messages"][1:] == [{"role": "user", "content": "<reminder>made-up context</reminder>\n\nlist the files here"}]
        assert [t["function"]["name"] for t in call["tools"]] == ["Bash", "Read"]
        assert call["tools"][0]["function"]["parameters"]["additionalProperties"] is False
        assert call["max_tokens"] == 32000 and call["tool_choice"] == "auto"

    def test_a_tool_call_comes_back_as_tool_use(self, keyed, monkeypatch):
        call_text = '<tool_call>\n{"name": "Bash", "arguments": {"command": "ls"}}\n</tool_call>'
        serve(monkeypatch, FakeRuntime(call_text, pieces=pieces_of(call_text, 8)))
        events = sse(post(keyed, claude_code_shaped_request(), **CLAUDE_CODE_HEADERS))
        (call,) = blocks(events)
        assert call["block"]["name"] == "Bash" and json.loads(call["json"]) == {"command": "ls"}
        assert events[-2][1]["delta"]["stop_reason"] == "tool_use"

    def test_the_thinking_model_keeps_its_reasoning_out_of_the_reply(self, keyed, monkeypatch):
        serve(monkeypatch, FakeRuntime(THINKING, pieces=pieces_of(THINKING, 7)))
        events = sse(post(keyed, claude_code_shaped_request(), **CLAUDE_CODE_HEADERS))
        assert [b["block"]["type"] for b in blocks(events)] == ["text"]

    def test_the_same_request_without_streaming(self, keyed, monkeypatch):
        serve(monkeypatch, FakeRuntime("ok"))
        resp = post(keyed, {**claude_code_shaped_request(), "stream": False}, **CLAUDE_CODE_HEADERS)
        assert resp.status_code == 200 and resp.json()["content"] == [{"type": "text", "text": "ok"}]

    def test_count_tokens_takes_the_same_body(self, keyed, monkeypatch):
        serve(monkeypatch, FakeRuntime(tokens=321))
        payload = {k: v for k, v in claude_code_shaped_request().items() if k not in ("max_tokens", "stream")}
        resp = keyed.post("/v1/messages/count_tokens?beta=true", json=payload, headers=CLAUDE_CODE_HEADERS)
        assert resp.json() == {"input_tokens": 321}


# ---------------------------------------------------------------------------
# Usage rows and logs: counts and ids only
# ---------------------------------------------------------------------------


class TestUsageAndLogs:
    def record(self, monkeypatch):
        rows = []

        async def fake(scope, model, **fields):
            rows.append((scope, model, fields))

        monkeypatch.setattr("potluck_local.routers.gateway._record_consumed", fake)
        return rows

    def test_a_whole_reply_records_the_engines_counts(self, client, monkeypatch):
        rows = self.record(monkeypatch)
        serve(monkeypatch, FakeRuntime())
        post(client, body())
        assert rows == [("local", "local-model", {"input_tokens": 7, "output_tokens": 5})]

    def test_a_stream_records_one_row_counting_deltas(self, client, monkeypatch):
        rows = self.record(monkeypatch)
        serve(monkeypatch, FakeRuntime("abc", pieces=["a", "b", "c"]))
        post(client, body(stream=True))
        ((scope, model, fields),) = rows
        assert (scope, model, fields["output_tokens"], fields["ok"]) == ("local", "local-model", 3, True)
        assert fields["ttft_ms"] is not None and set(fields) == {"output_tokens", "ttft_ms", "duration_ms", "ok"}

    def test_a_stream_that_fails_is_recorded_as_failed(self, client, monkeypatch):
        rows = self.record(monkeypatch)
        first = {"choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]}
        serve(monkeypatch, FakeRuntime(items=[first, ERROR_ITEM]))
        post(client, body(stream=True))
        assert rows[0][2]["ok"] is False

    def test_no_prompt_reply_or_tool_text_reaches_a_log_line(self, client, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG)
        secret = "ZEBRA-SECRET-4471"
        serve(monkeypatch, FakeRuntime(f'<tool_call>\n{{"name": "get_weather", "arguments": {{"city": "{secret}-ARGS"}}}}\n</tool_call>'))
        history = [{"role": "user", "content": f"{secret}-USER"},
                   {"role": "assistant", "content": [{"type": "tool_use", "id": "call1", "name": "get_weather", "input": {"city": f"{secret}-HISTORY"}}]},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call1", "content": f"{secret}-RESULT"}]}]
        for stream in (False, True):
            post(client, body(messages=history, tools=[WEATHER], system=f"{secret}-SYSTEM", stream=stream))
        serve(monkeypatch, FakeRuntime(chat_error=ValueError(f"Requested tokens (5000) exceed context window of 4096")))
        post(client, body(messages=history, tools=[WEATHER], system=f"{secret}-SYSTEM"))
        client.post("/v1/messages/count_tokens", json=body(messages=history, system=f"{secret}-SYSTEM"))
        assert any("gateway_messages" in r.getMessage() for r in caplog.records)
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# The OpenAI route, unchanged
# ---------------------------------------------------------------------------


class StrictRuntime:
    """Signatures exactly as before the Messages route: a call with a new keyword argument would fail."""

    def __init__(self, text="pong"):
        self.text, self.seen = text, []

    def is_loaded(self): return True
    def current_model_id(self): return "local-model"

    async def chat(self, messages, *, temperature, max_tokens, expected_model, expected_sha256):
        self.seen.append(("chat", messages, temperature, max_tokens))
        return completion(self.text)

    def stream_chat_raw(self, messages, *, temperature, max_tokens, stop, expected_model, expected_sha256):
        self.seen.append(("stream", messages, temperature, max_tokens, stop))

        async def gen():
            for chunk in llama_chunks(pieces_of(self.text, 3)):
                yield chunk
        return gen()


class TestOpenAIRouteUnchanged:
    def test_a_reply_and_the_call_the_model_receives(self, client, monkeypatch):
        rt = serve(monkeypatch, StrictRuntime("pong"))
        resp = client.post("/v1/chat/completions", json={"messages": [USER], "stop": ["END"], "max_tokens": 9, "temperature": 0.3})
        assert resp.status_code == 200 and resp.json() == completion("pong")
        assert resp.headers["x-potluck-served-by"] == "local"
        # stop is still not passed to a reply that is not streamed
        assert rt.seen == [("chat", [USER], 0.3, 9)]

    def test_a_stream_is_the_raw_chunks_and_a_done(self, client, monkeypatch):
        rt = serve(monkeypatch, StrictRuntime("Hello there, friend."))
        resp = client.post("/v1/chat/completions", json={"messages": [USER], "stream": True, "stop": "END"})
        want = "".join(f"data: {json.dumps(c)}\n\n" for c in llama_chunks(pieces_of("Hello there, friend.", 3)))
        assert resp.text == want + "data: [DONE]\n\n"
        assert rt.seen == [("stream", [USER], 0.7, 2048, ["END"])]

    def test_the_messages_route_without_stop_sequences_makes_the_same_call(self, client, monkeypatch):
        rt = serve(monkeypatch, StrictRuntime("pong"))
        assert post(client, body(messages=[USER], temperature=0.3, max_tokens=9)).status_code == 200
        assert post(client, body(messages=[USER], temperature=0.3, max_tokens=9, stream=True)).status_code == 200
        assert rt.seen[0] == ("chat", [USER], 0.3, 9) and rt.seen[1] == ("stream", [USER], 0.3, 9, None)

    def test_errors_keep_the_old_shape(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(chat_error=ValueError("Requested tokens (5000) exceed context window of 4096")))
        resp = client.post("/v1/chat/completions", json={"messages": [USER]})
        assert resp.status_code == 500 and resp.json() == {"detail": "inference failed: ValueError"}
        serve(monkeypatch, FakeRuntime(items=[OVERFLOW_ITEM]))
        resp = client.post("/v1/chat/completions", json={"messages": [USER], "stream": True})
        assert resp.status_code == 200
        assert resp.text == ('data: {"error": {"message": "Requested tokens (5000) exceed context window of 4096", '
                             '"type": "inference_error", "code": "inference_error"}}\n\ndata: [DONE]\n\n')


# ---------------------------------------------------------------------------
# The runtime pieces this route added
# ---------------------------------------------------------------------------


class TestRuntimePieces:
    def test_count_tokens_uses_the_loaded_models_tokenizer(self):
        from potluck_local.services.runtime import LLMRuntime

        class Tokenizer:
            def tokenize(self, text, add_bos=True, special=False):
                assert isinstance(text, bytes) and add_bos is False
                return list(text.split())

        runtime = LLMRuntime()
        assert asyncio.run(runtime.count_tokens("one two three")) is None  # nothing loaded
        runtime._model = Tokenizer()
        assert asyncio.run(runtime.count_tokens("one two three")) == 3

    def test_count_tokens_is_none_when_the_tokenizer_fails_or_is_missing(self):
        from potluck_local.services.runtime import LLMRuntime

        class Broken:
            def tokenize(self, text, add_bos=True, special=False):
                raise OSError("native failure")

        runtime = LLMRuntime()
        runtime._model = Broken()
        assert asyncio.run(runtime.count_tokens("x")) is None
        runtime._model = object()
        assert asyncio.run(runtime.count_tokens("x")) is None

    def test_chat_passes_stop_only_when_given(self):
        from potluck_local.services.runtime import LLMRuntime

        seen = []

        class Model:
            def create_chat_completion(self, **kw):
                seen.append(kw)
                return completion("ok")

        runtime = LLMRuntime()
        runtime._model, runtime._model_id = Model(), "m"
        asyncio.run(runtime.chat([USER]))
        asyncio.run(runtime.chat([USER], stop=["END"]))
        assert "stop" not in seen[0] and seen[1]["stop"] == ["END"]


class TestACountNeverHoldsAReplyUp:
    """The tokenizer waits behind a request queued for the model. A count gets a
    moment, then the length estimate is used."""

    def test_a_slow_tokenizer_gives_way_to_the_estimate(self, monkeypatch):
        from potluck_local.routers import anthropic as route

        class Stuck:
            async def count_tokens(self, text):
                await asyncio.sleep(30)

        monkeypatch.setattr(route, "COUNT_WAIT_S", 0.05)
        started = time.monotonic()
        assert asyncio.run(route._counted(Stuck(), "one two three")) is None  # noqa: SLF001
        assert time.monotonic() - started < 2

    def test_a_quick_tokenizer_is_used_and_a_runtime_without_one_is_skipped(self):
        from potluck_local.routers import anthropic as route

        class Quick:
            async def count_tokens(self, text):
                return 3

        assert asyncio.run(route._counted(Quick(), "one two three")) == 3  # noqa: SLF001
        assert asyncio.run(route._counted(object(), "one two three")) is None  # noqa: SLF001


class TestARefusedRouteIsLoggedWithItsModelName:
    def test_the_model_name_is_logged_plain_and_an_odd_one_as_unknown(self, monkeypatch, caplog):
        from fastapi import HTTPException

        from potluck_local.routers import anthropic as route

        async def refuse(job, scope, circle):
            raise HTTPException(404, f"model '{job.model}' is not loaded here or on a reachable household peer")

        monkeypatch.setattr(route.gateway, "plan_chat", refuse)
        app = __import__("fastapi").FastAPI()
        app.include_router(route.router, prefix="/v1")
        client = TestClient(app)
        with caplog.at_level(logging.INFO, logger="potluck_local.routers.anthropic"):
            first = client.post("/v1/messages", json=body(model="some-other-model"))
            second = client.post("/v1/messages", json=body(model="a name with spaces and a secret"))
        assert first.status_code == second.status_code == 404
        assert first.json()["error"]["type"] == "not_found_error"
        lines = [r.getMessage() for r in caplog.records if "gateway_messages_refused" in r.getMessage()]
        assert lines == ["gateway_messages_refused status=404 model=some-other-model",
                         "gateway_messages_refused status=404 model=unknown"]

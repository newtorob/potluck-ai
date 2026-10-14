"""POST /v1/chat/completions and /v1/completions: what a reply says.

Contract:
  * a whole reply served by another machine (household, circle, pool) has
    whole-number token counts, estimated from text length when the other machine
    gave none, with X-Potluck-Usage: estimated; counts the engine gave stay as they
    are, with no header; the usage row recorded uses the same numbers;
  * a prompt that does not fit this machine's model is a 400 with the code
    context_length_exceeded (a streamed reply keeps its shape: an error object in a
    data: line, then [DONE]);
  * `model` is the model id in whole replies and in every chunk, never the model
    file's path;
  * `stop` reaches this machine's model for a reply that is not streamed, as it does
    for a streamed one.

Same setup as test_gateway_reasoning_tools.py: TestClient with a loopback client
address, gateway auth off, no real model.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from potluck_local.services.model_traits import TemplateTraits
from potluck_local.services.remote_stream import RemoteStreamError

_LOOPBACK = ("127.0.0.1", 50000)

MODEL_PATH = "/Users/someone/.potluck/models/x/model.gguf"
COUNTED = {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}
SYSTEM = {"role": "system", "content": "abcd"}
USER = {"role": "user", "content": "efghi"}  # with SYSTEM: "abcd\nefghi", 10 characters, 3 tokens
WEATHER = {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}
HERMES = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>'
OVERFLOW = "Requested tokens (5000) exceed context window of 4096"
CONTEXT_MESSAGE = "This model's maximum context length is 4096 tokens. The prompt is 5000 tokens."


def llama_chunks(pieces, finish="stop", usage=None, model=MODEL_PATH):
    """What llama-cpp-python streams. It names the model file's path as the model."""
    base = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": model}
    yield {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "logprobs": None, "finish_reason": None}]}
    for piece in pieces:
        yield {**base, "choices": [{"index": 0, "delta": {"content": piece}, "logprobs": None, "finish_reason": None}]}
    last = {**base, "choices": [{"index": 0, "delta": {}, "logprobs": None, "finish_reason": finish}]}
    if usage:
        last["usage"] = usage
    yield last


def completion(text, usage=COUNTED, model=MODEL_PATH):
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "logprobs": None, "finish_reason": "stop"}],
            "usage": usage}


class Engine:
    """A loaded llama.cpp model that names its file path as its model, as the real one does."""

    def __init__(self, reply="pong", *, pieces=None, usage=COUNTED, chat_error=None, items=None):
        self.reply, self.usage, self.chat_error, self.items = reply, usage, chat_error, items
        self.pieces = pieces if pieces is not None else [reply]
        self.calls: list[dict] = []

    def is_loaded(self): return True
    def current_model_id(self): return "local-model"
    def template_traits(self): return TemplateTraits(supports_tools=True)

    async def chat(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        if self.chat_error:
            raise self.chat_error
        return completion(self.reply, self.usage)

    def stream_chat_raw(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})

        async def gen():
            for chunk in (self.items if self.items is not None else llama_chunks(self.pieces)):
                yield chunk
        return gen()


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

    async def no_peer(model, sha256=None):
        return None

    monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", no_peer)
    monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: False)
    return TestClient(create_app(), client=_LOOPBACK, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def rows(monkeypatch):
    """The usage rows the gateway records."""
    seen: list[dict] = []

    async def record(scope, model_slug, **fields):
        seen.append({"scope": scope, "model": model_slug, **fields})

    monkeypatch.setattr("potluck_local.routers.gateway._record_consumed", record)
    return seen


def serve(monkeypatch, runtime):
    monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: runtime)
    return runtime


def post(client, **fields):
    headers = fields.pop("headers", {})
    return client.post("/v1/chat/completions", json={"messages": [SYSTEM, USER], **fields}, headers=headers)


def events(resp):
    out = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            out.append("[DONE]" if line[6:] == "[DONE]" else json.loads(line[6:]))
    return out


def remote_completion(model, text, usage):
    return {"id": "x", "created": 1, "model": model, "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": usage}


NO_COUNTS = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}


def use_peer(monkeypatch, *, text="hello from peer", usage=NO_COUNTS, model=None):
    peer = {"node_name": "mac", "mesh_ip": "100.64.0.2", "_capabilities": {"supports_tools": True}}

    async def peer_for_model(m, sha256=None):
        return peer

    async def complete_peer_openai(p, messages, m, temperature, max_tokens, **kw):
        return remote_completion(model or m, text, dict(usage))

    async def stream_peer_openai(p, messages, m, temperature, max_tokens, **kw):
        for chunk in llama_chunks([text], model=model or m, usage=dict(usage)):
            yield chunk

    monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", peer_for_model)
    monkeypatch.setattr("potluck_local.services.gateway_routing.complete_peer_openai", complete_peer_openai)
    monkeypatch.setattr("potluck_local.services.gateway_routing.stream_peer_openai", stream_peer_openai)


def use_pool(monkeypatch, *, text="hi from pool", usage=NO_COUNTS):
    async def complete_pool_openai(messages, model, temperature, max_tokens, scope=None, circle_id=None):
        return remote_completion(model, text, dict(usage))

    async def resolve(selector):
        return "11111111-1111-1111-1111-111111111111"

    monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: True)
    monkeypatch.setattr("potluck_local.services.pool_routing.complete_pool_openai", complete_pool_openai)
    monkeypatch.setattr("potluck_local.services.pool_routing.resolve_circle_id", resolve)


# ---------------------------------------------------------------------------
# Token counts of a reply served by another machine
# ---------------------------------------------------------------------------


class TestTokenCountsOfRemoteReplies:
    def test_a_household_reply_has_whole_numbers_and_says_they_are_estimates(self, client, monkeypatch, rows):
        serve(monkeypatch, Engine())
        use_peer(monkeypatch, text="hello from peer")  # 15 characters, 4 tokens
        resp = post(client, model="remote-model")
        assert resp.status_code == 200
        assert resp.json()["usage"] == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
        assert resp.headers["x-potluck-usage"] == "estimated" and resp.headers["x-potluck-served-by"] == "household"

    def test_the_usage_row_has_the_same_numbers(self, client, monkeypatch, rows):
        serve(monkeypatch, Engine())
        use_peer(monkeypatch)
        post(client, model="remote-model")
        assert rows == [{"scope": "my_machines", "model": "remote-model", "input_tokens": 3, "output_tokens": 4}]

    @pytest.mark.parametrize("scope, served_by", [("circle", "circle"), (None, "pool")])
    def test_a_circle_or_pool_reply_keeps_the_count_it_has_and_estimates_the_rest(self, client, monkeypatch, rows, scope, served_by):
        serve(monkeypatch, Engine())
        use_pool(monkeypatch, usage={"prompt_tokens": None, "completion_tokens": 3, "total_tokens": None})
        resp = post(client, model="pool-model", headers={"X-Potluck-Scope": scope} if scope else {})
        assert resp.headers["x-potluck-served-by"] == served_by
        assert resp.json()["usage"] == {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}
        assert resp.headers["x-potluck-usage"] == "estimated"
        assert rows[0]["input_tokens"] == 3 and rows[0]["output_tokens"] == 3

    def test_counts_the_other_machine_gave_stay_with_no_header(self, client, monkeypatch, rows):
        serve(monkeypatch, Engine())
        use_peer(monkeypatch, usage=COUNTED)
        resp = post(client, model="remote-model")
        assert resp.json()["usage"] == COUNTED and "x-potluck-usage" not in resp.headers
        assert rows[0]["input_tokens"] == 7 and rows[0]["output_tokens"] == 5

    def test_a_reply_from_this_machines_own_model_is_left_alone(self, client, monkeypatch, rows):
        serve(monkeypatch, Engine())
        resp = post(client)
        assert resp.json()["usage"] == COUNTED and "x-potluck-usage" not in resp.headers
        assert rows == [{"scope": "local", "model": "local-model", "input_tokens": 7, "output_tokens": 5}]

    def test_the_prompt_estimate_covers_the_tools_the_model_is_shown(self, client, monkeypatch, rows):
        serve(monkeypatch, Engine())
        use_peer(monkeypatch, text=HERMES)
        plain = post(client, model="remote-model").json()["usage"]["prompt_tokens"]
        with_tools = post(client, model="remote-model", tools=[WEATHER]).json()["usage"]["prompt_tokens"]
        assert with_tools > plain == 3

    def test_the_completion_estimate_counts_the_calls_in_the_reply(self, client, monkeypatch, rows):
        serve(monkeypatch, Engine())
        use_peer(monkeypatch, text=HERMES)
        body = post(client, model="remote-model", tools=[WEATHER]).json()
        assert body["choices"][0]["finish_reason"] == "tool_calls"
        # the call is in the reply as a name and arguments; the text of the tags is not
        assert body["usage"]["completion_tokens"] == -(-(len("get_weather") + len('{"city": "Paris"}')) // 4)

    def test_a_reply_that_failed_has_no_header_and_no_row_is_invented(self, client, monkeypatch, rows):
        serve(monkeypatch, Engine())

        async def peer_for_model(m, sha256=None):
            return {"node_name": "mac", "mesh_ip": "100.64.0.2", "_capabilities": {}}

        async def broken(*a, **kw):
            raise RemoteStreamError("Inference failed", "inference_error")

        monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", peer_for_model)
        monkeypatch.setattr("potluck_local.services.gateway_routing.complete_peer_openai", broken)
        resp = post(client, model="remote-model")
        assert resp.status_code == 502 and "x-potluck-usage" not in resp.headers and rows == []

    def test_the_messages_route_still_reports_its_own_estimates(self, client, monkeypatch, rows):
        serve(monkeypatch, Engine())
        use_peer(monkeypatch, text="hello from peer")
        resp = client.post("/v1/messages", json={"model": "remote-model", "max_tokens": 9,
                                                 "messages": [{"role": "user", "content": "efghi"}]})
        assert resp.status_code == 200 and resp.headers["x-potluck-usage"] == "estimated"
        assert resp.json()["usage"]["input_tokens"] == 2 and resp.json()["usage"]["output_tokens"] == 4


# ---------------------------------------------------------------------------
# A prompt that does not fit
# ---------------------------------------------------------------------------


class TestAPromptThatDoesNotFit:
    def test_a_reply_that_is_not_streamed_is_a_400_with_the_code(self, client, monkeypatch):
        serve(monkeypatch, Engine(chat_error=ValueError(OVERFLOW)))
        resp = post(client)
        assert resp.status_code == 400
        assert resp.json() == {"detail": {"code": "context_length_exceeded", "message": CONTEXT_MESSAGE}}

    def test_a_streamed_reply_keeps_its_shape_with_a_typed_error_object(self, client, monkeypatch):
        serve(monkeypatch, Engine(items=[{"__error__": OVERFLOW, "__category__": "inference_error"}]))
        resp = post(client, stream=True)
        assert resp.status_code == 200
        error = {"message": CONTEXT_MESSAGE, "type": "invalid_request_error", "code": "context_length_exceeded"}
        assert resp.text == f"data: {json.dumps({'error': error})}\n\ndata: [DONE]\n\n"

    def test_the_legacy_completions_route_answers_the_same_way(self, client, monkeypatch):
        serve(monkeypatch, Engine(chat_error=ValueError(OVERFLOW)))
        resp = client.post("/v1/completions", json={"prompt": "hi"})
        assert resp.status_code == 400
        assert resp.json() == {"detail": {"code": "context_length_exceeded", "message": CONTEXT_MESSAGE}}
        serve(monkeypatch, Engine(items=[{"__error__": OVERFLOW, "__category__": "inference_error"}]))
        resp = client.post("/v1/completions", json={"prompt": "hi", "stream": True})
        error = {"message": CONTEXT_MESSAGE, "type": "invalid_request_error", "code": "context_length_exceeded"}
        # (this route has always sent its closing chunk after an error line; that is not changed here)
        assert resp.status_code == 200 and events(resp)[0] == {"error": error} and events(resp)[-1] == "[DONE]"

    def test_the_window_and_the_prompt_come_from_the_engines_message(self, client, monkeypatch):
        serve(monkeypatch, Engine(chat_error=ValueError("Requested tokens (4096) exceed context window of 4096")))
        message = post(client).json()["detail"]["message"]
        assert message == "This model's maximum context length is 4096 tokens. The prompt is 4096 tokens."

    def test_other_engine_errors_are_still_a_500_and_an_inference_error(self, client, monkeypatch):
        serve(monkeypatch, Engine(chat_error=ValueError("something else")))
        resp = post(client)
        assert resp.status_code == 500 and resp.json() == {"detail": "inference failed: ValueError"}
        serve(monkeypatch, Engine(items=[{"__error__": "the engine broke", "__category__": "inference_error"}]))
        assert events(post(client, stream=True))[0]["error"] == {
            "message": "the engine broke", "type": "inference_error", "code": "inference_error"}

    def test_the_words_of_another_machine_are_never_taken_for_it(self, client, monkeypatch):
        serve(monkeypatch, Engine())
        use_peer(monkeypatch)

        async def peer_says_it(*a, **kw):
            raise RemoteStreamError(OVERFLOW, "inference_error")

        monkeypatch.setattr("potluck_local.services.gateway_routing.complete_peer_openai", peer_says_it)
        resp = post(client, model="remote-model")
        assert resp.status_code == 502 and resp.json()["detail"]["code"] == "inference_error"

    def test_the_messages_route_keeps_its_own_words(self, client, monkeypatch):
        serve(monkeypatch, Engine(chat_error=ValueError(OVERFLOW)))
        resp = client.post("/v1/messages", json={"model": "local-model", "max_tokens": 9, "messages": [USER]})
        assert resp.status_code == 400 and resp.headers["x-potluck-error"] == "prompt_too_long"
        assert resp.json()["error"] == {"type": "invalid_request_error", "message": "prompt is too long: 5000 tokens > 4095 maximum"}

    def test_one_place_recognises_it(self):
        from potluck_local.routers import gateway
        from potluck_local.services.model_identity import ModelIdentityError
        from potluck_local.services.pool_routing import PoolDispatchError

        assert gateway.context_overflow(ValueError(OVERFLOW)) == (5000, 4096)
        assert gateway.context_overflow({"__error__": OVERFLOW, "__category__": "inference_error"}) == (5000, 4096)
        assert gateway.context_overflow(ValueError("something else")) is None
        assert gateway.context_overflow({"__error__": "boom"}) is None
        assert gateway.context_overflow({}) is None and gateway.context_overflow(None) is None
        assert gateway.context_overflow(ModelIdentityError("gone")) is None
        assert gateway.context_overflow(RemoteStreamError(OVERFLOW)) is None
        assert gateway.context_overflow(PoolDispatchError(400, OVERFLOW)) is None


# ---------------------------------------------------------------------------
# The model a reply names
# ---------------------------------------------------------------------------


class TestTheModelAReplyNames:
    def test_a_whole_reply_names_the_model_id(self, client, monkeypatch):
        serve(monkeypatch, Engine())
        body = post(client).json()
        assert body["model"] == "local-model" and MODEL_PATH not in json.dumps(body)

    def test_every_streamed_chunk_names_the_model_id(self, client, monkeypatch):
        serve(monkeypatch, Engine(pieces=["po", "ng"]))
        resp = post(client, stream=True)
        chunks = [e for e in events(resp) if e != "[DONE]"]
        assert len(chunks) == 4 and {c["model"] for c in chunks} == {"local-model"}
        assert MODEL_PATH not in resp.text

    def test_it_holds_when_reasoning_and_tool_calls_are_split_out(self, client, monkeypatch):
        text = "<think>\nhm\n</think>\n\nOk. " + HERMES
        serve(monkeypatch, Engine(text, pieces=[text[i:i + 6] for i in range(0, len(text), 6)]))
        for stream in (False, True):
            resp = post(client, stream=stream, tools=[WEATHER])
            assert MODEL_PATH not in resp.text
        chunks = [e for e in events(resp) if e != "[DONE]"]
        assert chunks and all(c["model"] == "local-model" for c in chunks)

    def test_the_reply_names_the_model_the_engine_loaded_when_the_request_names_it(self, client, monkeypatch):
        serve(monkeypatch, Engine())
        assert post(client, model="local-model").json()["model"] == "local-model"

    def test_the_legacy_completions_route_never_named_the_path_and_still_does_not(self, client, monkeypatch):
        serve(monkeypatch, Engine())
        whole = client.post("/v1/completions", json={"prompt": "hi"})
        streamed = client.post("/v1/completions", json={"prompt": "hi", "stream": True})
        assert whole.json()["model"] == "local-model"
        assert all(e["model"] == "local-model" for e in events(streamed) if e != "[DONE]")
        assert MODEL_PATH not in whole.text + streamed.text

    def test_a_reply_relayed_from_another_machine_names_the_model_asked_for(self, client, monkeypatch):
        serve(monkeypatch, Engine())
        use_peer(monkeypatch, model="/home/other/models/y.gguf")  # a machine that leaks its path
        whole = post(client, model="remote-model")
        streamed = post(client, model="remote-model", stream=True)
        assert whole.json()["model"] == "remote-model"
        assert {e["model"] for e in events(streamed) if e != "[DONE]"} == {"remote-model"}
        assert "/home/other" not in whole.text + streamed.text

    def test_an_error_chunk_is_left_as_it_is(self, client, monkeypatch):
        serve(monkeypatch, Engine(items=[{"__error__": "boom", "__category__": "inference_error"}]))
        assert events(post(client, stream=True))[0]["error"]["message"] == "boom"


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


class TestStop:
    @pytest.mark.parametrize("stream", [False, True])
    def test_stop_reaches_this_machines_model_whether_or_not_the_reply_is_streamed(self, client, monkeypatch, stream):
        rt = serve(monkeypatch, Engine())
        post(client, stream=stream, stop=["END", "STOP"])
        post(client, stream=stream, stop="END")
        assert [c["stop"] for c in rt.calls] == [["END", "STOP"], ["END"]]

    def test_without_stop_a_reply_that_is_not_streamed_is_asked_for_none(self, client, monkeypatch):
        rt = serve(monkeypatch, Engine())
        post(client)
        assert "stop" not in rt.calls[0]

    def test_the_job_has_no_flag_for_the_old_behaviour(self):
        from potluck_local.routers import gateway

        assert "stop_when_blocking" not in gateway.ChatJob.__dataclass_fields__

    def test_the_messages_route_still_passes_its_stop_sequences(self, client, monkeypatch):
        rt = serve(monkeypatch, Engine())
        client.post("/v1/messages", json={"model": "local-model", "max_tokens": 9, "stop_sequences": ["END"],
                                          "messages": [USER]})
        assert rt.calls[0]["stop"] == ["END"]

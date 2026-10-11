"""POST /v1/chat/completions: reasoning kept out of the answer, and tool calls.

Contract:
  * `<think>` text goes to message.reasoning_content (delta.reasoning_content when
    streaming) and `content` holds only the answer, for this machine's model and
    for replies relayed from a household peer, a circle or the pool.
    reasoning_format "none" opts out.
  * tools, tool_choice, assistant tool_calls and role "tool" messages are
    accepted; the model's tool-call text comes back as tool_calls.
  * a model whose template has no tool section, a peer that does not advertise
    tools, and circle or pool requests with tools are refused with a clear 400.
  * a request without tools reaches the model and comes back as it always did.

Same setup as test_gateway_scope.py: TestClient with a loopback client address,
gateway auth off, no real model (the runtime is a fake).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from potluck_local.services.model_traits import TemplateTraits

_LOOPBACK = ("127.0.0.1", 50000)

WEATHER = {"type": "function", "function": {"name": "get_weather", "description": "Weather",
                                            "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                                           "required": ["city"]}}}
TIME = {"type": "function", "function": {"name": "get_time"}}
USER = {"role": "user", "content": "What's the weather in Paris right now? Use the tool."}
HERMES = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>'
ANSWER = "A mesh network is a network where every node relays data for the others."
THINKING = "<think>\nOkay, the user wants a definition. Every node relays.\n</think>\n\n" + ANSWER
TOOLS_OK = TemplateTraits(supports_tools=True)


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


def llama_chunks(pieces, finish="stop", usage=None):
    """The chunks llama-cpp-python streams: a role chunk, content pieces, then an empty delta with the finish reason."""
    base = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "local-model"}
    yield {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "logprobs": None, "finish_reason": None}]}
    for piece in pieces:
        yield {**base, "choices": [{"index": 0, "delta": {"content": piece}, "logprobs": None, "finish_reason": None}]}
    last = {**base, "choices": [{"index": 0, "delta": {}, "logprobs": None, "finish_reason": finish}]}
    if usage:
        last["usage"] = usage
    yield last


def completion(text, finish="stop"):
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "local-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "logprobs": None,
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}}


class FakeRuntime:
    """A loaded llama.cpp model. `reply` is the text it will produce; `pieces` how it arrives when streamed."""

    def __init__(self, reply="pong", *, pieces=None, finish="stop", traits=TOOLS_OK):
        self.reply, self.finish = reply, finish
        self.pieces = pieces if pieces is not None else [reply]
        self.traits = traits
        self.calls: list[dict] = []

    def is_loaded(self): return True
    def current_model_id(self): return "local-model"
    def template_traits(self): return self.traits

    async def chat(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        return completion(self.reply, self.finish)

    def stream_chat_raw(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})

        async def gen():
            for chunk in llama_chunks(self.pieces, self.finish):
                yield chunk
        return gen()


def serve(monkeypatch, runtime):
    monkeypatch.setattr("potluck_local.routers.gateway.get_runtime", lambda: runtime)
    return runtime


def events(resp):
    out = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            out.append("[DONE]" if line[6:] == "[DONE]" else json.loads(line[6:]))
    return out


def stream_text(resp):
    """(reasoning, content, tool_calls, finish_reason, deltas) from a streamed reply."""
    reasoning = content = ""
    calls: list[dict] = []
    finish = None
    deltas = []
    for ev in events(resp):
        if ev == "[DONE]":
            continue
        choice = ev["choices"][0]
        delta = choice["delta"]
        deltas.append(delta)
        reasoning += delta.get("reasoning_content") or ""
        content += delta.get("content") or ""
        calls += delta.get("tool_calls") or []
        finish = choice["finish_reason"] or finish
    return reasoning, content, calls, finish, deltas


def pieces_of(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)]


def post(client, body, **headers):
    return client.post("/v1/chat/completions", json=body, headers=headers)


# ---------------------------------------------------------------------------
# A request without tools or reasoning is unchanged
# ---------------------------------------------------------------------------


class StrictRuntime:
    """Signatures exactly as before this change: a call with a new keyword argument would fail."""

    def __init__(self, text="pong"):
        self.text, self.seen = text, []

    def is_loaded(self): return True
    def current_model_id(self): return "local-model"

    async def chat(self, messages, *, temperature, max_tokens, expected_model, expected_sha256):
        self.seen.append(messages)
        return completion(self.text)

    def stream_chat_raw(self, messages, *, temperature, max_tokens, stop, expected_model, expected_sha256):
        self.seen.append(messages)

        async def gen():
            for chunk in llama_chunks(pieces_of(self.text, 3)):
                yield chunk
        return gen()


class TestPlainRequestsAreUnchanged:
    def test_non_streaming_reply_and_the_messages_the_model_receives(self, client, monkeypatch):
        rt = serve(monkeypatch, StrictRuntime("pong"))
        resp = post(client, {"messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "ping"}]})
        assert resp.status_code == 200
        assert resp.json() == completion("pong")
        assert rt.seen == [[{"role": "system", "content": "be brief"}, {"role": "user", "content": "ping"}]]

    def test_streamed_reply_is_the_same_bytes_as_the_raw_chunks(self, client, monkeypatch):
        serve(monkeypatch, StrictRuntime("Hello there, friend."))
        resp = post(client, {"messages": [USER], "stream": True})
        want = "".join(f"data: {json.dumps(c)}\n\n" for c in llama_chunks(pieces_of("Hello there, friend.", 3)))
        assert resp.text == want + "data: [DONE]\n\n"

    def test_a_tag_that_only_looks_like_one_late_in_the_answer_is_left_alone(self, client, monkeypatch):
        text = "Write <think> to open a block."
        serve(monkeypatch, FakeRuntime(text, pieces=pieces_of(text, 4)))
        body = post(client, {"messages": [USER]}).json()["choices"][0]["message"]
        assert body == {"role": "assistant", "content": text}
        _, content, _, _, _ = stream_text(post(client, {"messages": [USER], "stream": True}))
        assert content == text

    def test_message_content_may_be_null_or_text_parts(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime("ok"))
        resp = post(client, {"messages": [
            {"role": "system", "content": [{"type": "text", "text": "be"}, {"type": "text", "text": "brief"}]},
            {"role": "assistant", "content": None},
            {"role": "user", "content": "hi"}]})
        assert resp.status_code == 200
        assert rt.calls[0]["messages"] == [{"role": "system", "content": "be\nbrief"},
                                           {"role": "assistant", "content": ""}, {"role": "user", "content": "hi"}]

    def test_the_old_422_for_a_missing_role_stays_a_422(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        assert post(client, {"messages": [{"content": "no role"}]}).status_code == 422


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------


class TestReasoningNonStreaming:
    def test_reasoning_moves_to_reasoning_content(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(THINKING))
        choice = post(client, {"messages": [USER]}).json()["choices"][0]
        assert choice["message"] == {"role": "assistant", "content": ANSWER,
                                     "reasoning_content": "Okay, the user wants a definition. Every node relays."}
        assert choice["finish_reason"] == "stop"

    def test_the_empty_block_of_no_think_leaves_only_the_answer(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("<think>\n\n</think>\n\n" + ANSWER))
        assert post(client, {"messages": [USER]}).json()["choices"][0]["message"] == {"role": "assistant", "content": ANSWER}

    def test_a_template_that_opens_the_block_itself(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("Let me work it out. 17 x 23 = 391.\n</think>\n\n391",
                                       traits=TemplateTraits(opens_reasoning=True, thinks=True)))
        message = post(client, {"messages": [USER]}).json()["choices"][0]["message"]
        assert message["reasoning_content"] == "Let me work it out. 17 x 23 = 391." and message["content"] == "391"

    def test_a_close_tag_with_no_opener_is_text_when_this_template_does_not_open_the_block(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("About </think> tags.", traits=TOOLS_OK))
        assert post(client, {"messages": [USER]}).json()["choices"][0]["message"]["content"] == "About </think> tags."

    def test_a_reply_cut_off_inside_the_block_is_all_reasoning_and_keeps_the_engines_finish_reason(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("<think>\nOkay, so the user asks", finish="length"))
        choice = post(client, {"messages": [USER]}).json()["choices"][0]
        assert choice["message"]["content"] == "" and choice["message"]["reasoning_content"] == "Okay, so the user asks"
        assert choice["finish_reason"] == "length"

    def test_opting_out_gives_the_raw_text_back(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(THINKING))
        for value in ("none",):
            message = post(client, {"messages": [USER], "reasoning_format": value}).json()["choices"][0]["message"]
            assert message == {"role": "assistant", "content": THINKING}

    @pytest.mark.parametrize("value", ["auto", "deepseek"])
    def test_the_other_values_split(self, client, monkeypatch, value):
        serve(monkeypatch, FakeRuntime(THINKING))
        message = post(client, {"messages": [USER], "reasoning_format": value}).json()["choices"][0]["message"]
        assert message["content"] == ANSWER and "reasoning_content" in message

    def test_an_unknown_reasoning_format_is_a_422(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(THINKING))
        assert post(client, {"messages": [USER], "reasoning_format": "deepseek-legacy"}).status_code == 422

    def test_usage_and_model_are_kept(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(THINKING))
        body = post(client, {"messages": [USER]}).json()
        assert body["usage"] == {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12} and body["model"] == "local-model"


class TestReasoningStreaming:
    @pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 1000])
    def test_reasoning_and_answer_arrive_in_their_own_deltas_at_any_chunk_size(self, client, monkeypatch, size):
        serve(monkeypatch, FakeRuntime(THINKING, pieces=pieces_of(THINKING, size)))
        resp = post(client, {"messages": [USER], "stream": True})
        reasoning, content, calls, finish, deltas = stream_text(resp)
        assert reasoning == "Okay, the user wants a definition. Every node relays."
        assert content == ANSWER and calls == [] and finish == "stop"
        assert all("<think" not in (d.get("content") or "") and "think>" not in (d.get("content") or "") for d in deltas)
        assert events(resp)[-1] == "[DONE]"

    def test_the_role_chunk_and_the_finish_chunk_are_the_engines_own(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(THINKING, pieces=pieces_of(THINKING, 6)))
        evs = [e for e in events(post(client, {"messages": [USER], "stream": True})) if e != "[DONE]"]
        assert evs[0]["choices"][0]["delta"] == {"role": "assistant"}
        assert evs[-1]["choices"][0] == {"index": 0, "delta": {}, "logprobs": None, "finish_reason": "stop"}
        assert {e["id"] for e in evs} == {"chatcmpl-1"}

    def test_a_stream_cut_off_inside_the_block_ends_with_the_engines_finish_reason(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("", pieces=pieces_of("<think>\nOkay, so the user asks", 4), finish="length"))
        reasoning, content, _, finish, _ = stream_text(post(client, {"messages": [USER], "stream": True}))
        assert reasoning == "Okay, so the user asks" and content == "" and finish == "length"

    def test_a_template_that_opens_the_block_streams_its_reasoning_as_reasoning(self, client, monkeypatch):
        text = "Hmm, 17 x 23.\n</think>\n\n391"
        serve(monkeypatch, FakeRuntime(text, pieces=pieces_of(text, 3), traits=TemplateTraits(opens_reasoning=True)))
        reasoning, content, _, _, _ = stream_text(post(client, {"messages": [USER], "stream": True}))
        assert (reasoning, content) == ("Hmm, 17 x 23.", "391")

    def test_opting_out_streams_the_raw_text(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(THINKING, pieces=pieces_of(THINKING, 5)))
        resp = post(client, {"messages": [USER], "stream": True, "reasoning_format": "none"})
        reasoning, content, _, _, _ = stream_text(resp)
        assert reasoning == "" and content == THINKING

    def test_the_stream_does_not_hold_the_whole_answer_back_for_a_plain_reply(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("x", pieces=["Hello", " there", ", friend."]))
        _, _, _, _, deltas = stream_text(post(client, {"messages": [USER], "stream": True}))
        assert [d["content"] for d in deltas if "content" in d] == ["Hello", " there", ", friend."]


# ---------------------------------------------------------------------------
# Tool calls, this machine's model
# ---------------------------------------------------------------------------


def only_message(resp):
    assert resp.status_code == 200, resp.text
    choice = resp.json()["choices"][0]
    return choice["message"], choice["finish_reason"]


class TestToolCallsNonStreaming:
    def test_a_call_comes_back_as_tool_calls(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime(HERMES))
        message, finish = only_message(post(client, {"messages": [USER], "tools": [WEATHER]}))
        assert finish == "tool_calls" and message["content"] is None and message["role"] == "assistant"
        (call,) = message["tool_calls"]
        assert call["type"] == "function" and call["function"]["name"] == "get_weather"
        assert isinstance(call["function"]["arguments"], str)
        assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}
        assert len(call["id"]) == 9 and call["id"].isalnum()
        assert rt.calls[0]["tools"] == [WEATHER] and rt.calls[0]["tool_choice"] == "auto"
        assert rt.calls[0]["messages"] == [USER]

    def test_text_before_the_call_stays_as_content(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("I'll look that up.\n" + HERMES))
        message, finish = only_message(post(client, {"messages": [USER], "tools": [WEATHER]}))
        assert message["content"] == "I'll look that up." and finish == "tool_calls"

    def test_several_calls_in_one_reply(self, client, monkeypatch):
        second = '<tool_call>\n{"name": "get_time", "arguments": {"zone": "CET"}}\n</tool_call>'
        serve(monkeypatch, FakeRuntime(HERMES + "\n" + second))
        message, _ = only_message(post(client, {"messages": [USER], "tools": [WEATHER, TIME]}))
        assert [c["function"]["name"] for c in message["tool_calls"]] == ["get_weather", "get_time"]
        assert len({c["id"] for c in message["tool_calls"]}) == 2

    def test_the_llama_json_form_is_read_too(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime('{"name": "get_weather", "parameters": {"city": "Paris"}}'))
        message, finish = only_message(post(client, {"messages": [USER], "tools": [WEATHER]}))
        assert message["tool_calls"][0]["function"]["name"] == "get_weather" and finish == "tool_calls"

    def test_reasoning_and_a_call_in_one_reply(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("<think>\nThe user wants weather.\n</think>\n\n" + HERMES))
        message, finish = only_message(post(client, {"messages": [USER], "tools": [WEATHER]}))
        assert message["reasoning_content"] == "The user wants weather." and message["content"] is None
        assert message["tool_calls"][0]["function"]["name"] == "get_weather" and finish == "tool_calls"

    @pytest.mark.parametrize("reply", [
        '<tool_call>\n{"name": "get_weather", "arguments": {"city": \n</tool_call>',   # malformed
        '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}',         # cut off
        '<tool_call>\n{"name": "launch_missiles", "arguments": {}}\n</tool_call>',      # not offered
        "It is sunny in Paris.",
    ])
    def test_anything_that_is_not_a_clean_call_stays_plain_content(self, client, monkeypatch, reply):
        serve(monkeypatch, FakeRuntime(reply))
        message, finish = only_message(post(client, {"messages": [USER], "tools": [WEATHER]}))
        assert message == {"role": "assistant", "content": reply} and finish == "stop"

    def test_a_call_cut_off_by_the_token_limit_keeps_finish_reason_length(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(HERMES, finish="length"))
        message, finish = only_message(post(client, {"messages": [USER], "tools": [WEATHER]}))
        assert message["tool_calls"] and finish == "length"

    def test_without_tools_the_same_text_is_just_content(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(HERMES))
        message, finish = only_message(post(client, {"messages": [USER]}))
        assert message == {"role": "assistant", "content": HERMES} and finish == "stop"

    def test_tool_choice_none_does_not_show_the_tools_and_reads_no_call(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime(HERMES))
        message, finish = only_message(post(client, {"messages": [USER], "tools": [WEATHER], "tool_choice": "none"}))
        assert message["content"] == HERMES and "tool_calls" not in message and finish == "stop"
        assert "tools" not in rt.calls[0] and "tool_choice" not in rt.calls[0]

    @pytest.mark.parametrize("choice", ["required", {"type": "function", "function": {"name": "get_weather"}}])
    def test_forced_choices_reach_the_model(self, client, monkeypatch, choice):
        rt = serve(monkeypatch, FakeRuntime('{"name": "get_weather", "arguments": {"city": "Rome"}}'))
        message, finish = only_message(post(client, {"messages": [USER], "tools": [WEATHER, TIME], "tool_choice": choice}))
        assert rt.calls[0]["tool_choice"] == choice
        assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"city": "Rome"} and finish == "tool_calls"

    def test_the_second_turn_gives_the_model_the_call_and_its_result(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime("It is 17 degrees in Paris."))
        history = [USER,
                   {"role": "assistant", "content": None, "tool_calls": [
                       {"id": "call12345", "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}]},
                   {"role": "tool", "tool_call_id": "call12345", "content": [{"type": "text", "text": '{"temp_c": 17}'}]}]
        message, finish = only_message(post(client, {"messages": history, "tools": [WEATHER]}))
        assert message["content"] == "It is 17 degrees in Paris." and finish == "stop"
        sent = rt.calls[0]["messages"]
        assert sent[1] == {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call12345", "type": "function", "function": {"name": "get_weather", "arguments": {"city": "Paris"}}}]}
        assert sent[2] == {"role": "tool", "content": '{"temp_c": 17}', "tool_call_id": "call12345"}


class TestToolCallsStreaming:
    def test_the_call_is_held_back_and_arrives_as_one_tool_calls_delta(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(HERMES, pieces=pieces_of(HERMES, 4)))
        resp = post(client, {"messages": [USER], "tools": [WEATHER], "stream": True})
        _, content, calls, finish, deltas = stream_text(resp)
        assert content == "" and finish == "tool_calls"
        (call,) = calls
        assert call["index"] == 0 and call["type"] == "function" and call["function"]["name"] == "get_weather"
        assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}
        assert not any("tool_call" in (d.get("content") or "") for d in deltas)
        assert events(resp)[-1] == "[DONE]"

    @pytest.mark.parametrize("size", [1, 2, 3, 7, 1000])
    def test_content_before_the_call_streams_and_no_half_tag_leaks(self, client, monkeypatch, size):
        text = "I'll look that up.\n" + HERMES
        serve(monkeypatch, FakeRuntime(text, pieces=pieces_of(text, size)))
        resp = post(client, {"messages": [USER], "tools": [WEATHER], "stream": True})
        _, content, calls, finish, deltas = stream_text(resp)
        assert content == "I'll look that up." and len(calls) == 1 and finish == "tool_calls"
        assert all("<" not in (d.get("content") or "") for d in deltas)

    def test_several_calls_are_indexed(self, client, monkeypatch):
        text = HERMES + '\n<tool_call>\n{"name": "get_time", "arguments": {}}\n</tool_call>'
        serve(monkeypatch, FakeRuntime(text, pieces=pieces_of(text, 9)))
        _, _, calls, finish, _ = stream_text(post(client, {"messages": [USER], "tools": [WEATHER, TIME], "stream": True}))
        assert [(c["index"], c["function"]["name"]) for c in calls] == [(0, "get_weather"), (1, "get_time")] and finish == "tool_calls"

    def test_reasoning_streams_first_then_the_call(self, client, monkeypatch):
        text = "<think>\nThe user wants weather.\n</think>\n\n" + HERMES
        serve(monkeypatch, FakeRuntime(text, pieces=pieces_of(text, 5)))
        reasoning, content, calls, finish, _ = stream_text(post(client, {"messages": [USER], "tools": [WEATHER], "stream": True}))
        assert reasoning == "The user wants weather." and content == "" and len(calls) == 1 and finish == "tool_calls"

    def test_a_reply_that_is_not_a_call_comes_out_as_it_was_sent(self, client, monkeypatch):
        text = "It is sunny in Paris; see <tool_calls> in the docs. [1]"
        serve(monkeypatch, FakeRuntime(text, pieces=pieces_of(text, 5)))
        _, content, calls, finish, _ = stream_text(post(client, {"messages": [USER], "tools": [WEATHER], "stream": True}))
        assert content == text and calls == [] and finish == "stop"

    def test_malformed_call_text_streams_out_as_content_at_the_end(self, client, monkeypatch):
        text = '<tool_call>\n{"name": "get_weather", "arguments": {"city": \n</tool_call>'
        serve(monkeypatch, FakeRuntime(text, pieces=pieces_of(text, 6)))
        _, content, calls, finish, _ = stream_text(post(client, {"messages": [USER], "tools": [WEATHER], "stream": True}))
        assert content == text and calls == [] and finish == "stop"

    def test_usage_on_the_closing_chunk_is_kept(self, client, monkeypatch):
        class Rt(FakeRuntime):
            def stream_chat_raw(self, messages, **kw):
                async def gen():
                    for c in llama_chunks(pieces_of(HERMES, 8), usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}):
                        yield c
                return gen()

        serve(monkeypatch, Rt())
        evs = [e for e in events(post(client, {"messages": [USER], "tools": [WEATHER], "stream": True})) if e != "[DONE]"]
        assert evs[-1]["usage"] == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
        assert evs[-1]["choices"][0]["finish_reason"] == "tool_calls"
        assert not any("usage" in e for e in evs[:-1])

    def test_an_engine_error_still_ends_the_stream_with_an_error_event(self, client, monkeypatch):
        class Rt(FakeRuntime):
            def stream_chat_raw(self, messages, **kw):
                async def gen():
                    yield {"choices": [{"delta": {"content": "Partial "}, "finish_reason": None}]}
                    yield {"__error__": "device lost", "__category__": "inference_error"}
                return gen()

        serve(monkeypatch, Rt())
        resp = post(client, {"messages": [USER], "tools": [WEATHER], "stream": True})
        assert '"error"' in resp.text and "device lost" in resp.text


# ---------------------------------------------------------------------------
# Refusals and validation
# ---------------------------------------------------------------------------


NO_TOOLS = TemplateTraits(supports_tools=False)


class TestToolRefusals:
    @pytest.mark.parametrize("stream", [False, True])
    def test_a_model_whose_template_has_no_tool_section_is_refused_clearly(self, client, monkeypatch, stream):
        rt = serve(monkeypatch, FakeRuntime(traits=NO_TOOLS))
        resp = post(client, {"messages": [USER], "tools": [WEATHER], "stream": stream})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["code"] == "tools_not_supported"
        assert "loaded model does not support tool calls" in detail["message"]
        assert rt.calls == []

    def test_a_backend_that_cannot_say_is_refused_too(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(traits=None))
        assert post(client, {"messages": [USER], "tools": [WEATHER]}).status_code == 400

    def test_tool_messages_alone_are_refused_on_a_model_without_tool_support(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(traits=NO_TOOLS))
        resp = post(client, {"messages": [USER, {"role": "tool", "content": "{}", "tool_call_id": "x"}]})
        assert resp.status_code == 400 and resp.json()["detail"]["code"] == "tools_not_supported"

    def test_tool_choice_none_needs_no_tool_support(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime("plain", traits=NO_TOOLS))
        assert post(client, {"messages": [USER], "tools": [WEATHER], "tool_choice": "none"}).status_code == 200
        assert "tools" not in rt.calls[0]

    @pytest.mark.parametrize("body,code", [
        ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:,x"}}]}]}, "unsupported_content_part"),
        ({"messages": [USER], "tools": "get_weather"}, None),
        ({"messages": [USER], "tools": [{"type": "function", "function": {"name": "bad name"}}]}, "invalid_tools"),
        ({"messages": [USER], "tools": [{"type": "web_search"}]}, "invalid_tools"),
        ({"messages": [USER], "tools": [WEATHER], "tool_choice": "sometimes"}, "invalid_tool_choice"),
        ({"messages": [USER], "tools": [WEATHER], "tool_choice": {"type": "function", "function": {"name": "nope"}}}, "invalid_tool_choice"),
        ({"messages": [USER], "tool_choice": "required"}, "invalid_tool_choice"),
        ({"messages": [USER, {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "get_weather", "arguments": "{oops"}}]}], "tools": [WEATHER]}, "invalid_tool_calls"),
    ])
    def test_bad_requests_get_a_400_that_says_what_is_wrong(self, client, monkeypatch, body, code):
        rt = serve(monkeypatch, FakeRuntime())
        resp = post(client, body)
        if code is None:  # a type error pydantic catches first
            assert resp.status_code == 422
        else:
            assert resp.status_code == 400 and resp.json()["detail"]["code"] == code
            assert resp.json()["detail"]["message"]
        assert rt.calls == []

    def test_the_image_message_names_the_part_type(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        resp = post(client, {"messages": [{"role": "user", "content": [{"type": "text", "text": "what is this"},
                                                                        {"type": "image_url", "image_url": {"url": "x"}}]}]})
        assert resp.status_code == 400 and "image_url" in resp.json()["detail"]["message"]


class TestWhatTheGatewayAdvertises:
    def test_capabilities_say_whether_the_loaded_model_supports_tools(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(traits=TOOLS_OK))
        body = client.get("/v1/potluck/capabilities").json()
        assert body["supports_tools"] is True and body["model_lock_version"] == 1 and body["scopes"] == ["local", "mine"]
        serve(monkeypatch, FakeRuntime(traits=NO_TOOLS))
        assert client.get("/v1/potluck/capabilities").json()["supports_tools"] is False
        serve(monkeypatch, FakeRuntime(traits=None))
        assert client.get("/v1/potluck/capabilities").json()["supports_tools"] is False

    def test_the_model_list_marks_the_loaded_model(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(traits=TOOLS_OK))

        async def none():
            return {}

        monkeypatch.setattr("potluck_local.services.gateway_routing.household_model_map", none)
        monkeypatch.setattr("potluck_local.routers.gateway.list_models", lambda: [])
        entries = client.get("/v1/models").json()["data"]
        assert [(e["id"], e["supports_tools"]) for e in entries] == [("local-model", True)]

    def test_the_model_list_marks_household_models_from_their_capabilities(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(traits=TOOLS_OK))

        async def household():
            return {"qwen3-8b-q4": {"mesh_ip": "100.64.0.2", "_capabilities": {"supports_tools": True}},
                    "old-model": {"mesh_ip": "100.64.0.3", "_capabilities": {}}}

        monkeypatch.setattr("potluck_local.services.gateway_routing.household_model_map", household)
        monkeypatch.setattr("potluck_local.routers.gateway.list_models", lambda: [])
        by_id = {e["id"]: e for e in client.get("/v1/models").json()["data"]}
        assert by_id["qwen3-8b-q4"]["supports_tools"] is True and by_id["old-model"]["supports_tools"] is False
        assert by_id["qwen3-8b-q4"]["owned_by"] == "potluck-household"


# ---------------------------------------------------------------------------
# Household peers
# ---------------------------------------------------------------------------


PEER_CAPS = {"loaded_model": "peer-model", "available_for_inference": True}


def peer_chunks(text, size=5, finish="stop"):
    base = {"id": "chatcmpl-p", "object": "chat.completion.chunk", "created": 1, "model": "peer-model"}
    pieces = [{**base, "choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}]} for p in pieces_of(text, size)]
    return [*pieces, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                      "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}}]


class FakeHousehold:
    def __init__(self, monkeypatch, text, *, supports_tools=None, size=5):
        self.text, self.size = text, size
        self.stream_calls: list[dict] = []
        self.complete_calls: list[dict] = []
        caps = dict(PEER_CAPS)
        if supports_tools is not None:
            caps["supports_tools"] = supports_tools
        peer = {"node_name": "mac", "mesh_ip": "100.64.0.2", "_capabilities": caps}

        async def peer_for_model(model, sha256=None):
            return peer if model == "peer-model" else None

        def stream_peer_openai(p, messages, model, temperature, max_tokens, **kw):
            self.stream_calls.append({"messages": messages, **kw})

            async def gen():
                for c in peer_chunks(self.text, self.size):
                    yield c
            return gen()

        async def complete_peer_openai(p, messages, model, temperature, max_tokens, **kw):
            self.complete_calls.append({"messages": messages, **kw})
            from potluck_local.services.remote_stream import collect_completion

            async def gen():
                for c in peer_chunks(self.text, self.size):
                    yield c
            return await collect_completion(gen())

        monkeypatch.setattr("potluck_local.services.gateway_routing.peer_for_model", peer_for_model)
        monkeypatch.setattr("potluck_local.services.gateway_routing.stream_peer_openai", stream_peer_openai)
        monkeypatch.setattr("potluck_local.services.gateway_routing.complete_peer_openai", complete_peer_openai)


class TestHouseholdPeer:
    def test_tools_and_tool_choice_are_passed_to_a_peer_that_advertises_them(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        peer = FakeHousehold(monkeypatch, HERMES, supports_tools=True)
        resp = post(client, {"model": "peer-model", "messages": [USER], "tools": [WEATHER], "tool_choice": "required"})
        message, finish = only_message(resp)
        assert peer.complete_calls[0]["tools"] == [WEATHER] and peer.complete_calls[0]["tool_choice"] == "required"
        assert message["tool_calls"][0]["function"]["name"] == "get_weather" and finish == "tool_calls"
        assert resp.headers["x-potluck-served-by"] == "household"

    def test_a_streamed_peer_reply_is_shaped_the_same_way(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        peer = FakeHousehold(monkeypatch, "Checking.\n" + HERMES, supports_tools=True, size=3)
        resp = post(client, {"model": "peer-model", "messages": [USER], "tools": [WEATHER], "stream": True})
        _, content, calls, finish, _ = stream_text(resp)
        assert peer.stream_calls[0]["tools"] == [WEATHER]
        assert content == "Checking." and calls[0]["function"]["name"] == "get_weather" and finish == "tool_calls"

    @pytest.mark.parametrize("advertised", [None, False])
    def test_a_peer_that_does_not_advertise_tools_is_never_sent_them(self, client, monkeypatch, advertised):
        serve(monkeypatch, FakeRuntime())
        peer = FakeHousehold(monkeypatch, HERMES, supports_tools=advertised)
        for stream in (False, True):
            resp = post(client, {"model": "peer-model", "messages": [USER], "tools": [WEATHER], "stream": stream})
            assert resp.status_code == 400
            detail = resp.json()["detail"]
            assert detail["code"] == "tools_not_supported" and "household machine" in detail["message"]
        assert peer.stream_calls == [] and peer.complete_calls == []

    def test_tool_history_alone_also_needs_a_peer_that_advertises_tools(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        peer = FakeHousehold(monkeypatch, "ok", supports_tools=None)
        resp = post(client, {"model": "peer-model", "messages": [USER, {"role": "tool", "content": "{}"}]})
        assert resp.status_code == 400 and peer.complete_calls == []

    def test_a_request_without_tools_reaches_the_peer_exactly_as_before(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        peer = FakeHousehold(monkeypatch, "hello", supports_tools=None)
        assert post(client, {"model": "peer-model", "messages": [USER]}).status_code == 200
        assert peer.complete_calls == [{"messages": [USER]}]
        post(client, {"model": "peer-model", "messages": [USER], "stream": True})
        assert peer.stream_calls == [{"messages": [USER]}]

    def test_reasoning_in_a_peer_reply_is_split(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        FakeHousehold(monkeypatch, THINKING, size=4)
        message, _ = only_message(post(client, {"model": "peer-model", "messages": [USER]}))
        assert message["content"] == ANSWER and message["reasoning_content"].startswith("Okay")
        reasoning, content, _, _, _ = stream_text(post(client, {"model": "peer-model", "messages": [USER], "stream": True}))
        assert reasoning.startswith("Okay") and content == ANSWER

    def test_a_peer_that_opened_the_block_in_its_template_is_split_when_the_reply_is_whole(self, client, monkeypatch):
        """The requester cannot see the peer's template; a lone </think> in a whole reply is taken as the end of the block."""
        serve(monkeypatch, FakeRuntime(traits=TemplateTraits(opens_reasoning=False)))
        FakeHousehold(monkeypatch, "Working it out.\n</think>\n\n391")
        message, _ = only_message(post(client, {"model": "peer-model", "messages": [USER]}))
        assert message["reasoning_content"] == "Working it out." and message["content"] == "391"

    def test_opting_out_applies_to_peer_replies(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        FakeHousehold(monkeypatch, THINKING)
        message, _ = only_message(post(client, {"model": "peer-model", "messages": [USER], "reasoning_format": "none"}))
        assert message["content"] == THINKING


# ---------------------------------------------------------------------------
# Circle and pool
# ---------------------------------------------------------------------------


class FakePool:
    def __init__(self, monkeypatch, text, *, size=6):
        self.calls: list[tuple] = []
        self.text = text

        def stream_pool_openai(messages, model, temperature, max_tokens, scope="pool", circle_id=None):
            self.calls.append(("stream", scope, circle_id))

            async def gen():
                for c in peer_chunks(self.text, size):
                    yield c
            return gen()

        async def complete_pool_openai(messages, model, temperature, max_tokens, scope="pool", circle_id=None):
            self.calls.append(("complete", scope, circle_id))
            from potluck_local.services.remote_stream import collect_completion

            async def gen():
                for c in peer_chunks(self.text, size):
                    yield c
            return await collect_completion(gen())

        async def resolve_circle_id(selector):
            return selector or "circle-1"

        monkeypatch.setattr("potluck_local.services.pool_routing.stream_pool_openai", stream_pool_openai)
        monkeypatch.setattr("potluck_local.services.pool_routing.complete_pool_openai", complete_pool_openai)
        monkeypatch.setattr("potluck_local.services.pool_routing.resolve_circle_id", resolve_circle_id)
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: True)


class TestCircleAndPool:
    @pytest.mark.parametrize("stream", [False, True])
    def test_a_circle_request_with_tools_is_refused_and_nothing_is_dispatched(self, client, monkeypatch, stream):
        serve(monkeypatch, FakeRuntime())
        pool = FakePool(monkeypatch, HERMES)
        resp = post(client, {"model": "some-model", "messages": [USER], "tools": [WEATHER], "stream": stream},
                    **{"X-Potluck-Scope": "circle"})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["code"] == "tools_not_supported" and "circle requests" in detail["message"]
        assert pool.calls == []

    @pytest.mark.parametrize("stream", [False, True])
    def test_a_pool_request_with_tools_is_refused(self, client, monkeypatch, stream):
        serve(monkeypatch, FakeRuntime())
        pool = FakePool(monkeypatch, HERMES)
        resp = post(client, {"model": "pool-only-model", "messages": [USER], "tools": [WEATHER], "stream": stream})
        assert resp.status_code == 400 and "pool requests" in resp.json()["detail"]["message"]
        assert pool.calls == []

    def test_a_tool_message_in_a_circle_request_is_refused_too(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        pool = FakePool(monkeypatch, "x")
        resp = post(client, {"model": "m", "messages": [USER, {"role": "tool", "content": "{}"}]}, **{"X-Potluck-Scope": "circle"})
        assert resp.status_code == 400 and pool.calls == []

    def test_a_circle_reply_without_tools_has_its_reasoning_split(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        pool = FakePool(monkeypatch, THINKING)
        message, _ = only_message(post(client, {"model": "m", "messages": [USER]}, **{"X-Potluck-Scope": "circle"}))
        assert message["content"] == ANSWER and message["reasoning_content"].startswith("Okay")
        reasoning, content, _, _, _ = stream_text(post(client, {"model": "m", "messages": [USER], "stream": True}, **{"X-Potluck-Scope": "circle"}))
        assert reasoning.startswith("Okay") and content == ANSWER
        assert [c[:2] for c in pool.calls] == [("complete", "circle"), ("stream", "circle")]

    def test_a_pool_reply_has_its_reasoning_split_and_can_opt_out(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        FakePool(monkeypatch, THINKING)
        message, _ = only_message(post(client, {"model": "pool-only-model", "messages": [USER]}))
        assert message["content"] == ANSWER
        raw, _ = only_message(post(client, {"model": "pool-only-model", "messages": [USER], "reasoning_format": "none"}))
        assert raw["content"] == THINKING

    def test_a_request_with_tools_for_the_local_model_is_still_served_when_the_pool_is_on(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(HERMES))
        pool = FakePool(monkeypatch, "unused")
        message, finish = only_message(post(client, {"model": "local-model", "messages": [USER], "tools": [WEATHER]}))
        assert finish == "tool_calls" and pool.calls == []


# ---------------------------------------------------------------------------
# qwen3-1.7b-q4 with thinking off: the block is opened and never closed
# ---------------------------------------------------------------------------

import qwen3_recorded as rec  # noqa: E402

HYBRID = TemplateTraits(supports_tools=True, thinks=True)
UNCLOSED = rec.TOOLS_NO_THINK[0]  # '<think>\n\n<tool_call>\n{"name": "get_weather", ...}\n</tool_call>'


class TestAnUnclosedBlockInFrontOfACall:
    def test_the_call_is_returned_and_there_is_no_reasoning(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(UNCLOSED, traits=HYBRID))
        message, finish = only_message(post(client, {"messages": [USER], "tools": [WEATHER], "temperature": 0}))
        assert finish == "tool_calls" and message["content"] is None and "reasoning_content" not in message
        assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"city": "Paris"}

    @pytest.mark.parametrize("size", [1, 3, 7, 1000])
    def test_streamed_at_any_chunk_size(self, client, monkeypatch, size):
        serve(monkeypatch, FakeRuntime(UNCLOSED, pieces=pieces_of(UNCLOSED, size), traits=HYBRID))
        resp = post(client, {"messages": [USER], "tools": [WEATHER], "stream": True})
        reasoning, content, calls, finish, deltas = stream_text(resp)
        assert reasoning == "" and content == "" and finish == "tool_calls"
        assert [(c["index"], c["function"]["name"]) for c in calls] == [(0, "get_weather")]
        assert not any("<think" in (d.get("content") or "") or "tool_call" in (d.get("content") or "") for d in deltas)

    def test_a_raw_reply_has_the_text_before_the_call_untouched(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(UNCLOSED, traits=HYBRID))
        message, finish = only_message(post(client, {"messages": [USER], "tools": [WEATHER], "reasoning_format": "none"}))
        assert message["content"] == "<think>\n\n" and finish == "tool_calls" and len(message["tool_calls"]) == 1
        serve(monkeypatch, FakeRuntime(UNCLOSED, pieces=pieces_of(UNCLOSED, 4), traits=HYBRID))
        _, content, calls, finish, _ = stream_text(post(client, {"messages": [USER], "tools": [WEATHER], "stream": True,
                                                                  "reasoning_format": "none"}))
        assert content == "<think>\n\n" and len(calls) == 1 and finish == "tool_calls"

    def test_without_tools_the_old_rule_stands(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(UNCLOSED, traits=HYBRID))
        message, finish = only_message(post(client, {"messages": [USER]}))
        assert message["content"] == "" and message["reasoning_content"].startswith("<tool_call>") and finish == "stop"

    def test_a_household_peers_reply_is_handled_the_same_way(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        FakeHousehold(monkeypatch, UNCLOSED, supports_tools=True, size=5)
        message, finish = only_message(post(client, {"model": "peer-model", "messages": [USER], "tools": [WEATHER]}))
        assert finish == "tool_calls" and message["tool_calls"][0]["function"]["name"] == "get_weather" and message["content"] is None
        _, content, calls, finish, _ = stream_text(post(client, {"model": "peer-model", "messages": [USER], "tools": [WEATHER], "stream": True}))
        assert content == "" and len(calls) == 1 and finish == "tool_calls"

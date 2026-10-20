"""POST /v1/responses, through the real app and a fake runtime.

Same setup as test_anthropic_route.py, whose fake runtime, fixtures and peer and pool
stand-ins are reused here: TestClient with a loopback client address, the gateway key
off unless a test sets one, no real model.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from responses_support import assert_well_formed, items, names, parse_sse, sse_events
from test_anthropic_route import (  # noqa: F401 - the fixtures are used by name
    ANSWER, ERROR_ITEM, HERMES, OVERFLOW_ITEM, THINKING, TWO_CALLS, FakeRuntime, InferenceFailure, ModelIdentityError,
    RemoteStreamError, TemplateTraits, client, pieces_of, serve, settings, use_peer, use_pool,
)

_LOOPBACK = ("127.0.0.1", 50000)

WEATHER = {"type": "function", "name": "get_weather", "description": "Weather", "strict": False, "parameters": {
    "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}
TIME = {"type": "function", "name": "get_time", "parameters": {"type": "object"}}
PATCH = {"type": "custom", "name": "apply_patch", "description": "Edit files.", "format": {"type": "grammar", "syntax": "lark", "definition": "x"}}
USER = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "What's the weather in Paris right now? Use the tool."}]}
PROMPT_TOO_LONG = "This model's maximum context length is 4096 tokens. However, your input resulted in 5000 tokens."


def body(**fields):
    return {"model": "local-model", "input": "ping", **fields}


def post(client, payload, **headers):
    return client.post("/v1/responses", json=payload, headers=headers)


def sse(resp):
    assert resp.status_code == 200, resp.text
    return parse_sse(sse_events(resp.text))


def assert_error(resp, status, kind, message=None, code=None):
    assert resp.status_code == status, resp.text
    payload = resp.json()
    assert set(payload) == {"error"} and set(payload["error"]) == {"message", "type", "param", "code"}
    assert payload["error"]["type"] == kind and payload["error"]["param"] is None
    if message is not None:
        assert payload["error"]["message"] == message
    if code is not None:
        assert payload["error"]["code"] == code == resp.headers["x-potluck-error"]
    return payload["error"]["message"]


# ---------------------------------------------------------------------------
# A whole reply
# ---------------------------------------------------------------------------


class TestNonStreaming:
    def test_a_plain_response(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime("pong"))
        resp = post(client, body(instructions="be brief", metadata={"a": "b"}))
        assert resp.status_code == 200
        reply = resp.json()
        assert reply["id"].startswith("resp_") and reply["object"] == "response" and reply["status"] == "completed"
        assert reply["model"] == "local-model" and reply["metadata"] == {"a": "b"} and type(reply["created_at"]) is int
        (item,) = reply["output"]
        assert item["type"] == "message" and item["role"] == "assistant" and item["content"][0]["text"] == "pong"
        assert reply["usage"] == {"input_tokens": 7, "input_tokens_details": {"cached_tokens": 0},
                                  "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 12}
        assert resp.headers["x-potluck-served-by"] == "local" and "x-potluck-usage" not in resp.headers
        (call,) = rt.calls
        assert call["messages"] == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "ping"}]
        assert call["temperature"] == 0.7 and call["max_tokens"] == 2048 and "stop" not in call and "tools" not in call

    def test_without_a_model_the_loaded_one_answers_and_is_named(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        payload = body()
        del payload["model"]
        assert post(client, payload).json()["model"] == "local-model"

    def test_options_reach_the_model_and_the_rest_is_accepted(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime())
        resp = post(client, body(temperature=0.2, max_output_tokens=9, top_p=0.5, store=False, include=["reasoning.encrypted_content"],
                                 reasoning={"summary": "auto"}, prompt_cache_key="k", client_metadata={"a": "b"}, parallel_tool_calls=True))
        assert resp.status_code == 200
        assert rt.calls[0]["temperature"] == 0.2 and rt.calls[0]["max_tokens"] == 9 and "top_p" not in rt.calls[0]

    def test_reasoning_is_hidden(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(THINKING))
        reply = post(client, body(reasoning={"effort": "high", "summary": "auto"})).json()
        assert [c["text"] for i in reply["output"] for c in i["content"]] == [ANSWER]
        assert "Every node relays" not in json.dumps(reply)

    def test_a_reply_cut_off_by_length_is_incomplete(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("cut", finish="length"))
        reply = post(client, body()).json()
        assert reply["status"] == "incomplete" and reply["incomplete_details"] == {"reason": "max_output_tokens"}

    def test_usage_is_estimated_when_the_engine_gave_none(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("a" * 40, usage=None))
        resp = post(client, body())
        assert resp.headers["x-potluck-usage"] == "estimated" and resp.json()["usage"]["output_tokens"] == 10

    def test_the_conversation_is_converted(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime("ok"))
        raw = [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]},
               {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "one"}]},
               {"type": "reasoning", "id": "rs_1", "summary": []},
               {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "two"}]},
               {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "three"}]}]
        post(client, body(input=raw, instructions="top"))
        assert rt.calls[0]["messages"] == [
            {"role": "system", "content": "top\n\nrules"}, {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"}, {"role": "user", "content": "three"}]


class TestToolCalls:
    def test_a_call_is_a_function_call_item_with_the_engines_id(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime(HERMES))
        reply = post(client, body(input=[USER], tools=[WEATHER])).json()
        (item,) = reply["output"]
        assert item["type"] == "function_call" and item["name"] == "get_weather" and json.loads(item["arguments"]) == {"city": "Paris"}
        assert len(item["call_id"]) == 9 and item["call_id"].isalnum() and reply["status"] == "completed"
        assert rt.calls[0]["tools"] == [{"type": "function", "function": {
            "name": "get_weather", "description": "Weather", "parameters": WEATHER["parameters"]}}]
        assert rt.calls[0]["tool_choice"] == "auto"

    def test_the_call_id_comes_back_unchanged_in_the_next_request(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime(HERMES))
        first = post(client, body(input=[USER], tools=[WEATHER])).json()
        call = first["output"][0]
        rt.reply = "It is sunny in Paris."
        history = [USER, call, {"type": "function_call_output", "call_id": call["call_id"], "output": "sunny, 21C"}]
        second = post(client, body(input=history, tools=[WEATHER])).json()
        assert second["output"][0]["content"][0]["text"] == "It is sunny in Paris."
        sent = rt.calls[1]["messages"]
        assert sent[1]["tool_calls"][0]["id"] == call["call_id"] and sent[1]["tool_calls"][0]["function"]["arguments"] == {"city": "Paris"}
        assert sent[2] == {"role": "tool", "content": "sunny, 21C", "tool_call_id": call["call_id"]}

    def test_tool_choice_reaches_the_model(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime(HERMES))
        post(client, body(input=[USER], tools=[WEATHER], tool_choice={"type": "function", "name": "get_weather"}))
        assert rt.calls[0]["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}

    def test_tool_choice_none_offers_no_tools(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime("hello"))
        post(client, body(input=[USER], tools=[WEATHER], tool_choice="none"))
        assert "tools" not in rt.calls[0]

    def test_parallel_calls_are_kept_unless_turned_off(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(TWO_CALLS))
        both = post(client, body(input=[USER], tools=[WEATHER, TIME])).json()
        assert [i["name"] for i in both["output"]] == ["get_weather", "get_time"]
        one = post(client, body(input=[USER], tools=[WEATHER, TIME], parallel_tool_calls=False)).json()
        assert [i["name"] for i in one["output"]] == ["get_weather"]

    def test_hosted_tools_are_left_out_and_reported(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime("hi"))
        resp = post(client, body(tools=[WEATHER, {"type": "web_search", "external_web_access": False}, {"type": "web_search"}]))
        assert resp.status_code == 200 and resp.headers["x-potluck-ignored-tools"] == "web_search"
        assert [t["function"]["name"] for t in rt.calls[0]["tools"]] == ["get_weather"]

    def test_only_hosted_tools_does_not_need_tool_support(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("hi", traits=TemplateTraits()))
        assert post(client, body(tools=[{"type": "web_search"}])).status_code == 200

    def test_a_freeform_tool_call_round_trip(self, client, monkeypatch):
        patch = "*** Begin Patch\n*** Add File: a.txt\n+hi\n*** End Patch"
        call = '<tool_call>\n' + json.dumps({"name": "apply_patch", "arguments": {"input": patch}}) + '\n</tool_call>'
        rt = serve(monkeypatch, FakeRuntime(call))
        reply = post(client, body(input=[USER], tools=[PATCH])).json()
        (item,) = reply["output"]
        assert item["type"] == "custom_tool_call" and item["name"] == "apply_patch" and item["input"] == patch
        rt.reply = "done"
        history = [USER, item, {"type": "custom_tool_call_output", "call_id": item["call_id"], "output": "Success"}]
        post(client, body(input=history, tools=[PATCH]))
        sent = rt.calls[1]["messages"]
        assert sent[1]["tool_calls"][0]["function"] == {"name": "apply_patch", "arguments": {"input": patch}}
        assert sent[2]["role"] == "tool" and sent[2]["content"] == "Success"

    def test_a_namespaced_call_comes_back_in_its_namespace(self, client, monkeypatch):
        call = '<tool_call>\n{"name": "agents__spawn", "arguments": {"task": "x"}}\n</tool_call>'
        serve(monkeypatch, FakeRuntime(call))
        space = {"type": "namespace", "name": "agents", "description": "d", "tools": [
            {"type": "function", "name": "spawn", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}}}]}
        (item,) = post(client, body(input=[USER], tools=[space])).json()["output"]
        assert item["namespace"] == "agents" and item["name"] == "spawn" and json.loads(item["arguments"]) == {"task": "x"}

    def test_a_model_without_tool_support_is_a_clear_400(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(traits=TemplateTraits()))
        resp = post(client, body(input=[USER], tools=[WEATHER]))
        assert "does not support tool calls" in assert_error(resp, 400, "invalid_request_error", code="tools_not_supported")


# ---------------------------------------------------------------------------
# A streamed reply
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_a_plain_stream_has_the_documented_events_in_order(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("Hello there", pieces=["Hello", " there"]))
        resp = post(client, body(stream=True))
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["x-potluck-served-by"] == "local" and resp.headers["x-potluck-usage"] == "estimated"
        assert resp.headers["cache-control"] == "no-cache"
        events = sse(resp)
        assert_well_formed(events)
        assert names(events) == [
            "response.created", "response.in_progress", "response.output_item.added", "response.content_part.added",
            "response.output_text.delta", "response.output_text.delta", "response.output_text.done",
            "response.content_part.done", "response.output_item.done", "response.completed"]
        assert items(events)[0]["content"][0]["text"] == "Hello there"
        assert events[0][1]["response"]["model"] == "local-model"
        assert resp.text.startswith("event: response.created\ndata: {")

    def test_input_tokens_come_from_the_tokenizer_of_the_model_that_answers(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime("ok", tokens=4321))
        events = sse(post(client, body(stream=True, instructions="SYSTEM-TEXT")))
        assert events[-1][1]["response"]["usage"]["input_tokens"] == 4321
        assert "SYSTEM-TEXT" in rt.counted[0] and "ping" in rt.counted[0]

    @pytest.mark.parametrize("size", [1, 3, 7, 1000])
    def test_reasoning_then_text_at_any_chunk_size(self, client, monkeypatch, size):
        serve(monkeypatch, FakeRuntime(THINKING, pieces=pieces_of(THINKING, size)))
        events = sse(post(client, body(stream=True)))
        assert_well_formed(events)
        assert [i["content"][0]["text"] for i in items(events)] == [ANSWER]
        assert "Every node relays" not in json.dumps(events)

    def test_a_function_call(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(HERMES, pieces=pieces_of(HERMES, 6)))
        events = sse(post(client, body(stream=True, input=[USER], tools=[WEATHER])))
        assert_well_formed(events)
        (item,) = items(events)
        assert item["type"] == "function_call" and item["name"] == "get_weather" and json.loads(item["arguments"]) == {"city": "Paris"}
        assert len(item["call_id"]) == 9
        assert names(events).count("response.function_call_arguments.done") == 1
        assert events[-1][0] == "response.completed"

    def test_text_then_two_calls(self, client, monkeypatch):
        text = "Let me check both.\n" + TWO_CALLS
        serve(monkeypatch, FakeRuntime(text, pieces=pieces_of(text, 4)))
        events = sse(post(client, body(stream=True, input=[USER], tools=[WEATHER, TIME])))
        assert_well_formed(events)
        assert [(i["type"], i.get("name")) for i in items(events)] == [("message", None), ("function_call", "get_weather"), ("function_call", "get_time")]

    def test_only_the_first_call_streams_when_parallel_calls_are_off(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(TWO_CALLS, pieces=pieces_of(TWO_CALLS, 9)))
        events = sse(post(client, body(stream=True, input=[USER], tools=[WEATHER, TIME], parallel_tool_calls=False)))
        assert_well_formed(events)
        assert [i["name"] for i in items(events)] == ["get_weather"]

    def test_a_freeform_call(self, client, monkeypatch):
        patch = "*** Begin Patch\n*** End Patch"
        call = '<tool_call>\n' + json.dumps({"name": "apply_patch", "arguments": {"input": patch}}) + '\n</tool_call>'
        serve(monkeypatch, FakeRuntime(call, pieces=pieces_of(call, 5)))
        events = sse(post(client, body(stream=True, input=[USER], tools=[PATCH])))
        assert_well_formed(events)
        assert items(events)[0]["type"] == "custom_tool_call" and items(events)[0]["input"] == patch
        assert names(events).count("response.custom_tool_call_input.done") == 1

    def test_length_is_incomplete(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime("cut", finish="length"))
        events = sse(post(client, body(stream=True)))
        assert events[-1][0] == "response.incomplete" and events[-1][1]["response"]["incomplete_details"] == {"reason": "max_output_tokens"}


class TestStreamFailures:
    def test_an_engine_error_before_the_first_event_is_an_http_error(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(items=[ERROR_ITEM]))
        resp = post(client, body(stream=True))
        assert_error(resp, 500, "server_error", "the engine broke", "inference_error")
        assert resp.headers["content-type"].startswith("application/json")

    def test_gpu_out_of_memory_is_a_503(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(items=[{"__error__": "The GPU stopped this reply.", "__category__": "gpu_out_of_memory"}]))
        assert_error(post(client, body(stream=True)), 503, "server_error")

    def test_a_model_that_changed_under_the_request_is_a_409(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(stream_error=ModelIdentityError("Requested model is no longer loaded.")))
        assert_error(post(client, body(stream=True)), 409, "invalid_request_error", "Requested model is no longer loaded.")

    def test_an_error_after_the_first_event_is_response_failed(self, client, monkeypatch):
        first = {"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]}
        serve(monkeypatch, FakeRuntime(items=[first, ERROR_ITEM]))
        resp = post(client, body(stream=True))
        assert resp.status_code == 200
        events = parse_sse(sse_events(resp.text))
        assert names(events)[-1] == "response.failed" and "response.completed" not in names(events)
        response = events[-1][1]["response"]
        assert response["status"] == "failed" and response["error"] == {"code": "inference_error", "message": "the engine broke"}

    def test_a_quiet_engine_gets_the_stream_started_and_keepalives(self, client, monkeypatch):
        from potluck_local.routers import responses

        monkeypatch.setattr(responses, "COMMIT_WINDOW_S", 0.02)
        monkeypatch.setattr(responses, "KEEPALIVE_INTERVAL_S", 0.04)
        serve(monkeypatch, FakeRuntime("late", first_delay=0.3))
        resp = post(client, body(stream=True))
        assert resp.text.count(": keep-alive\n\n") >= 3
        events = sse(resp)
        assert_well_formed(events)
        assert items(events)[0]["content"][0]["text"] == "late"

    def test_a_failure_after_a_quiet_start_is_response_failed_with_the_context_code(self, client, monkeypatch):
        from potluck_local.routers import responses

        monkeypatch.setattr(responses, "COMMIT_WINDOW_S", 0.02)
        serve(monkeypatch, FakeRuntime(items=[OVERFLOW_ITEM], first_delay=0.2))
        resp = post(client, body(stream=True))
        assert resp.status_code == 200
        events = parse_sse(sse_events(resp.text))
        assert names(events) == ["response.created", "response.in_progress", "response.failed"]
        assert events[-1][1]["response"]["error"] == {"code": "context_length_exceeded", "message": PROMPT_TOO_LONG}


class TestPromptTooLong:
    def test_streamed(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(items=[OVERFLOW_ITEM]))
        assert_error(post(client, body(stream=True)), 400, "invalid_request_error", PROMPT_TOO_LONG, "context_length_exceeded")

    def test_not_streamed(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(chat_error=ValueError("Requested tokens (5000) exceed context window of 4096")))
        assert_error(post(client, body()), 400, "invalid_request_error", PROMPT_TOO_LONG, "context_length_exceeded")

    def test_other_engine_errors_are_not_mistaken_for_it(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(chat_error=ValueError("something else")))
        assert_error(post(client, body()), 500, "server_error", "inference failed: ValueError")


# ---------------------------------------------------------------------------
# Routing: the scope headers, a household peer, a circle, the pool
# ---------------------------------------------------------------------------


class TestRouting:
    def test_a_household_peer_answers(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        seen = use_peer(monkeypatch)
        resp = post(client, body(model="remote-model"))
        reply = resp.json()
        assert reply["model"] == "remote-model" and reply["output"][0]["content"][0]["text"] == "hello from the peer"
        assert resp.headers["x-potluck-served-by"] == "household" and resp.headers["x-potluck-usage"] == "estimated"
        assert seen[0]["model"] == "remote-model"

    def test_a_household_peer_streams(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        use_peer(monkeypatch)
        resp = post(client, body(model="remote-model", stream=True))
        events = sse(resp)
        assert_well_formed(events)
        assert resp.headers["x-potluck-served-by"] == "household" and items(events)[0]["content"][0]["text"] == "hello from the peer"
        assert events[-1][1]["response"]["model"] == "remote-model" and events[-1][1]["response"]["usage"]["output_tokens"] == 5

    def test_tools_go_to_a_peer_that_supports_them_and_not_to_one_that_does_not(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        seen = use_peer(monkeypatch, HERMES)
        reply = post(client, body(model="remote-model", input=[USER], tools=[WEATHER])).json()
        assert reply["output"][0]["type"] == "function_call" and seen[0]["tools"][0]["function"]["name"] == "get_weather"
        use_peer(monkeypatch, HERMES, supports_tools=False)
        assert "household machine" in assert_error(post(client, body(model="remote-model", input=[USER], tools=[WEATHER])), 400, "invalid_request_error")

    def test_scope_local_refuses_another_machines_model(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        use_peer(monkeypatch)
        assert_error(post(client, body(model="remote-model"), **{"X-Potluck-Scope": "local"}), 409, "invalid_request_error",
                     "Requested model is not loaded locally. Load that model or use mine scope.")

    def test_an_unknown_model_is_a_404(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        assert_error(post(client, body(model="nobody-has-this")), 404, "invalid_request_error")

    def test_the_open_pool_when_opted_in(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: True)
        seen = use_pool(monkeypatch)
        resp = post(client, body(model="pool-model"))
        assert resp.headers["x-potluck-served-by"] == "pool" and resp.json()["output"][0]["content"][0]["text"] == "hello from the pool"
        assert seen[0]["scope"] == "pool"

    def test_a_trusted_circle_by_header(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        seen = use_pool(monkeypatch)
        resp = post(client, body(model="circle-model", stream=True), **{"X-Potluck-Scope": "circle", "X-Potluck-Circle": "friends"})
        assert resp.headers["x-potluck-served-by"] == "circle" and sse(resp)
        assert seen[0]["scope"] == "circle" and seen[0]["circle_id"] == "11111111-1111-1111-1111-111111111111"

    def test_a_gpu_failure_on_this_machine_is_a_503(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime(chat_error=InferenceFailure("gpu_out_of_memory", "The GPU stopped this reply.")))
        assert_error(post(client, body()), 503, "server_error", "The GPU stopped this reply.", "gpu_out_of_memory")


# ---------------------------------------------------------------------------
# Errors in OpenAI's shape
# ---------------------------------------------------------------------------


class TestErrorShape:
    def test_bad_json(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        resp = client.post("/v1/responses", content=b"{not json", headers={"content-type": "application/json"})
        assert_error(resp, 400, "invalid_request_error", "The request body is not valid JSON.", "invalid_json")

    @pytest.mark.parametrize("payload", [[1, 2], "text", 5, "null"])
    def test_a_body_of_the_wrong_shape(self, client, monkeypatch, payload):
        serve(monkeypatch, FakeRuntime())
        assert_error(post(client, payload), 400, "invalid_request_error", "The request body must be a JSON object.")

    def test_previous_response_id_is_a_clear_400(self, client, monkeypatch):
        rt = serve(monkeypatch, FakeRuntime())
        message = assert_error(post(client, body(previous_response_id="resp_1")), 400, "invalid_request_error",
                               code="previous_response_id_not_supported")
        assert "no response store" in message and "full conversation" in message
        assert rt.calls == []

    @pytest.mark.parametrize("payload, code", [
        (body(input=[]), "invalid_input"),
        ({"model": "local-model"}, "invalid_input"),
        (body(max_output_tokens=0), "invalid_max_output_tokens"),
        (body(stream="yes"), "invalid_stream"),
        (body(input=[{"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "x"}]}]), "unsupported_content_part"),
        (body(input=[{"type": "item_reference", "id": "msg_1"}]), "item_reference_not_supported"),
        (body(input=[{"type": "mystery"}]), "unsupported_input_item"),
        (body(tools=[{"type": "namespace", "name": "n", "tools": [{"type": "custom", "name": "p"}]}]), "invalid_tools"),
        (body(text={"format": {"type": "json_object"}}), "unsupported_output_format"),
        (body(tools=[WEATHER], tool_choice={"type": "function", "name": "other"}), "invalid_tool_choice"),
        (body(tools=[WEATHER], tool_choice={"type": "web_search"}), "invalid_tool_choice"),
    ])
    def test_refusals(self, client, monkeypatch, payload, code):
        rt = serve(monkeypatch, FakeRuntime())
        assert_error(post(client, payload), 400, "invalid_request_error", code=code)
        assert rt.calls == []

    def test_an_image_is_named_by_type(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        message = assert_error(post(client, body(input=[{"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "x"}]}])),
                               400, "invalid_request_error")
        assert "input_image" in message and "text only" in message

    def test_a_body_that_is_too_large(self, client, monkeypatch):
        from potluck_local.services import responses_api

        serve(monkeypatch, FakeRuntime())
        monkeypatch.setattr(responses_api, "MAX_BODY_BYTES", 200)
        assert_error(post(client, body(instructions="x" * 500)), 413, "invalid_request_error", "The request body is too large.")

    def test_an_unexpected_failure_leaks_nothing(self, client, monkeypatch):
        async def boom(*a, **kw):
            raise RuntimeError("secret internal detail")

        serve(monkeypatch, FakeRuntime())
        monkeypatch.setattr("potluck_local.routers.gateway.plan_chat", boom)
        assert "secret" not in assert_error(post(client, body()), 500, "server_error")

    def test_the_other_routes_keep_their_error_shape(self, client, monkeypatch):
        serve(monkeypatch, FakeRuntime())
        resp = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}], "model": "nobody-has-this"})
        assert resp.status_code == 404 and set(resp.json()) == {"detail"}


class TestAuth:
    @pytest.fixture()
    def keyed(self, monkeypatch, settings):
        monkeypatch.setenv("POTLUCK_GATEWAY_API_KEY", "gw-secret")
        from potluck_local.app import create_app

        serve(monkeypatch, FakeRuntime())
        return TestClient(create_app(), client=_LOOPBACK, raise_server_exceptions=False)

    def test_bearer_key(self, keyed):
        assert post(keyed, body(), authorization="Bearer gw-secret").status_code == 200

    @pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer wrong"}, {"authorization": "Bearer "}, {"authorization": "gw-secret"}])
    def test_a_missing_or_wrong_key_is_a_401_in_openai_shape(self, keyed, headers):
        assert_error(post(keyed, body(), **headers), 401, "authentication_error", "missing or invalid gateway API key")

    def test_the_gateway_switched_off_is_a_404_in_openai_shape(self, monkeypatch, settings):
        monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", "sidecar-secret")
        from potluck_local.app import create_app

        off = TestClient(create_app(), client=_LOOPBACK, raise_server_exceptions=False)
        assert_error(post(off, body()), 404, "invalid_request_error", "gateway disabled; enable it in Settings -> Connect tools")

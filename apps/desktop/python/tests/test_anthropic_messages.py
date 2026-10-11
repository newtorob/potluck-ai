"""The Anthropic Messages translation on its own: request in, reply and events out.

No HTTP and no engine. test_anthropic_route.py runs the same code through the real
route against a fake runtime.
"""
from __future__ import annotations

import asyncio
import itertools
import json

import pytest

from anthropic_support import assert_well_formed, blocks, names, parse_sse
from potluck_local.services import anthropic_messages as am
from potluck_local.services.gateway_request import RequestError

BASE = {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
WEATHER = {"name": "get_weather", "description": "Weather", "input_schema": {
    "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}
TIME = {"name": "get_time", "input_schema": {"type": "object"}}


def parse(**fields):
    body = {**BASE, **fields}
    return am.parse_request(body, default_max_tokens=2048, default_temperature=0.7)


def messages_of(**fields):
    return parse(**fields).prepared.messages


def refused(**fields):
    with pytest.raises(RequestError) as info:
        parse(**fields)
    return info.value


# ---------------------------------------------------------------------------
# Request: system and messages
# ---------------------------------------------------------------------------


class TestSystem:
    def test_a_string(self):
        assert messages_of(system="be brief")[0] == {"role": "system", "content": "be brief"}

    def test_text_blocks_are_joined_with_a_blank_line_and_cache_control_is_ignored(self):
        system = [{"type": "text", "text": "one"}, {"type": "text", "text": "two", "cache_control": {"type": "ephemeral"}}]
        assert messages_of(system=system)[0] == {"role": "system", "content": "one\n\ntwo"}

    def test_role_system_entries_join_the_system_text_in_order(self):
        msgs = [{"role": "user", "content": "a"},
                {"role": "system", "content": [{"type": "text", "text": "late rule", "cache_control": {"type": "ephemeral"}}]},
                {"role": "assistant", "content": "b"},
                {"role": "system", "content": "later rule"},
                {"role": "user", "content": "c"}]
        out = messages_of(system="first", messages=msgs)
        assert out == [{"role": "system", "content": "first\n\nlate rule\n\nlater rule"},
                       {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                       {"role": "user", "content": "c"}]

    def test_no_system_text_means_no_system_message(self):
        assert [m["role"] for m in messages_of(system=[], messages=[{"role": "system", "content": ""}, *BASE["messages"]])] == ["user"]

    @pytest.mark.parametrize("system", [5, [{"type": "image"}], [{"type": "text"}], ["plain"]])
    def test_other_shapes_are_refused(self, system):
        assert refused(system=system).code == "invalid_system"


class TestUserMessages:
    def test_a_string_and_text_blocks(self):
        out = messages_of(messages=[{"role": "user", "content": "one"},
                                    {"role": "user", "content": [{"type": "text", "text": "two", "cache_control": {"type": "ephemeral"}},
                                                                  {"type": "text", "text": "three"}]}])
        assert out == [{"role": "user", "content": "one"}, {"role": "user", "content": "two\n\nthree"}]

    def test_tool_results_come_first_then_one_user_message_with_the_text(self):
        content = [{"type": "text", "text": "before"},
                   {"type": "tool_result", "tool_use_id": "call1", "content": "sunny"},
                   {"type": "text", "text": "after"},
                   {"type": "tool_result", "tool_use_id": "call2", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]
        assert messages_of(messages=[{"role": "user", "content": content}]) == [
            {"role": "tool", "content": "sunny", "tool_call_id": "call1"},
            {"role": "tool", "content": "a\n\nb", "tool_call_id": "call2"},
            {"role": "user", "content": "before\n\nafter"},
        ]

    def test_tool_results_alone_make_no_user_message(self):
        out = messages_of(messages=[{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c", "content": "x"}]}])
        assert out == [{"role": "tool", "content": "x", "tool_call_id": "c"}]

    def test_an_image_in_a_tool_result_becomes_a_line_of_text(self):
        content = [{"type": "tool_result", "tool_use_id": "c", "content": [
            {"type": "text", "text": "shot:"}, {"type": "image", "source": {"type": "base64", "data": "AAAA"}}]}]
        (tool,) = messages_of(messages=[{"role": "user", "content": content}])
        assert tool["content"] == "shot:\n\n[image left out: this model reads text only]"
        assert "AAAA" not in json.dumps(tool)

    @pytest.mark.parametrize("result, is_error, want", [
        ("", True, "Error"), (None, True, "Error"), ([], True, "Error"), ("boom", True, "boom"),
        ("", False, ""), (None, None, ""),
    ])
    def test_empty_and_failed_results(self, result, is_error, want):
        block = {"type": "tool_result", "tool_use_id": "c"}
        if result is not None:
            block["content"] = result
        if is_error is not None:
            block["is_error"] = is_error
        (tool,) = messages_of(messages=[{"role": "user", "content": [block]}])
        assert tool["content"] == want

    @pytest.mark.parametrize("kind", ["image", "document"])
    def test_a_top_level_image_or_document_is_refused(self, kind):
        err = refused(messages=[{"role": "user", "content": [{"type": "text", "text": "x"}, {"type": kind, "source": {}}]}])
        assert err.code == "unsupported_content_block" and kind in err.message

    def test_an_unknown_block_is_refused_by_name_and_a_strange_name_is_not_repeated(self):
        err = refused(messages=[{"role": "user", "content": [{"type": "search_result"}]}])
        assert "search_result" in err.message
        err = refused(messages=[{"role": "user", "content": [{"type": "<b>x</b> secret text"}]}])
        assert "secret" not in err.message and "unknown" in err.message

    def test_a_tool_result_needs_an_id_and_readable_content(self):
        assert refused(messages=[{"role": "user", "content": [{"type": "tool_result", "content": "x"}]}]).code == "invalid_tool_result"
        assert refused(messages=[{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c", "content": [{"type": "document"}]}]}]).code == "unsupported_content_block"
        assert refused(messages=[{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c", "content": 5}]}]).code == "invalid_tool_result"

    @pytest.mark.parametrize("entry", [{"role": "user"}, {"role": "user", "content": 5}, {"role": "tool", "content": "x"}, {"content": "x"}, "text", None])
    def test_malformed_entries_are_refused(self, entry):
        assert refused(messages=[entry]).code in ("invalid_content", "invalid_role")

    @pytest.mark.parametrize("messages", [None, [], "hi", {"role": "user"}])
    def test_messages_must_be_a_list_with_something_in_it(self, messages):
        assert refused(messages=messages).code == "invalid_messages"


class TestAssistantMessages:
    def test_text_and_tool_use_become_content_and_a_call_with_the_same_id(self):
        content = [{"type": "thinking", "thinking": "hmm", "signature": "sig"},
                   {"type": "redacted_thinking", "data": "xx"},
                   {"type": "text", "text": "Looking."},
                   {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}, "cache_control": {"type": "ephemeral"}}]
        out = messages_of(messages=[{"role": "user", "content": "q"}, {"role": "assistant", "content": content}])
        assert out[1] == {"role": "assistant", "content": "Looking.", "tool_calls": [
            {"id": "toolu_1", "type": "function", "function": {"name": "get_weather", "arguments": {"city": "Paris"}}}]}

    def test_two_calls_keep_their_order(self):
        content = [{"type": "tool_use", "id": "a", "name": "get_weather", "input": {}}, {"type": "tool_use", "id": "b", "name": "get_time", "input": None}]
        (_, msg) = messages_of(messages=[{"role": "user", "content": "q"}, {"role": "assistant", "content": content}])
        assert [c["id"] for c in msg["tool_calls"]] == ["a", "b"] and msg["content"] == ""
        assert msg["tool_calls"][1]["function"]["arguments"] == {}

    def test_a_turn_with_only_thinking_is_dropped(self):
        out = messages_of(messages=[{"role": "user", "content": "q"}, {"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": ""}]},
                                    {"role": "user", "content": "r"}])
        assert [m["role"] for m in out] == ["user", "user"]

    def test_a_string(self):
        assert messages_of(messages=[{"role": "assistant", "content": "hello"}]) == [{"role": "assistant", "content": "hello"}]

    @pytest.mark.parametrize("block", [
        {"type": "server_tool_use", "id": "s", "name": "web_search", "input": {}},
        {"type": "image"}, {"type": "tool_use", "id": "x", "name": "t", "input": [1]}, {"type": "tool_use", "name": "t", "input": {}},
        {"type": "tool_use", "id": "x", "input": {}},
    ])
    def test_blocks_the_engine_cannot_use_are_refused(self, block):
        assert refused(messages=[{"role": "assistant", "content": [block]}]).code in ("unsupported_content_block", "invalid_tool_use")


# ---------------------------------------------------------------------------
# Request: tools and options
# ---------------------------------------------------------------------------


class TestTools:
    def test_input_schema_tools_become_function_tools(self):
        req = parse(tools=[{**WEATHER, "cache_control": {"type": "ephemeral"}}, TIME])
        assert req.prepared.tools == [
            {"type": "function", "function": {"name": "get_weather", "description": "Weather", "parameters": WEATHER["input_schema"]}},
            {"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"}}},
        ]
        assert req.prepared.tool_choice == "auto"

    @pytest.mark.parametrize("tool, kind", [
        ({"type": "web_search_20250305", "name": "web_search"}, "web_search_20250305"),
        ({"type": "bash_20250124", "name": "bash"}, "bash_20250124"),
        ({"type": "code_execution_20250522", "name": "code_execution"}, "code_execution_20250522"),
        ({"name": "no_schema"}, "custom"),
    ])
    def test_a_tool_without_an_input_schema_is_refused_and_its_type_is_named(self, tool, kind):
        err = refused(tools=[WEATHER, tool])
        assert err.code == "unsupported_tool" and f"'{kind}'" in err.message

    def test_the_existing_tool_checks_apply(self):
        assert refused(tools=[{"name": "bad name!", "input_schema": {}}]).code == "invalid_tools"
        assert refused(tools=[WEATHER, WEATHER]).code == "invalid_tools"
        assert refused(tools="x").code == "invalid_tools"
        assert refused(tools=[5]).code == "invalid_tools"

    @pytest.mark.parametrize("choice, want, first_only", [
        ({"type": "auto"}, "auto", False),
        ({"type": "auto", "disable_parallel_tool_use": True}, "auto", True),
        ({"type": "any"}, "required", False),
        ({"type": "any", "disable_parallel_tool_use": True}, "required", True),
        ({"type": "tool", "name": "get_weather"}, {"type": "function", "function": {"name": "get_weather"}}, False),
        ({"type": "tool", "name": "get_weather", "disable_parallel_tool_use": True}, {"type": "function", "function": {"name": "get_weather"}}, True),
    ])
    def test_tool_choice(self, choice, want, first_only):
        req = parse(tools=[WEATHER], tool_choice=choice)
        assert req.prepared.tool_choice == want and req.first_call_only is first_only

    def test_tool_choice_none_shows_the_model_no_tools(self):
        req = parse(tools=[WEATHER], tool_choice={"type": "none"})
        assert req.prepared.tools is None and req.prepared.tool_choice is None

    @pytest.mark.parametrize("choice", ["auto", {"type": "sometimes"}, {"type": "tool"}, {"type": "tool", "name": "nope"}, {}])
    def test_bad_tool_choice_is_refused(self, choice):
        assert refused(tools=[WEATHER], tool_choice=choice).code == "invalid_tool_choice"

    def test_any_needs_tools(self):
        assert refused(tool_choice={"type": "any"}).code == "invalid_tool_choice"


class TestOptions:
    def test_defaults(self):
        req = am.parse_request({"messages": BASE["messages"]}, default_max_tokens=2048, default_temperature=0.7)
        assert (req.model, req.max_tokens, req.temperature, req.stop, req.stream) == (None, 2048, 0.7, [], False)
        assert req.show_thinking is False and req.first_call_only is False

    def test_given_values(self):
        req = parse(max_tokens=7, temperature=0, stop_sequences=["END", ""], stream=True, top_p=0.5, top_k=3)
        assert (req.model, req.max_tokens, req.temperature, req.stop, req.stream) == ("m", 7, 0.0, ["END"], True)

    @pytest.mark.parametrize("fields, code", [
        ({"max_tokens": 0}, "invalid_max_tokens"), ({"max_tokens": "5"}, "invalid_max_tokens"), ({"max_tokens": True}, "invalid_max_tokens"),
        ({"max_tokens": 1.5}, "invalid_max_tokens"),
        ({"temperature": "hot"}, "invalid_temperature"), ({"temperature": 3}, "invalid_temperature"), ({"temperature": -1}, "invalid_temperature"),
        ({"temperature": True}, "invalid_temperature"),
        ({"stop_sequences": "END"}, "invalid_stop_sequences"), ({"stop_sequences": [1]}, "invalid_stop_sequences"),
        ({"stream": "yes"}, "invalid_stream"), ({"model": 5}, "invalid_model"),
    ])
    def test_bad_values(self, fields, code):
        assert refused(**fields).code == code

    @pytest.mark.parametrize("thinking, shown", [
        ({"type": "enabled", "budget_tokens": 1024}, True),
        ({"type": "enabled", "budget_tokens": 1024, "display": "summarized"}, True),
        ({"type": "adaptive"}, True),
        ({"type": "adaptive", "display": "omitted"}, False),
        ({"type": "enabled", "budget_tokens": 1024, "display": "omitted"}, False),
        ({"type": "disabled"}, False), (None, False), ("enabled", False), ({"type": "future_mode"}, False),
    ])
    def test_thinking_is_shown_only_when_asked_for(self, thinking, shown):
        assert parse(thinking=thinking).show_thinking is shown

    def test_fields_that_are_accepted_and_ignored(self):
        req = parse(metadata={"user_id": "u"}, context_management={"edits": []}, safeguards=[{"type": "x"}], container="c",
                    mcp_servers=[{"name": "s"}], service_tier="auto", output_config={"effort": "high"}, something_new=1)
        assert req.prepared.messages == [{"role": "user", "content": "hi"}]

    @pytest.mark.parametrize("fields", [
        {"output_config": {"format": {"type": "json_schema", "schema": {}}}},
        {"output_config": {"effort": "high", "format": {"type": "json_schema"}}},
        {"output_format": {"type": "json_schema", "schema": {}}},
    ])
    def test_structured_output_is_refused(self, fields):
        assert refused(**fields).code == "unsupported_output_format"

    @pytest.mark.parametrize("body", [None, [], "x", 5])
    def test_the_body_must_be_an_object(self, body):
        with pytest.raises(RequestError):
            am.parse_request(body, default_max_tokens=1, default_temperature=0.7)

    def test_prompt_text_covers_system_messages_calls_and_tools(self):
        req = parse(system="SYS", tools=[WEATHER], messages=[
            {"role": "user", "content": "USER"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "get_weather", "input": {"city": "Paris"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "RESULT"}]}])
        text = am.prompt_text(req)
        for part in ("SYS", "USER", "get_weather", '{"city": "Paris"}', "RESULT", '"parameters"'):
            assert part in text


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestErrors:
    @pytest.mark.parametrize("status, kind", [
        (400, "invalid_request_error"), (409, "invalid_request_error"), (422, "invalid_request_error"),
        (401, "authentication_error"), (403, "permission_error"), (404, "not_found_error"), (413, "request_too_large"),
        (429, "rate_limit_error"), (503, "overloaded_error"), (529, "overloaded_error"),
        (500, "api_error"), (502, "api_error"), (418, "api_error"),
    ])
    def test_kinds(self, status, kind):
        assert am.error_body(status, "msg") == {"type": "error", "error": {"type": kind, "message": "msg"}}

    def test_detail_forms(self):
        assert am.error_detail("text") == ("text", None)
        assert am.error_detail({"code": "c", "message": "m"}) == ("m", "c")
        assert am.error_detail({"message": "m"}) == ("m", None)
        assert am.error_detail(["odd"]) == ("The request failed.", None)

    def test_prompt_too_long_uses_the_largest_prompt_the_engine_takes(self):
        assert am.prompt_too_long(5000, 4096) == "prompt is too long: 5000 tokens > 4095 maximum"
        assert am.prompt_too_long(4096, 4096) == "prompt is too long: 4096 tokens > 4095 maximum"


# ---------------------------------------------------------------------------
# Whole replies
# ---------------------------------------------------------------------------

OPTIONS = am.ReplyOptions(model="claude-x", input_estimate=11)


def completion(content="Hi.", *, reasoning=None, calls=None, finish="stop", usage=None):
    message = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    if calls:
        message["tool_calls"] = [{"id": c[0], "type": "function", "function": {"name": c[1], "arguments": json.dumps(c[2])}} for c in calls]
    result = {"id": "x", "object": "chat.completion", "model": "local", "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    if usage is not None:
        result["usage"] = usage
    return result


COUNTED = {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27}


class TestReply:
    def test_a_plain_reply(self):
        reply, estimated = am.message_from_completion(completion(usage=COUNTED), OPTIONS)
        assert estimated is False
        assert reply.pop("id").startswith("msg_") and len(reply.pop("id", "msg_" + "x" * 24)) == 28
        assert reply == {"type": "message", "role": "assistant", "model": "claude-x", "content": [{"type": "text", "text": "Hi."}],
                         "stop_reason": "end_turn", "stop_sequence": None,
                         "usage": {"input_tokens": 20, "output_tokens": 7, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}

    def test_the_id_is_msg_and_24_characters_and_new_each_time(self):
        ids = {am.message_from_completion(completion(), OPTIONS)[0]["id"] for _ in range(5)}
        assert len(ids) == 5 and all(i.startswith("msg_") and len(i) == 28 for i in ids)

    def test_block_order_is_thinking_text_then_calls(self):
        shown = am.ReplyOptions("m", show_thinking=True)
        reply, _ = am.message_from_completion(
            completion("Let me check.", reasoning="Need the tool.", calls=[("call1", "get_weather", {"city": "Paris"}), ("call2", "get_time", {})],
                       usage=COUNTED), shown)
        assert reply["content"] == [
            {"type": "thinking", "thinking": "Need the tool.", "signature": ""},
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "call1", "name": "get_weather", "input": {"city": "Paris"}},
            {"type": "tool_use", "id": "call2", "name": "get_time", "input": {}},
        ]
        assert reply["stop_reason"] == "tool_use"

    def test_reasoning_is_left_out_unless_shown(self):
        reply, _ = am.message_from_completion(completion("A.", reasoning="secret thoughts"), am.ReplyOptions("m"))
        assert reply["content"] == [{"type": "text", "text": "A."}]
        assert "secret thoughts" not in json.dumps(reply)

    def test_empty_text_is_left_out(self):
        reply, _ = am.message_from_completion(completion(None, calls=[("c1", "get_time", {})]), OPTIONS)
        assert [b["type"] for b in reply["content"]] == ["tool_use"]
        reply, _ = am.message_from_completion(completion(""), OPTIONS)
        assert reply["content"] == []

    def test_only_the_first_call_is_kept_when_parallel_calls_are_off(self):
        reply, _ = am.message_from_completion(
            completion(None, calls=[("c1", "get_weather", {"city": "A"}), ("c2", "get_time", {})]), am.ReplyOptions("m", first_call_only=True))
        assert [b["id"] for b in reply["content"]] == ["c1"]

    @pytest.mark.parametrize("finish, calls, want", [
        ("stop", [], "end_turn"), ("length", [], "max_tokens"), (None, [], "end_turn"), ("content_filter", [], "end_turn"),
        ("tool_calls", [("c", "get_time", {})], "tool_use"), ("stop", [("c", "get_time", {})], "tool_use"), ("length", [("c", "get_time", {})], "tool_use"),
    ])
    def test_stop_reason(self, finish, calls, want):
        reply, _ = am.message_from_completion(completion("x", calls=calls, finish=finish), OPTIONS)
        assert reply["stop_reason"] == want and reply["stop_sequence"] is None

    def test_estimated_usage_when_the_engine_gave_none(self):
        none = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
        for usage in (none, None, {}):
            reply, estimated = am.message_from_completion(completion("a" * 41, usage=usage), OPTIONS)
            assert estimated is True
            assert reply["usage"]["input_tokens"] == 11 and reply["usage"]["output_tokens"] == 11  # ceil(41 / 4)

    def test_one_missing_figure_is_estimated_and_the_other_kept(self):
        reply, estimated = am.message_from_completion(completion("abcd", usage={"prompt_tokens": 30, "completion_tokens": None}), OPTIONS)
        assert estimated is True and reply["usage"]["input_tokens"] == 30 and reply["usage"]["output_tokens"] == 1

    @pytest.mark.parametrize("bad", [True, -1, 1.5, "7", None])
    def test_figures_that_are_not_counts_are_not_used(self, bad):
        reply, estimated = am.message_from_completion(completion("abcd", usage={"prompt_tokens": bad, "completion_tokens": bad}), OPTIONS)
        assert estimated is True and reply["usage"]["input_tokens"] == 11

    def test_usage_numbers_are_always_integers(self):
        reply, _ = am.message_from_completion(completion("abc", reasoning="r", usage={"prompt_tokens": 0, "completion_tokens": 0}), OPTIONS)
        assert all(type(v) is int for v in reply["usage"].values())

    def test_estimate_covers_hidden_reasoning_and_call_arguments(self):
        reply, _ = am.message_from_completion(completion("", reasoning="r" * 40, calls=[("c", "get_time", {"a": "b" * 20})]), OPTIONS)
        assert reply["usage"]["output_tokens"] > 10

    @pytest.mark.parametrize("result", [None, 5, {}, {"choices": []}, {"choices": [None]}, {"choices": [{"message": None}]}])
    def test_odd_results_give_an_empty_message_and_nothing_raises(self, result):
        reply, estimated = am.message_from_completion(result, OPTIONS)
        assert reply["content"] == [] and reply["stop_reason"] == "end_turn" and estimated is True


# ---------------------------------------------------------------------------
# Streamed replies
# ---------------------------------------------------------------------------


def text(t):
    return {"choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}]}


def reasoning(t):
    return {"choices": [{"index": 0, "delta": {"reasoning_content": t}, "finish_reason": None}]}


def call(index, call_id, name, arguments):
    return {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": index, "id": call_id, "type": "function",
                                                                "function": {"name": name, "arguments": arguments}}]}, "finish_reason": None}]}


def finish(reason="stop", usage=None):
    chunk = {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}
    if usage:
        chunk["usage"] = usage
    return chunk


ROLE = {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
ERROR = {"__error__": "the engine broke", "__category__": "inference_error"}


def describe(failure):
    if isinstance(failure, dict):
        return am.Failure(500, failure["__error__"], failure.get("__category__"))
    return am.Failure(502, f"{type(failure).__name__} happened", None)


class Sleep:
    """In a source's items: the engine says nothing for this many seconds."""

    def __init__(self, seconds):
        self.seconds = seconds


def source(items, *, gap=0.0, closed=None):
    """An async iterator over ``items``. An exception in the list is raised; ``gap`` seconds pass before each item."""
    async def gen():
        try:
            for item in items:
                if gap:
                    await asyncio.sleep(gap)
                if isinstance(item, Sleep):
                    await asyncio.sleep(item.seconds)
                    continue
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if closed is not None:
                closed.append(True)
    return gen()


async def stream_of(items, *, options=OPTIONS, window=0.5, interval=10.0, gap=0.0, input_tokens=12, closed=None):
    stream = am.MessageStream(source(items, gap=gap, closed=closed), options, input_tokens, describe, ping_interval=interval)
    failure = await stream.start(window)
    if failure is not None:
        await stream.aclose()
        return failure, []
    return None, parse_sse([e async for e in stream.events()])


def run_stream(items, **kw):
    return asyncio.run(stream_of(items, **kw))[1]


class TestStreamEvents:
    def test_text_only(self):
        events = run_stream([ROLE, text("Hel"), text("lo"), finish("stop")], input_tokens=12)
        assert_well_formed(events)
        assert names(events) == ["message_start", "ping", "content_block_start", "content_block_delta", "content_block_delta",
                                 "content_block_stop", "message_delta", "message_stop"]
        start = events[0][1]["message"]
        assert start["content"] == [] and start["stop_reason"] is None and start["stop_sequence"] is None
        assert start["role"] == "assistant" and start["model"] == "claude-x" and start["id"].startswith("msg_") and len(start["id"]) == 28
        assert start["usage"] == {"input_tokens": 12, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        assert events[2][1] == {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
        assert events[3][1]["delta"] == {"type": "text_delta", "text": "Hel"}
        assert events[6][1] == {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}}  # ceil(5 / 4)

    def test_the_exact_wire_format(self):
        async def go():
            stream = am.MessageStream(source([text("a"), finish()]), OPTIONS, 3, describe)
            await stream.start(0.5)
            return [e async for e in stream.events()]
        raw = asyncio.run(go())
        assert raw[1] == 'event: ping\ndata: {"type": "ping"}\n\n'
        assert raw[3] == 'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "a"}}\n\n'
        assert raw[-1] == 'event: message_stop\ndata: {"type": "message_stop"}\n\n'

    def test_thinking_then_text(self):
        events = run_stream([reasoning("Hm"), reasoning("m."), text("Answer."), finish()], options=am.ReplyOptions("m", show_thinking=True))
        assert_well_formed(events)
        assert names(events)[2:] == ["content_block_start", "content_block_delta", "content_block_delta", "content_block_delta",
                                     "content_block_stop", "content_block_start", "content_block_delta", "content_block_stop",
                                     "message_delta", "message_stop"]
        first, second = blocks(events)
        assert first["block"] == {"type": "thinking", "thinking": "", "signature": ""} and first["text"] == "Hmm." and first["signature"] == ""
        assert second["block"] == {"type": "text", "text": ""} and second["text"] == "Answer."
        assert events[5][1]["delta"] == {"type": "signature_delta", "signature": ""}  # right before the thinking block stops

    def test_hidden_thinking_sends_nothing_for_the_reasoning(self):
        events = run_stream([reasoning("secret"), reasoning(" more"), text("Answer."), finish()])
        assert_well_formed(events)
        assert [b["text"] for b in blocks(events)] == ["Answer."]
        assert "secret" not in json.dumps(events)
        # the hidden reasoning still counts toward the estimate of output tokens
        assert events[-2][1]["usage"]["output_tokens"] == am.tokens_for_chars(len("secret more") + len("Answer."))

    def test_text_then_two_tool_calls(self):
        events = run_stream([text("Checking."), call(0, "call1", "get_weather", '{"city":"Paris"}'), call(1, "call2", "get_time", "{}"), finish("tool_calls")])
        assert_well_formed(events)
        text_block, first, second = blocks(events)
        assert text_block["text"] == "Checking."
        assert first["block"] == {"type": "tool_use", "id": "call1", "name": "get_weather", "input": {}}
        assert json.loads(first["json"]) == {"city": "Paris"}
        assert second["block"] == {"type": "tool_use", "id": "call2", "name": "get_time", "input": {}} and second["json"] == "{}"
        assert [b["index"] for n, b in events if n == "content_block_start"] == [0, 1, 2]
        assert events[-2][1]["delta"]["stop_reason"] == "tool_use"

    def test_a_tool_call_only(self):
        events = run_stream([ROLE, call(0, "call1", "get_weather", '{"city":"Paris"}'), finish("tool_calls")])
        assert_well_formed(events)
        assert names(events) == ["message_start", "ping", "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"]
        assert events[3][1]["delta"] == {"type": "input_json_delta", "partial_json": '{"city":"Paris"}'}
        assert events[-2][1]["delta"] == {"stop_reason": "tool_use", "stop_sequence": None}

    def test_a_call_that_arrives_in_pieces(self):
        pieces = [call(0, "c1", "get_weather", ""), call(0, None, None, '{"ci'), call(0, None, None, 'ty":"Paris"}'), finish("tool_calls")]
        # a continuation names neither id nor function name
        for p in pieces[1:3]:
            f = p["choices"][0]["delta"]["tool_calls"][0]
            f.pop("id"), f["function"].pop("name")
        events = run_stream(pieces)
        assert_well_formed(events)
        (only,) = blocks(events)
        assert only["block"]["name"] == "get_weather" and json.loads(only["json"]) == {"city": "Paris"}

    def test_only_the_first_call_is_streamed_when_parallel_calls_are_off(self):
        events = run_stream([call(0, "c1", "get_weather", "{}"), call(1, "c2", "get_time", "{}"), finish("tool_calls")],
                            options=am.ReplyOptions("m", first_call_only=True))
        assert_well_formed(events)
        assert [b["block"]["id"] for b in blocks(events)] == ["c1"]

    @pytest.mark.parametrize("reason, want", [("stop", "end_turn"), ("length", "max_tokens"), (None, "end_turn"), ("tool_calls", "end_turn")])
    def test_stop_reasons_without_calls(self, reason, want):
        items = [text("x"), finish(reason)] if reason else [text("x")]
        assert run_stream(items)[-2][1]["delta"]["stop_reason"] == want

    def test_output_tokens_are_the_engines_when_it_gave_them(self):
        events = run_stream([text("abcdefgh"), finish("stop", usage={"prompt_tokens": None, "completion_tokens": 9, "total_tokens": None})])
        assert events[-2][1]["usage"] == {"output_tokens": 9}
        events = run_stream([text("abcdefgh"), finish("stop", usage={"prompt_tokens": None, "completion_tokens": None})])
        assert events[-2][1]["usage"] == {"output_tokens": 2}  # ceil(8 / 4)

    def test_a_reply_with_nothing_in_it_still_closes_properly(self):
        events = run_stream([finish("stop")])
        assert names(events) == ["message_start", "ping", "message_delta", "message_stop"]
        assert names(run_stream([])) == ["message_start", "ping", "message_delta", "message_stop"]

    def test_odd_chunks_are_skipped(self):
        events = run_stream(["junk", {"choices": []}, {"choices": [None]}, {"choices": [{"delta": None}]}, text("ok"), finish()])
        assert_well_formed(events)
        assert blocks(events)[0]["text"] == "ok"

    def test_text_after_a_tool_call_opens_a_new_block(self):
        events = run_stream([call(0, "c1", "get_time", "{}"), text("after"), finish("tool_calls")])
        assert_well_formed(events)
        assert [b["block"]["type"] for b in blocks(events)] == ["tool_use", "text"]


class TestStreamFailures:
    def test_an_error_before_the_first_event_is_returned_not_streamed(self):
        failure, events = asyncio.run(stream_of([ERROR]))
        assert failure == am.Failure(500, "the engine broke", "inference_error") and events == []

    def test_an_exception_before_the_first_event_is_returned_not_streamed(self):
        failure, events = asyncio.run(stream_of([ValueError("x")]))
        assert failure == am.Failure(502, "ValueError happened", None) and events == []

    def test_the_source_is_closed_when_the_start_failed(self):
        closed = []
        asyncio.run(stream_of([ERROR], closed=closed))
        assert closed == [True]

    def test_an_error_after_text_is_an_error_event_and_the_stream_ends(self):
        events = run_stream([text("partial"), ERROR, text("never sent")])
        assert names(events)[-1] == "error" and "message_stop" not in names(events) and "message_delta" not in names(events)
        assert events[-1][1] == {"type": "error", "error": {"type": "api_error", "message": "the engine broke"}}
        assert "never sent" not in json.dumps(events)

    def test_an_exception_mid_stream_is_an_error_event(self):
        events = run_stream([text("partial"), RuntimeError("x")])
        assert events[-1] == ("error", {"type": "error", "error": {"type": "api_error", "message": "RuntimeError happened"}})

    def test_the_kind_follows_the_status(self):
        def overloaded(_):
            return am.Failure(503, "busy", "gpu_out_of_memory")
        async def go():
            stream = am.MessageStream(source([text("a"), ERROR]), OPTIONS, 1, overloaded)
            await stream.start(0.5)
            return parse_sse([e async for e in stream.events()])
        assert asyncio.run(go())[-1][1]["error"]["type"] == "overloaded_error"

    def test_an_engine_that_is_quiet_past_the_window_starts_the_stream_and_a_later_failure_is_an_event(self):
        async def go():
            stream = am.MessageStream(source([ERROR], gap=0.15), OPTIONS, 1, describe, ping_interval=10)
            failure = await stream.start(0.02)
            return failure, parse_sse([e async for e in stream.events()])
        failure, events = asyncio.run(go())
        assert failure is None
        assert names(events) == ["message_start", "ping", "error"]

    def test_closing_the_stream_early_closes_the_source(self):
        closed = []
        async def go():
            stream = am.MessageStream(source([text("a"), text("b"), text("c"), finish()], gap=0.01, closed=closed), OPTIONS, 1, describe)
            await stream.start(0.5)
            events = stream.events()
            first = [await events.__anext__() for _ in range(3)]
            await events.aclose()
            return first
        first = asyncio.run(go())
        assert len(first) == 3 and closed == [True]

    def test_cancelling_a_waiting_stream_closes_the_source(self):
        closed = []
        async def go():
            stream = am.MessageStream(source([text("a")], gap=5, closed=closed), OPTIONS, 1, describe, ping_interval=10)
            await stream.start(0.01)
            task = asyncio.create_task(_drain(stream))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async def _drain(stream):
            async for _ in stream.events():
                pass
        asyncio.run(go())
        assert closed == [True]


class TestPings:
    def test_one_ping_follows_message_start(self):
        assert names(run_stream([text("a"), finish()]))[:2] == ["message_start", "ping"]

    def test_pings_fill_a_silent_gap_and_never_come_between_a_chunk_and_its_events(self):
        # The engine says nothing for 0.25 s (a long prompt) with a ping every 0.05 s.
        async def go():
            stream = am.MessageStream(source([Sleep(0.25), text("late"), finish()]), OPTIONS, 1, describe, ping_interval=0.05)
            await stream.start(0.01)
            return parse_sse([e async for e in stream.events()])
        events = asyncio.run(go())
        before_first_block = names(events)[: names(events).index("content_block_start")]
        assert before_first_block[:2] == ["message_start", "ping"]
        assert before_first_block.count("ping") >= 4
        assert names(events)[len(before_first_block):] == ["content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"]

    def test_pings_still_come_while_hidden_reasoning_chunks_arrive(self):
        # Chunks arrive every 0.02 s but none is sent on: the silence is what counts.
        chunks = [reasoning("x")] * 10 + [text("done"), finish()]
        async def go():
            stream = am.MessageStream(source(chunks, gap=0.02), OPTIONS, 1, describe, ping_interval=0.05)
            await stream.start(0.01)
            return parse_sse([e async for e in stream.events()])
        events = asyncio.run(go())
        assert names(events).count("ping") >= 3
        assert_well_formed(events)

    def test_no_extra_ping_while_events_flow(self):
        assert names(run_stream([text("a")] * 20 + [finish()], interval=5.0)).count("ping") == 1

    def test_a_clock_that_runs_ahead_makes_pings_due_and_the_chunks_still_arrive_in_order(self):
        ticks = itertools.count(0, 100)  # every look at the clock is 100 s later than the last

        async def go():
            stream = am.MessageStream(source([Sleep(0.02), text("a"), text("b"), finish()]), OPTIONS, 1, describe,
                                      ping_interval=10, clock=lambda: next(ticks))
            await stream.start(0.001)
            return parse_sse([e async for e in stream.events()])

        events = asyncio.run(go())
        assert names(events).count("ping") >= 2
        assert_well_formed(events)
        assert blocks(events)[0]["text"] == "ab"

    def test_a_clock_that_stands_still_never_adds_a_ping(self):
        async def go():
            stream = am.MessageStream(source([text("a"), finish()]), OPTIONS, 1, describe, ping_interval=10, clock=lambda: 0.0)
            await stream.start(0.5)
            return parse_sse([e async for e in stream.events()])

        assert names(asyncio.run(go())).count("ping") == 1

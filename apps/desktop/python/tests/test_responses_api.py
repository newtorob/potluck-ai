"""The Responses API translation on its own: request in, Response and events out.

No HTTP and no engine. test_responses_route.py runs the same code through the real
route against a fake runtime.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from potluck_local.services import responses_api as ra
from potluck_local.services.gateway_request import RequestError
from responses_support import assert_well_formed, items, names, parse_sse

BASE = {"model": "m", "input": "hi"}
FN = {"type": "function", "name": "get_weather", "description": "Weather", "strict": False,
      "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}
PATCH = {"type": "custom", "name": "apply_patch", "description": "Edit files.",
         "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.+/"}}


def parse(**fields):
    return ra.parse_request({**BASE, **fields}, default_temperature=0.7)


def messages_of(**fields):
    return parse(**fields).prepared.messages


def refused(**fields):
    with pytest.raises(RequestError) as info:
        parse(**fields)
    return info.value


def text_message(role, *parts, kind="input_text"):
    return {"type": "message", "role": role, "content": [{"type": kind, "text": t} for t in parts]}


# ---------------------------------------------------------------------------
# Request: input
# ---------------------------------------------------------------------------


class TestInput:
    def test_a_string_is_a_user_message(self):
        assert messages_of(input="hello") == [{"role": "user", "content": "hello"}]

    def test_instructions_and_developer_messages_make_one_system_message_first(self):
        raw = [text_message("developer", "dev rules"), text_message("user", "q"),
               {"role": "system", "content": "sys"}]
        assert messages_of(input=raw, instructions="be brief") == [
            {"role": "system", "content": "be brief\n\ndev rules\n\nsys"}, {"role": "user", "content": "q"}]

    def test_message_items_without_a_type_are_messages(self):
        assert messages_of(input=[{"role": "user", "content": "plain"}]) == [{"role": "user", "content": "plain"}]

    def test_output_text_and_refusal_parts_are_text(self):
        raw = [text_message("user", "q"), text_message("assistant", "a1", "a2", kind="output_text"),
               {"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "no"}]}]
        # The two assistant messages are neighbours, so they become one.
        assert messages_of(input=raw)[1:] == [{"role": "assistant", "content": "a1\na2\n\nno"}]

    def test_reasoning_and_compaction_items_are_dropped(self):
        raw = [text_message("user", "q"), {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "x"},
               {"type": "compaction", "encrypted_content": "y"}, text_message("user", "again")]
        assert messages_of(input=raw) == [{"role": "user", "content": "q\n\nagain"}]

    def test_extra_fields_on_items_are_ignored(self):
        raw = [{"type": "message", "id": "msg_1", "role": "user", "phase": "x", "content": [{"type": "input_text", "text": "q"}]}]
        assert messages_of(input=raw) == [{"role": "user", "content": "q"}]

    @pytest.mark.parametrize("part", [{"type": "input_image", "image_url": "data:..."}, {"type": "input_file", "file_id": "f"}])
    def test_images_and_files_are_refused_by_type(self, part):
        err = refused(input=[{"type": "message", "role": "user", "content": [part]}])
        assert err.code == "unsupported_content_part" and part["type"] in err.message and "text only" in err.message

    def test_an_image_in_a_tool_output_becomes_a_line_of_text(self):
        raw = [{"type": "function_call_output", "call_id": "c1",
                "output": [{"type": "input_text", "text": "seen"}, {"type": "input_image", "image_url": "x"}]}]
        assert messages_of(input=raw)[0] == {"role": "tool", "content": f"seen\n\n{ra.IMAGE_LEFT_OUT}", "tool_call_id": "c1"}

    @pytest.mark.parametrize("raw", [[], 5, None, ["text"], [{"type": "mystery"}], [{"role": "tool", "content": "x"}]])
    def test_bad_input_is_refused(self, raw):
        assert refused(input=raw).code in {"invalid_input", "invalid_input_item", "unsupported_input_item", "invalid_role"}

    def test_an_item_reference_says_why(self):
        err = refused(input=[{"type": "item_reference", "id": "msg_1"}])
        assert err.code == "item_reference_not_supported" and "keeps none" in err.message


class TestToolHistory:
    def test_calls_and_their_outputs(self):
        raw = [text_message("user", "weather?"),
               {"type": "function_call", "call_id": "c1", "name": "get_weather", "arguments": '{"city": "Paris"}'},
               {"type": "function_call", "call_id": "c2", "name": "get_weather", "arguments": '{"city": "Rome"}'},
               {"type": "function_call_output", "call_id": "c1", "output": "sunny"},
               {"type": "function_call_output", "call_id": "c2", "output": "rain"}]
        assert messages_of(input=raw, tools=[FN]) == [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": {"city": "Paris"}}},
                {"id": "c2", "type": "function", "function": {"name": "get_weather", "arguments": {"city": "Rome"}}}]},
            {"role": "tool", "content": "sunny", "tool_call_id": "c1"},
            {"role": "tool", "content": "rain", "tool_call_id": "c2"}]

    def test_a_call_joins_the_assistant_message_before_it(self):
        raw = [text_message("user", "q"), text_message("assistant", "Let me look.", kind="output_text"),
               {"type": "function_call", "call_id": "c1", "name": "get_weather", "arguments": "{}"}]
        sent = messages_of(input=raw, tools=[FN])
        assert len(sent) == 2 and sent[1]["content"] == "Let me look." and sent[1]["tool_calls"][0]["id"] == "c1"

    def test_empty_arguments_are_an_empty_object(self):
        raw = [{"type": "function_call", "call_id": "c1", "name": "get_weather", "arguments": ""}]
        assert messages_of(input=raw, tools=[FN])[0]["tool_calls"][0]["function"]["arguments"] == {}

    def test_a_freeform_call_and_its_output(self):
        raw = [{"type": "custom_tool_call", "call_id": "c1", "name": "apply_patch", "input": "*** Begin Patch"},
               {"type": "custom_tool_call_output", "call_id": "c1", "output": "done"}]
        sent = messages_of(input=raw, tools=[PATCH])
        assert sent[0]["tool_calls"][0]["function"] == {"name": "apply_patch", "arguments": {"input": "*** Begin Patch"}}
        assert sent[1] == {"role": "tool", "content": "done", "tool_call_id": "c1"}

    @pytest.mark.parametrize("item", [
        {"type": "function_call", "name": "get_weather", "arguments": "{}"},
        {"type": "function_call", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call_output", "output": "x"},
    ])
    def test_a_call_needs_its_id_and_name(self, item):
        assert refused(input=[item], tools=[FN]).code == "invalid_input_item"


# ---------------------------------------------------------------------------
# Request: tools and options
# ---------------------------------------------------------------------------


class TestTools:
    def test_a_function_tool_is_passed_on(self):
        tools = parse(tools=[FN]).prepared.tools
        assert tools == [{"type": "function", "function": {"name": "get_weather", "description": "Weather", "parameters": FN["parameters"]}}]

    def test_a_freeform_tool_is_a_function_with_one_string_parameter(self):
        parsed = parse(tools=[PATCH])
        (tool,) = parsed.prepared.tools
        assert tool["function"]["name"] == "apply_patch" and tool["function"]["parameters"]["required"] == ["input"]
        assert parsed.names.custom == {"apply_patch"}

    def test_a_namespace_is_flattened_and_remembered(self):
        space = {"type": "namespace", "name": "agents", "description": "d", "tools": [
            {"type": "function", "name": "spawn", "parameters": {"type": "object"}}, FN]}
        parsed = parse(tools=[space])
        assert [t["function"]["name"] for t in parsed.prepared.tools] == ["agents__spawn", "agents__get_weather"]
        assert parsed.names.split("agents__spawn") == ("agents", "spawn")
        assert parsed.names.flat("agents", "spawn") == "agents__spawn" and parsed.names.flat(None, "x") == "x"

    def test_a_namespace_that_ends_in_an_underscore_is_joined_plainly(self):
        parsed = parse(tools=[{"type": "namespace", "name": "mcp__srv__", "tools": [{"type": "function", "name": "read"}]}])
        assert parsed.prepared.tools[0]["function"]["name"] == "mcp__srv__read"

    def test_flattened_names_never_collide_or_pass_64_characters(self):
        long = "n" * 60
        space = {"type": "namespace", "name": long, "tools": [{"type": "function", "name": "a" * 20}, {"type": "function", "name": "a" * 20 + "x"}]}
        top = {"type": "function", "name": "agents__spawn"}
        parsed = parse(tools=[top, {"type": "namespace", "name": "agents", "tools": [{"type": "function", "name": "spawn"}]}, space])
        flat = [t["function"]["name"] for t in parsed.prepared.tools]
        assert len(set(flat)) == len(flat) and all(len(n) <= 64 for n in flat)
        assert flat[1] == "agents__spawn_2"

    def test_a_history_call_in_a_namespace_gets_the_flat_name(self):
        space = {"type": "namespace", "name": "agents", "tools": [{"type": "function", "name": "spawn"}]}
        raw = [{"type": "function_call", "call_id": "c1", "namespace": "agents", "name": "spawn", "arguments": "{}"}]
        assert messages_of(input=raw, tools=[space])[0]["tool_calls"][0]["function"]["name"] == "agents__spawn"

    def test_hosted_tools_are_left_out_and_named(self):
        parsed = parse(tools=[FN, {"type": "web_search", "external_web_access": False}, {"type": "file_search"}])
        assert [t["function"]["name"] for t in parsed.prepared.tools] == ["get_weather"]
        assert parsed.ignored_tools == ("web_search", "file_search")

    def test_only_hosted_tools_means_no_tools(self):
        parsed = parse(tools=[{"type": "web_search"}])
        assert parsed.prepared.tools is None and not parsed.prepared.needs_tool_support

    @pytest.mark.parametrize("tools", [
        "x", ["x"], [{"type": "function"}], [{"type": "function", "name": "bad name"}],
        [{"type": "namespace", "name": "n", "tools": [{"type": "custom", "name": "p"}]}],
        [{"type": "namespace", "name": "n"}],
    ])
    def test_bad_tools_are_refused(self, tools):
        assert refused(tools=tools).code == "invalid_tools"

    @pytest.mark.parametrize("choice, want", [
        (None, "auto"), ("auto", "auto"), ("required", "required"), ("none", None),
        ({"type": "function", "name": "get_weather"}, {"type": "function", "function": {"name": "get_weather"}}),
        ({"type": "function", "function": {"name": "get_weather"}}, None),
        ({"type": "allowed_tools", "mode": "required", "tools": []}, "required"),
    ])
    def test_tool_choice(self, choice, want):
        if isinstance(choice, dict) and "function" in choice:
            assert refused(tools=[FN], tool_choice=choice).code == "invalid_tool_choice"
            return
        assert parse(tools=[FN], tool_choice=choice).prepared.tool_choice == want

    def test_tool_choice_can_name_a_freeform_or_namespaced_tool(self):
        space = {"type": "namespace", "name": "agents", "tools": [{"type": "function", "name": "spawn"}]}
        choice = {"type": "function", "namespace": "agents", "name": "spawn"}
        assert parse(tools=[space], tool_choice=choice).prepared.tool_choice == {"type": "function", "function": {"name": "agents__spawn"}}
        assert parse(tools=[PATCH], tool_choice={"type": "custom", "name": "apply_patch"}).prepared.tool_choice["function"]["name"] == "apply_patch"

    @pytest.mark.parametrize("choice", [{"type": "web_search"}, {"type": "function"}, 5, {"type": "function", "name": "nope"}])
    def test_bad_tool_choices_are_refused(self, choice):
        assert refused(tools=[FN], tool_choice=choice).code == "invalid_tool_choice"

    def test_parallel_tool_calls_false_keeps_the_first_call(self):
        assert parse().first_call_only is False
        assert parse(parallel_tool_calls=True).first_call_only is False
        assert parse(parallel_tool_calls=False).first_call_only is True


class TestOptions:
    def test_defaults_and_settings(self):
        parsed = parse()
        assert (parsed.model, parsed.max_tokens, parsed.temperature, parsed.stream) == ("m", None, 0.7, False)
        parsed = parse(max_output_tokens=9, temperature=0, stream=True, top_p=0.5, store=False, metadata={"a": "b"},
                       include=["reasoning.encrypted_content"], reasoning={"summary": "auto"}, prompt_cache_key="k",
                       client_metadata={"x": "y"}, service_tier="auto", user="u", something_new=1)
        assert (parsed.max_tokens, parsed.temperature, parsed.stream) == (9, 0.0, True)
        assert parsed.echo["metadata"] == {"a": "b"} and parsed.echo["top_p"] == 0.5

    @pytest.mark.parametrize("fields, code", [
        ({"max_output_tokens": 0}, "invalid_max_output_tokens"), ({"max_output_tokens": "5"}, "invalid_max_output_tokens"),
        ({"temperature": 3}, "invalid_temperature"), ({"temperature": True}, "invalid_temperature"),
        ({"stream": "yes"}, "invalid_stream"), ({"model": 5}, "invalid_model"), ({"instructions": ["x"]}, "invalid_instructions"),
        ({"previous_response_id": "resp_1"}, "previous_response_id_not_supported"),
        ({"conversation": "conv_1"}, "previous_response_id_not_supported"),
        ({"background": True}, "background_not_supported"),
        ({"text": {"format": {"type": "json_schema", "name": "x", "schema": {}}}}, "unsupported_output_format"),
    ])
    def test_refusals(self, fields, code):
        assert refused(**fields).code == code

    def test_previous_response_id_explains_the_alternative(self):
        message = refused(previous_response_id="resp_1").message
        assert "no response store" in message and "full conversation" in message

    def test_plain_text_format_is_fine(self):
        assert parse(text={"format": {"type": "text"}}).model == "m"

    def test_a_body_that_is_not_an_object(self):
        with pytest.raises(RequestError):
            ra.parse_request([], default_temperature=0.7)

    def test_the_error_body_has_openai_shape(self):
        assert ra.error_body(400, "bad", "code_x") == {"error": {"message": "bad", "type": "invalid_request_error", "param": None, "code": "code_x"}}
        assert ra.error_body(503, "busy")["error"]["type"] == "server_error"
        assert ra.error_body(401, "no")["error"]["type"] == "authentication_error"


# ---------------------------------------------------------------------------
# Whole replies
# ---------------------------------------------------------------------------


def completion(text=None, calls=(), finish="stop", usage=None, reasoning=None):
    message = {"role": "assistant", "content": text}
    if reasoning:
        message["reasoning_content"] = reasoning
    if calls:
        message["tool_calls"] = [{"id": cid, "type": "function", "function": {"name": name, "arguments": args}} for cid, name, args in calls]
    result = {"choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    if usage:
        result["usage"] = usage
    return result


def reply(result, request=None, estimate=11):
    return ra.response_from_completion(result, request or parse(), "m", estimate)


class TestWholeReply:
    def test_a_text_reply(self):
        body, estimated = reply(completion("pong", usage={"prompt_tokens": 7, "completion_tokens": 5}), parse(metadata={"k": "v"}, instructions="sys"))
        assert not estimated
        assert body["id"].startswith("resp_") and body["object"] == "response" and body["status"] == "completed"
        assert body["model"] == "m" and body["error"] is None and body["incomplete_details"] is None
        assert body["metadata"] == {"k": "v"} and body["instructions"] == "sys" and type(body["created_at"]) is int
        (item,) = body["output"]
        assert item["id"].startswith("msg_") and item["type"] == "message" and item["role"] == "assistant" and item["status"] == "completed"
        assert item["content"] == [{"type": "output_text", "text": "pong", "annotations": [], "logprobs": []}]
        assert body["usage"] == {"input_tokens": 7, "input_tokens_details": {"cached_tokens": 0},
                                 "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 12}

    def test_reasoning_is_never_returned(self):
        body, _ = reply(completion("answer", reasoning="secret thoughts"))
        assert "secret thoughts" not in json.dumps(body)

    def test_usage_is_estimated_when_the_engine_gave_none(self):
        body, estimated = reply(completion("a" * 40), estimate=11)
        assert estimated and body["usage"]["input_tokens"] == 11 and body["usage"]["output_tokens"] == 10

    def test_function_calls_come_with_the_engines_id(self):
        body, _ = reply(completion(None, [("call1", "get_weather", '{"city": "Paris"}')], finish="tool_calls"), parse(tools=[FN]))
        (item,) = body["output"]
        assert item["type"] == "function_call" and item["call_id"] == "call1" and item["name"] == "get_weather"
        assert item["arguments"] == '{"city": "Paris"}' and item["id"].startswith("fc_") and item["status"] == "completed"

    def test_text_then_calls_in_order(self):
        body, _ = reply(completion("Looking.", [("a", "get_weather", "{}"), ("b", "get_weather", "{}")]), parse(tools=[FN]))
        assert [i["type"] for i in body["output"]] == ["message", "function_call", "function_call"]

    def test_only_the_first_call_when_parallel_calls_are_off(self):
        body, _ = reply(completion(None, [("a", "get_weather", "{}"), ("b", "get_weather", "{}")]), parse(tools=[FN], parallel_tool_calls=False))
        assert [i["call_id"] for i in body["output"]] == ["a"]

    def test_object_arguments_become_a_json_string(self):
        body, _ = reply(completion(None, [("a", "get_weather", {"city": "Rome"})]), parse(tools=[FN]))
        assert json.loads(body["output"][0]["arguments"]) == {"city": "Rome"}

    def test_a_namespaced_call_comes_back_with_its_namespace(self):
        request = parse(tools=[{"type": "namespace", "name": "agents", "tools": [{"type": "function", "name": "spawn"}]}])
        body, _ = reply(completion(None, [("a", "agents__spawn", "{}")]), request)
        assert body["output"][0]["namespace"] == "agents" and body["output"][0]["name"] == "spawn"

    def test_a_freeform_call_carries_the_string_not_json(self):
        patch = "*** Begin Patch\n*** End Patch"
        body, _ = reply(completion(None, [("a", "apply_patch", json.dumps({"input": patch}))]), parse(tools=[PATCH]))
        (item,) = body["output"]
        assert item["type"] == "custom_tool_call" and item["input"] == patch and item["call_id"] == "a"
        assert item["id"].startswith("ctc_") and "arguments" not in item

    def test_a_reply_cut_off_by_length_is_incomplete(self):
        body, _ = reply(completion("cut", finish="length"))
        assert body["status"] == "incomplete" and body["incomplete_details"] == {"reason": "max_output_tokens"}

    def test_an_empty_reply_has_no_output(self):
        body, _ = reply(completion(""))
        assert body["output"] == [] and body["status"] == "completed"


# ---------------------------------------------------------------------------
# Streamed replies
# ---------------------------------------------------------------------------


def delta(content=None, calls=None, finish=None, usage=None, reasoning=None):
    d = {}
    if content is not None:
        d["content"] = content
    if reasoning is not None:
        d["reasoning_content"] = reasoning
    if calls is not None:
        d["tool_calls"] = calls
    chunk = {"choices": [{"index": 0, "delta": d, "finish_reason": finish}]}
    if usage:
        chunk["usage"] = usage
    return chunk


def call_chunk(index, call_id=None, name=None, args=None):
    function = {}
    if name is not None:
        function["name"] = name
    if args is not None:
        function["arguments"] = args
    call = {"index": index, "type": "function", "function": function}
    if call_id:
        call["id"] = call_id
    return delta(calls=[call])


async def _source(chunks):
    for chunk in chunks:
        yield chunk


def run(chunks, request=None, describe=None):
    """The parsed events of a stream of ``chunks``."""
    stream = ra.ResponseStream(_source(chunks), request or parse(), "m", 12,
                               describe or (lambda f: ra.Failure(500, "boom", "x")))

    async def go():
        assert await stream.start(1) is None
        return [e async for e in stream.events()]

    raw = asyncio.run(go())
    return parse_sse([e for e in raw if not e.startswith(":")])


class TestStreaming:
    def test_a_text_reply_has_the_documented_event_order(self):
        events = run([delta("Hel"), delta("lo"), delta(finish="stop", usage={"completion_tokens": 4})])
        assert names(events) == [
            "response.created", "response.in_progress", "response.output_item.added", "response.content_part.added",
            "response.output_text.delta", "response.output_text.delta", "response.output_text.done",
            "response.content_part.done", "response.output_item.done", "response.completed"]
        assert_well_formed(events)

    def test_the_event_payloads(self):
        events = dict(run([delta("Hi"), delta(finish="stop", usage={"completion_tokens": 4})]))
        created = events["response.created"]["response"]
        assert created["status"] == "in_progress" and created["output"] == [] and created["usage"] is None
        added = events["response.output_item.added"]
        assert added["output_index"] == 0 and added["item"]["type"] == "message" and added["item"]["status"] == "in_progress"
        assert added["item"]["content"] == []
        item_id = added["item"]["id"]
        part = events["response.content_part.added"]
        assert part["item_id"] == item_id and part["content_index"] == 0 and part["part"]["type"] == "output_text"
        text = events["response.output_text.delta"]
        assert text["delta"] == "Hi" and text["item_id"] == item_id and text["content_index"] == 0 and text["logprobs"] == []
        assert events["response.output_text.done"]["text"] == "Hi"
        assert events["response.content_part.done"]["part"]["text"] == "Hi"
        done = events["response.output_item.done"]["item"]
        assert done["id"] == item_id and done["status"] == "completed" and done["content"][0]["text"] == "Hi"
        final = events["response.completed"]["response"]
        assert final["status"] == "completed" and final["id"] == created["id"]
        assert final["usage"]["input_tokens"] == 12 and final["usage"]["output_tokens"] == 4 and final["usage"]["total_tokens"] == 16

    def test_output_tokens_are_estimated_when_the_engine_gave_none(self):
        events = run([delta("a" * 40), delta(finish="stop")])
        assert events[-1][1]["response"]["usage"]["output_tokens"] == 10

    def test_reasoning_is_not_shown(self):
        events = run([delta(reasoning="secret"), delta("answer"), delta(finish="stop")])
        assert "secret" not in json.dumps(events)
        assert [i["type"] for i in items(events)] == ["message"]

    def test_a_function_call_streams_its_arguments(self):
        events = run([call_chunk(0, "call1", "get_weather", ""), call_chunk(0, args='{"ci'), call_chunk(0, args='ty": "Paris"}'),
                      delta(finish="tool_calls")], parse(tools=[FN]))
        assert names(events) == [
            "response.created", "response.in_progress", "response.output_item.added",
            "response.function_call_arguments.delta", "response.function_call_arguments.delta",
            "response.function_call_arguments.done", "response.output_item.done", "response.completed"]
        assert_well_formed(events)
        added = events[2][1]
        assert added["item"] == {"id": added["item"]["id"], "type": "function_call", "status": "in_progress",
                                 "call_id": "call1", "name": "get_weather", "arguments": ""}
        assert events[3][1]["item_id"] == added["item"]["id"] and events[3][1]["delta"] == '{"ci'
        assert events[5][1]["arguments"] == '{"city": "Paris"}'
        (done,) = items(events)
        assert done["arguments"] == '{"city": "Paris"}' and done["status"] == "completed" and done["call_id"] == "call1"

    def test_text_then_two_calls_are_three_items_in_order(self):
        events = run([delta("Looking."), call_chunk(0, "a", "get_weather", '{"city": "A"}'),
                      call_chunk(1, "b", "get_weather", '{"city": "B"}'), delta(finish="tool_calls")], parse(tools=[FN]))
        assert_well_formed(events)
        assert [(i["type"], i.get("call_id")) for i in items(events)] == [("message", None), ("function_call", "a"), ("function_call", "b")]
        assert [b["output_index"] for n, b in events if n == "response.output_item.added"] == [0, 1, 2]

    def test_parallel_calls_off_drops_the_later_ones(self):
        events = run([call_chunk(0, "a", "get_weather", "{}"), call_chunk(1, "b", "get_weather", '{"x": 1}'), delta(finish="tool_calls")],
                     parse(tools=[FN], parallel_tool_calls=False))
        assert_well_formed(events)
        assert [i["call_id"] for i in items(events)] == ["a"]

    def test_a_call_without_arguments_has_an_empty_object(self):
        events = run([call_chunk(0, "a", "get_weather"), delta(finish="tool_calls")], parse(tools=[FN]))
        assert items(events)[0]["arguments"] == "{}"

    def test_a_namespaced_call_has_its_namespace_in_the_finished_item(self):
        request = parse(tools=[{"type": "namespace", "name": "agents", "tools": [{"type": "function", "name": "spawn"}]}])
        events = run([call_chunk(0, "a", "agents__spawn", "{}"), delta(finish="tool_calls")], request)
        (item,) = items(events)
        assert item["namespace"] == "agents" and item["name"] == "spawn"

    def test_a_freeform_call_streams_its_input_string(self):
        patch = "*** Begin Patch\n*** Add File: a\n+hi\n*** End Patch"
        args = json.dumps({"input": patch})
        events = run([call_chunk(0, "a", "apply_patch", args[:10]), call_chunk(0, args=args[10:]), delta(finish="tool_calls")], parse(tools=[PATCH]))
        assert names(events) == [
            "response.created", "response.in_progress", "response.output_item.added",
            "response.custom_tool_call_input.delta", "response.custom_tool_call_input.done",
            "response.output_item.done", "response.completed"]
        assert_well_formed(events)
        assert events[3][1]["delta"] == patch and events[4][1]["input"] == patch
        (item,) = items(events)
        assert item["type"] == "custom_tool_call" and item["input"] == patch and item["name"] == "apply_patch"
        assert events[2][1]["item"]["input"] == ""

    def test_a_reply_cut_off_by_length_ends_with_incomplete(self):
        events = run([delta("cut"), delta(finish="length")])
        assert events[-1][0] == "response.incomplete"
        response = events[-1][1]["response"]
        assert response["status"] == "incomplete" and response["incomplete_details"] == {"reason": "max_output_tokens"}
        assert_well_formed(events)

    def test_an_empty_stream_completes_with_no_output(self):
        events = run([delta(finish="stop")])
        assert names(events) == ["response.created", "response.in_progress", "response.completed"]

    def test_a_failure_after_the_start_is_response_failed(self):
        async def broken():
            yield delta("partial")
            raise RuntimeError("engine died")

        stream = ra.ResponseStream(broken(), parse(), "m", 1, lambda f: ra.Failure(502, "the engine died", "inference_failed"))

        async def go():
            assert await stream.start(1) is None
            return [e async for e in stream.events()]

        events = parse_sse(asyncio.run(go()))
        assert names(events)[-1] == "response.failed"
        response = events[-1][1]["response"]
        assert response["status"] == "failed" and response["error"] == {"code": "inference_failed", "message": "the engine died"}

    def test_the_engines_error_chunk_is_a_failure(self):
        seen = []

        def describe(failure):
            seen.append(failure)
            return ra.Failure(500, "broke", None)

        events = run([delta("x"), {"__error__": "the engine broke"}], describe=describe)
        assert seen == [{"__error__": "the engine broke"}] and events[-1][1]["response"]["error"]["code"] == "server_error"

    def test_a_failure_in_the_commit_window_is_returned_not_streamed(self):
        stream = ra.ResponseStream(_source([{"__error__": "x"}]), parse(), "m", 1, lambda f: ra.Failure(503, "no gpu", "oom"))

        async def go():
            failure = await stream.start(1)
            await stream.aclose()
            return failure

        assert asyncio.run(go()) == ra.Failure(503, "no gpu", "oom")

    def test_a_keepalive_comment_is_sent_while_the_engine_is_quiet(self):
        async def slow():
            await asyncio.sleep(0.35)
            yield delta("late")
            yield delta(finish="stop")

        stream = ra.ResponseStream(slow(), parse(), "m", 1, lambda f: ra.Failure(500, "x"), keepalive_interval=0.1)

        async def go():
            assert await stream.start(0.01) is None
            return [e async for e in stream.events()]

        raw = asyncio.run(go())
        assert raw[0].startswith("event: response.created") and ra.KEEPALIVE in raw
        assert raw.index(ra.KEEPALIVE) < next(i for i, e in enumerate(raw) if "output_text.delta" in e)
        assert_well_formed(parse_sse([e for e in raw if not e.startswith(":")]))

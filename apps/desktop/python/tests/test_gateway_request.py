"""Checking the tool-related parts of an OpenAI chat request."""
from __future__ import annotations

import pytest

from potluck_local.services import gateway_request as gr

WEATHER = {"type": "function", "function": {"name": "get_weather", "description": "Weather",
                                            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}


def msgs(*pairs):
    return [gr.normalise_message(role, content) for role, content in pairs]


# ---- messages ------------------------------------------------------------------


def test_a_plain_message_keeps_exactly_its_old_shape():
    assert gr.normalise_message("user", "hi") == {"role": "user", "content": "hi"}
    assert gr.normalise_message("user", None) == {"role": "user", "content": ""}


def test_text_parts_are_joined():
    parts = [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]
    assert gr.normalise_message("user", parts)["content"] == "one\ntwo"


@pytest.mark.parametrize("part", [
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    {"type": "input_audio", "input_audio": {"data": "x", "format": "wav"}},
    {"type": "text"},            # no text
    {"type": "text", "text": 5},
    {"text": "no type"},
    "bare string",
    None,
])
def test_other_part_types_are_refused_with_a_clear_message(part):
    with pytest.raises(gr.RequestError) as err:
        gr.normalise_message("user", [{"type": "text", "text": "ok"}, part])
    assert err.value.code == "unsupported_content_part"
    assert "Only text content is supported" in err.value.message and "'user'" in err.value.message


def test_the_part_type_is_named_only_when_it_is_a_plain_identifier():
    with pytest.raises(gr.RequestError) as err:
        gr.normalise_message("user", [{"type": "<script>alert(1)</script>"}])
    assert "script" not in err.value.message


def test_content_that_is_not_text_is_refused():
    with pytest.raises(gr.RequestError) as err:
        gr.normalise_message("user", {"a": 1})
    assert err.value.code == "invalid_content"


def test_assistant_tool_calls_get_their_arguments_as_an_object():
    call = {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}
    m = gr.normalise_message("assistant", None, tool_calls_=[call])
    assert m == {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": {"city": "Paris"}}}]}
    # the request's own dict is not changed
    assert call["function"]["arguments"] == '{"city": "Paris"}'


def test_tool_call_arguments_may_be_empty_missing_or_already_an_object():
    for args in ("", "  ", None, {}):
        call = {"id": "a", "type": "function", "function": {"name": "f", "arguments": args}}
        assert gr.normalise_message("assistant", "", tool_calls_=[call])["tool_calls"][0]["function"]["arguments"] == {}
    call = {"type": "function", "function": {"name": "f"}}
    assert gr.normalise_message("assistant", "", tool_calls_=[call])["tool_calls"][0] == {
        "type": "function", "function": {"name": "f", "arguments": {}}}


@pytest.mark.parametrize("bad", [
    "not a list", [5], [{"function": "x"}], [{"function": {"arguments": "{}"}}],
    [{"function": {"name": "f", "arguments": "{broken"}}],
    [{"function": {"name": "f", "arguments": "[1, 2]"}}],
    [{"function": {"name": "f", "arguments": 7}}],
    [{"function": {"name": "f"}}] * (gr.MAX_MESSAGE_TOOL_CALLS + 1),
])
def test_bad_assistant_tool_calls_are_refused(bad):
    with pytest.raises(gr.RequestError) as err:
        gr.normalise_message("assistant", "", tool_calls_=bad)
    assert err.value.code == "invalid_tool_calls"


def test_a_tool_message_keeps_its_call_id_and_name():
    m = gr.normalise_message("tool", '{"temp": 17}', tool_call_id="call_1", name="get_weather")
    assert m == {"role": "tool", "content": '{"temp": 17}', "tool_call_id": "call_1", "name": "get_weather"}
    assert gr.normalise_message("tool", "x") == {"role": "tool", "content": "x"}


def test_a_name_on_an_ordinary_message_is_not_forwarded():
    assert gr.normalise_message("user", "hi", name="alice") == {"role": "user", "content": "hi"}


def test_tool_history_is_noticed():
    assert not gr.has_tool_history(msgs(("user", "hi"), ("assistant", "yo")))
    assert gr.has_tool_history(msgs(("user", "hi"), ("tool", "{}")))
    assert gr.has_tool_history([gr.normalise_message("assistant", "", tool_calls_=[{"function": {"name": "f"}}])])


# ---- tools ---------------------------------------------------------------------


def test_function_tools_are_accepted_and_cleaned():
    extra = {"type": "function", "function": {"name": "f", "strict": True, "parameters": {"type": "object"}}, "cache": 1}
    assert gr.normalise_tools([WEATHER, extra]) == [
        WEATHER, {"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]


@pytest.mark.parametrize("bad,why", [
    ("tools", "must be a list"),
    ([5], "Only function tools"),
    ([{"type": "retrieval"}], "Only function tools"),
    ([{"type": "function"}], "Only function tools"),
    ([{"type": "function", "function": {"name": "bad name"}}], "tool name must be"),
    ([{"type": "function", "function": {"name": ""}}], "tool name must be"),
    ([{"type": "function", "function": {"name": "f", "parameters": "x"}}], "JSON schema object"),
    ([WEATHER, WEATHER], "more than once"),
])
def test_bad_tools_are_refused_with_a_reason(bad, why):
    with pytest.raises(gr.RequestError) as err:
        gr.normalise_tools(bad)
    assert err.value.code == "invalid_tools" and why in err.value.message


def test_too_many_or_too_large_tools_are_refused():
    many = [{"type": "function", "function": {"name": f"f{i}"}} for i in range(gr.MAX_TOOLS + 1)]
    with pytest.raises(gr.RequestError):
        gr.normalise_tools(many)
    huge = [{"type": "function", "function": {"name": "f", "description": "x" * gr.MAX_TOOLS_BYTES}}]
    with pytest.raises(gr.RequestError) as err:
        gr.normalise_tools(huge)
    assert "too large" in err.value.message


# ---- tool_choice ----------------------------------------------------------------


def test_tool_choice_values():
    tools = gr.normalise_tools([WEATHER])
    assert gr.resolve_tool_choice(None, tools) == "auto"
    for word in ("auto", "none", "required"):
        assert gr.resolve_tool_choice(word, tools) == word
    named = {"type": "function", "function": {"name": "get_weather"}}
    assert gr.resolve_tool_choice(named, tools) == named
    assert gr.resolve_tool_choice({"type": "function", "name": "get_weather"}, tools) == named


@pytest.mark.parametrize("choice", ["sometimes", 5, {"type": "function"}, {"type": "retrieval", "function": {"name": "get_weather"}},
                                    {"type": "function", "function": {"name": "nope"}}, ["auto"]])
def test_bad_tool_choice_is_refused(choice):
    with pytest.raises(gr.RequestError) as err:
        gr.resolve_tool_choice(choice, gr.normalise_tools([WEATHER]))
    assert err.value.code == "invalid_tool_choice"


def test_required_needs_tools_but_auto_and_none_do_not():
    assert gr.resolve_tool_choice("auto", []) == "auto"
    assert gr.resolve_tool_choice("none", []) == "none"
    with pytest.raises(gr.RequestError):
        gr.resolve_tool_choice("required", [])


# ---- the whole request -----------------------------------------------------------


def test_a_request_without_tools_changes_nothing():
    p = gr.prepare(msgs(("user", "hi")), None, None)
    assert p.tools is None and p.runtime_kwargs() == {} and not p.needs_tool_support and p.offered_names == frozenset()
    assert gr.prepare(msgs(("user", "hi")), [], "auto").tools is None


def test_tools_with_auto_are_offered_and_passed_on():
    p = gr.prepare(msgs(("user", "hi")), [WEATHER], None)
    assert p.offered_names == {"get_weather"} and p.needs_tool_support
    assert p.runtime_kwargs() == {"tools": [WEATHER], "tool_choice": "auto"}


def test_tool_choice_none_hides_the_tools_from_the_model():
    p = gr.prepare(msgs(("user", "hi")), [WEATHER], "none")
    assert p.tools is None and p.runtime_kwargs() == {} and not p.needs_tool_support


def test_tool_messages_alone_still_need_a_template_with_tool_support():
    p = gr.prepare(msgs(("user", "hi"), ("tool", "{}")), None, None)
    assert p.tools is None and p.needs_tool_support and p.runtime_kwargs() == {}

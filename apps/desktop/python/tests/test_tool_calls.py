"""Reading tool calls out of reply text: each format, several calls, bad input, bounds.

The Hermes sample is real output from qwen-2.5-0.5b-instruct-q4 and
qwen-2.5-1.5b-instruct-q4 run here. The Llama, Mistral and Phi samples follow the
model cards and were not produced by a model in these tests.
"""
from __future__ import annotations

import itertools
import json
import random

import pytest

from potluck_local.services import tool_calls as tc

OFFERED = ["get_weather", "get_time"]
HERMES = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>'  # real qwen 2.5 output


def parsed(text: str, offered=OFFERED):
    content, calls = tc.extract_tool_calls(text, offered)
    return content, [(c.name, json.loads(c.arguments)) for c in calls]


# ---- the formats -------------------------------------------------------------


def test_hermes_form_as_the_qwen_models_emit_it():
    assert parsed(HERMES) == ("", [("get_weather", {"city": "Paris"})])


def test_hermes_form_with_text_before_the_call():
    content, calls = parsed("I'll check the weather.\n" + HERMES)
    assert content == "I'll check the weather." and calls == [("get_weather", {"city": "Paris"})]


def test_hermes_form_with_several_calls():
    text = (HERMES + "\n" + '<tool_call>\n{"name": "get_time", "arguments": {"zone": "CET"}}\n</tool_call>')
    assert parsed(text) == ("", [("get_weather", {"city": "Paris"}), ("get_time", {"zone": "CET"})])


def test_hermes_form_without_the_closing_tag_at_the_very_end():
    assert parsed('<tool_call>\n{"name": "get_weather", "arguments": {"city": "Rome"}}')[1] == [("get_weather", {"city": "Rome"})]


def test_hermes_form_with_arguments_given_as_a_json_string_or_left_out():
    assert parsed('<tool_call>{"name": "get_weather", "arguments": "{\\"city\\": \\"Oslo\\"}"}</tool_call>')[1] == [("get_weather", {"city": "Oslo"})]
    assert parsed('<tool_call>{"name": "get_time"}</tool_call>')[1] == [("get_time", {})]
    assert parsed('<tool_call>{"name": "get_time", "arguments": null}</tool_call>')[1] == [("get_time", {})]


def test_llama_json_form_with_and_without_the_python_tag():
    body = '{"name": "get_weather", "parameters": {"city": "Paris"}}'
    assert parsed(body) == ("", [("get_weather", {"city": "Paris"})])
    assert parsed("<|python_tag|>" + body) == ("", [("get_weather", {"city": "Paris"})])
    assert parsed("  \n" + body + "\n") == ("", [("get_weather", {"city": "Paris"})])


def test_llama_32_form_with_a_type_key_and_several_calls_separated_by_semicolons():
    text = ('{"type": "function", "name": "get_weather", "parameters": {"city": "Paris"}}; '
            '{"type": "function", "name": "get_time", "parameters": {}}')
    assert parsed(text) == ("", [("get_weather", {"city": "Paris"}), ("get_time", {})])


def test_mistral_form_with_and_without_the_marker():
    array = '[{"name": "get_weather", "arguments": {"city": "Paris"}, "id": "abc123xyz"}]'
    assert parsed("[TOOL_CALLS] " + array) == ("", [("get_weather", {"city": "Paris"})])
    assert parsed(array) == ("", [("get_weather", {"city": "Paris"})])  # llama-cpp-python does not print the control token
    two = '[{"name": "get_weather", "arguments": {"city": "A"}}, {"name": "get_time", "arguments": {}}]'
    assert [n for n, _ in parsed(two)[1]] == ["get_weather", "get_time"]


def test_phi_form_with_its_closing_token():
    text = '<|tool_call|>[{"name": "get_weather", "arguments": {"city": "Paris"}}]<|/tool_call|>'
    assert parsed(text) == ("", [("get_weather", {"city": "Paris"})])


def test_the_openai_nested_shape_is_read_too():
    text = '{"type": "function", "function": {"name": "get_weather", "arguments": "{\\"city\\": \\"Paris\\"}"}}'
    assert parsed(text) == ("", [("get_weather", {"city": "Paris"})])


def test_trailing_end_tokens_are_ignored():
    assert parsed(HERMES + "<|im_end|>")[1] == [("get_weather", {"city": "Paris"})]
    assert parsed('{"name": "get_time", "parameters": {}}<|eom_id|>')[1] == [("get_time", {})]


def test_arguments_come_back_as_compact_json_text_with_unicode_kept():
    call = tc.extract_tool_calls('<tool_call>{"name": "get_weather", "arguments": {"city": "Zürich", "n": 1}}</tool_call>', OFFERED)[1][0]
    assert call.arguments == '{"city":"Zürich","n":1}'


def test_ids_are_nine_letters_and_digits_and_distinct():
    calls = tc.extract_tool_calls("[" + ",".join(['{"name": "get_time", "arguments": {}}'] * 10) + "]", OFFERED)[1]
    ids = [c.id for c in calls]
    assert len(set(ids)) == 10
    assert all(len(i) == 9 and i.isalnum() for i in ids)


# ---- text that is not a call -------------------------------------------------


@pytest.mark.parametrize("text", [
    "The weather in Paris is sunny.",
    "Use the <tool_call> tag to call a function.",
    '<tool_call>{"name": "get_weather", "arguments": {"city": "Paris"}',  # cut off inside the JSON
    '<tool_call>{"name": "get_weather", "arguments": {"city": }}</tool_call>',
    '<tool_call>not json</tool_call>',
    '{"name": "get_weather", "parameters": {"city": "Paris"}} and then some prose',
    '{"name": "get_weather", "parameters": ["Paris"]}',  # arguments are not an object
    '{"name": "get_weather", "parameters": "Paris"}',
    '{"name": "get_weather", "parameters": {"n": NaN}}',
    '{"name": 5, "parameters": {}}',
    '{"name": "get_weather", "type": "other", "parameters": {}}',
    '[{"name": "get_weather", "parameters": {}}, 5]',
    HERMES + "\nHere you go.",  # text after the calls
    HERMES + "{}",
    "<tool_call>" + HERMES,
    "[TOOL_CALLS]",
    "<|python_tag|>",
    '{"name": "get_weather"',
    "{",
    "[]",
    "{}",
])
def test_not_a_call_stays_plain_content(text):
    content, calls = tc.extract_tool_calls(text, OFFERED)
    assert calls == [] and content == text


def test_a_name_the_request_did_not_offer_is_plain_content():
    text = '<tool_call>{"name": "delete_everything", "arguments": {}}</tool_call>'
    assert tc.extract_tool_calls(text, OFFERED) == (text, [])
    assert tc.extract_tool_calls(text, []) == (text, [])


def test_one_bad_call_among_good_ones_leaves_the_whole_tool_part_as_content():
    text = HERMES + '\n<tool_call>{"name": "launch_missiles", "arguments": {}}</tool_call>'
    assert tc.extract_tool_calls(text, OFFERED) == (text, [])


def test_an_ordinary_json_answer_is_not_mistaken_for_a_call():
    answer = '{"name": "Alice", "age": 30, "tags": ["a", "b"]}'
    assert tc.extract_tool_calls(answer, OFFERED) == (answer, [])
    assert tc.extract_tool_calls('{"city": "Paris"}', OFFERED) == ('{"city": "Paris"}', [])


def test_deeply_nested_json_does_not_raise():
    deep = '{"name": "get_weather", "parameters": ' + "[" * 100_000 + "]" * 100_000 + "}"
    assert tc.extract_tool_calls(deep, OFFERED) == (deep, [])
    assert tc.parse_calls("<tool_call>" + "[" * 5000 + "]" * 5000 + "</tool_call>", OFFERED) is None


# ---- bounds ------------------------------------------------------------------


def test_more_than_the_maximum_number_of_calls_is_plain_content():
    one = '<tool_call>{"name": "get_time", "arguments": {}}</tool_call>'
    ok = one * tc.MAX_TOOL_CALLS
    assert len(tc.extract_tool_calls(ok, OFFERED)[1]) == tc.MAX_TOOL_CALLS
    too_many = one * (tc.MAX_TOOL_CALLS + 1)
    assert tc.extract_tool_calls(too_many, OFFERED) == (too_many, [])


def test_oversized_arguments_make_the_call_plain_content():
    big = '<tool_call>{"name": "get_weather", "arguments": {"city": "%s"}}</tool_call>'
    fits = big % ("x" * (tc.MAX_ARGUMENT_BYTES - 100))
    assert len(tc.extract_tool_calls(fits, OFFERED)[1]) == 1
    too_big = big % ("x" * (tc.MAX_ARGUMENT_BYTES + 100))
    assert tc.extract_tool_calls(too_big, OFFERED) == (too_big, [])


def test_text_held_back_is_bounded_and_then_released_as_content():
    scanner = tc.ToolCallScanner(OFFERED)
    scanner.feed("<tool_call>{\"name\": \"get_weather\", \"arguments\": {\"city\": \"")
    released = ""
    for _ in range(40):
        released += scanner.feed("y" * 20_000)
        assert scanner._size <= tc.MAX_TOOL_TEXT + 20_000  # noqa: SLF001
    released += scanner.finish()
    assert scanner.calls == [] and len(released) > tc.MAX_TOOL_TEXT
    assert released.startswith("<tool_call>") and set(released[-20_000:]) == {"y"}


# ---- streaming ---------------------------------------------------------------


def stream_through(text: str, cuts: list[int], offered=OFFERED):
    scanner = tc.ToolCallScanner(offered)
    out = []
    last = 0
    for cut in [*cuts, len(text)]:
        out.append(scanner.feed(text[last:cut]))
        last = cut
    out.append(scanner.finish())
    return out, scanner.calls


STREAM_TEXTS = [
    HERMES,
    "I'll check.\n" + HERMES,
    HERMES + "\n" + '<tool_call>\n{"name": "get_time", "arguments": {}}\n</tool_call>',
    '<|python_tag|>{"name": "get_weather", "parameters": {"city": "Paris"}}',
    '{"name": "get_weather", "parameters": {"city": "Paris"}}',
    '[{"name": "get_time", "arguments": {}}]',
    "Plain answer with no call at all.",
    "Plain answer ending with a bracket [",
    "Look at <tool_call> in prose",
    "Use the <tool_call> tag. Then <tool_call>{\"name\": \"get_time\", \"arguments\": {}}</tool_call>",
    '{"name": "Alice", "age": 30}',
    '{"name": "get_weather", "parameters": {"city": "Paris"}} trailing prose',
    "  \n  ",
    "",
]


@pytest.mark.parametrize("text", STREAM_TEXTS)
def test_streaming_cut_anywhere_gives_the_same_content_and_calls_as_the_whole_reply(text):
    want_content, want_calls = parsed(text)

    def got(cuts):
        out, calls = stream_through(text, cuts)
        return "".join(out), [(c.name, json.loads(c.arguments)) for c in calls]

    for i in range(len(text) + 1):
        assert got([i]) == (want_content, want_calls), f"cut at {i}"
    for i, j in itertools.combinations(range(len(text) + 1), 2):
        assert got([i, j]) == (want_content, want_calls), f"cuts at {i}, {j}"
    assert got(list(range(1, len(text)))) == (want_content, want_calls)


def test_plain_text_streams_through_as_it_arrives():
    scanner = tc.ToolCallScanner(OFFERED)
    assert scanner.feed("Hello") == "Hello"
    assert scanner.feed(" world") == " world"
    assert scanner.feed(", fine.") == ", fine."
    assert scanner.finish() == ""


def test_ordinary_content_before_a_call_streams_and_the_call_is_held_back():
    scanner = tc.ToolCallScanner(OFFERED)
    assert scanner.feed("Let me look.") == "Let me look."
    assert scanner.feed("\n<tool_call>\n") == ""
    assert scanner.feed('{"name": "get_weather", ') == ""
    assert scanner.feed('"arguments": {"city": "Paris"}}\n</tool_call>') == ""
    assert scanner.finish() == "" and [c.name for c in scanner.calls] == ["get_weather"]


def test_half_a_marker_is_never_released_as_text():
    scanner = tc.ToolCallScanner(OFFERED)
    emitted = scanner.feed("Checking <tool_")
    assert emitted == "Checking" and "<" not in emitted
    assert scanner.feed("call>") == ""  # the whole marker is now held with the call
    # ...and when the half marker was only text, it comes out with what followed
    other = tc.ToolCallScanner(OFFERED)
    assert other.feed("a <tool_") == "a"
    assert other.feed("box is here") == " <tool_box is here"


def test_a_json_looking_start_is_held_until_it_is_clear_it_is_not_a_call():
    scanner = tc.ToolCallScanner(OFFERED)
    assert scanner.feed("{") == ""
    assert scanner.feed('"na') == ""
    assert scanner.feed('me": "get_w') == ""  # still the start of an offered tool name
    held = tc.ToolCallScanner(OFFERED)
    assert held.feed('{"name": "') == ""
    assert held.feed("Al") == '{"name": "Al'  # cannot become an offered name: released at once
    # released as soon as that is clear, not at the end of the reply
    scanner2 = tc.ToolCallScanner(OFFERED)
    assert scanner2.feed('{"colour": ') == '{"colour":'  # trailing space waits for the next piece
    assert scanner2.feed("red}") == " red}"


def test_a_leading_bracket_that_is_prose_is_released_at_once():
    scanner = tc.ToolCallScanner(OFFERED)
    assert scanner.feed("[Verse 1] la la") == "[Verse 1] la la"


def test_whitespace_before_a_plain_reply_is_kept_and_before_a_call_is_dropped():
    assert "".join(stream_through("\n\nHello", [1])[0]) == "\n\nHello"
    assert tc.extract_tool_calls("\n\n" + HERMES, OFFERED)[0] == ""


def test_prose_that_mentions_the_marker_is_released_with_its_whitespace_intact():
    text = "Hello \n<tool_call> is a tag."
    assert "".join(stream_through(text, [7])[0]) == text


def test_once_released_the_scanner_stops_looking_for_calls():
    scanner = tc.ToolCallScanner(OFFERED)
    text = "x <tool_call> is prose. " + HERMES
    out = scanner.feed(text) + scanner.finish()
    assert out == text and scanner.calls == []


def test_random_garbage_in_random_pieces_never_raises_and_keeps_every_character():
    rng = random.Random(11)
    parts = ["<tool_call>", "</tool_call>", "{", "}", "[", "]", '"name"', ":", '"get_time"', ",", "<|python_tag|>",
             "[TOOL_CALLS]", " ", "\n", "x", '"arguments"', '"parameters"', "\\", '"', "<|im_end|>"]
    for _ in range(600):
        text = "".join(rng.choice(parts) for _ in range(rng.randint(0, 16)))
        cuts = sorted(rng.sample(range(len(text) + 1), k=min(len(text) + 1, rng.randint(0, 4))))
        out, calls = stream_through(text, cuts)
        content, whole_calls = tc.extract_tool_calls(text, OFFERED)
        assert "".join(out) == content and len(calls) == len(whole_calls)
        if not calls:
            assert content == text  # no call means the reply comes back whole


# ---- what the model is asked to do -------------------------------------------

WEATHER = {"type": "function", "function": {"name": "get_weather", "description": "Weather",
                                            "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                                           "required": ["city"]}}}
TIME = {"type": "function", "function": {"name": "get_time"}}


def test_completion_kwargs_pass_tools_for_auto_and_nothing_for_none():
    assert tc.completion_kwargs(None, None) == {}
    assert tc.completion_kwargs([WEATHER], "none") == {}
    assert tc.completion_kwargs([WEATHER], "auto") == {"tools": [WEATHER]}
    assert tc.completion_kwargs([WEATHER], None) == {"tools": [WEATHER]}


def test_a_forced_choice_adds_a_grammar_shape_and_never_llama_cpps_own_tool_choice():
    named = tc.completion_kwargs([WEATHER, TIME], {"type": "function", "function": {"name": "get_weather"}})
    assert "tool_choice" not in named
    schema = named["response_format"]["schema"]
    assert named["response_format"]["type"] == "json_object"
    assert schema["properties"]["name"] == {"const": "get_weather"}
    assert schema["properties"]["arguments"] == WEATHER["function"]["parameters"]
    required = tc.completion_kwargs([WEATHER, TIME], "required")["response_format"]["schema"]
    assert [b["properties"]["name"]["const"] for b in required["anyOf"]] == ["get_weather", "get_time"]
    assert required["anyOf"][1]["properties"]["arguments"] == {"type": "object"}  # a tool with no parameters


def test_the_forced_shape_is_what_the_parser_reads():
    out = '{"name": "get_weather", "arguments": {"city": "Paris"}}'
    assert parsed(out) == ("", [("get_weather", {"city": "Paris"})])


def test_check_forced_schema_accepts_a_plain_schema_and_has_nothing_to_check_without_a_force():
    assert tc.check_forced_schema([WEATHER], "auto") is None
    assert tc.check_forced_schema([WEATHER], "required") is None


def test_valid_tool_names():
    assert tc.valid_tool_name("get_weather") and tc.valid_tool_name("a.b-c_1")
    assert not tc.valid_tool_name("") and not tc.valid_tool_name("x" * 65) and not tc.valid_tool_name("a b")
    assert not tc.valid_tool_name(None) and not tc.valid_tool_name(5)


# ---- the text before a call, raw and trimmed ---------------------------------------


def stream_text(text, cuts, *, trim):
    scanner = tc.ToolCallScanner(OFFERED, trim=trim)
    out, last = [], 0
    for cut in [*cuts, len(text)]:
        out.append(scanner.feed(text[last:cut]))
        last = cut
    out.append(scanner.finish())
    return "".join(out), [(c.name, json.loads(c.arguments)) for c in scanner.calls]


def test_a_trimmed_reply_drops_the_whitespace_between_the_text_and_the_call():
    assert tc.extract_tool_calls("I'll look.\n\n" + HERMES, OFFERED)[0] == "I'll look."


def test_a_raw_reply_keeps_the_text_before_the_call_untouched():
    assert tc.extract_tool_calls("I'll look.\n\n" + HERMES, OFFERED, trim=False)[0] == "I'll look.\n\n"
    assert tc.extract_tool_calls("<think>\n\n" + HERMES, OFFERED, trim=False)[0] == "<think>\n\n"


def test_a_raw_reply_that_is_only_whitespace_before_the_call_has_no_content():
    assert tc.extract_tool_calls("\n\n" + HERMES, OFFERED, trim=False)[0] == ""


@pytest.mark.parametrize("text", ["<think>\n\n" + HERMES, "I'll look.\n\n" + HERMES, "\n" + HERMES, "a \n<tool_call> is prose.", "x  "])
@pytest.mark.parametrize("trim", [True, False])
def test_raw_and_trimmed_streams_give_what_the_whole_reply_gives_at_any_cut(text, trim):
    want = parsed(text) if trim else (tc.extract_tool_calls(text, OFFERED, trim=False)[0],
                                      [(c.name, json.loads(c.arguments)) for c in tc.extract_tool_calls(text, OFFERED, trim=False)[1]])
    for i in range(len(text) + 1):
        assert stream_text(text, [i], trim=trim) == want, f"cut at {i}"
    assert stream_text(text, list(range(1, len(text))), trim=trim) == want


def test_a_long_run_of_whitespace_in_a_trimmed_reply_is_released_not_held_for_ever():
    scanner = tc.ToolCallScanner(OFFERED)
    scanner.feed("x")
    released = 0
    for _ in range(50):
        released += len(scanner.feed(" " * 1000))
    assert released > 40_000 and len(scanner._pending) <= tc.MAX_LEAD + 14  # noqa: SLF001


def test_a_long_call_arriving_in_tiny_pieces_is_not_recopied_for_every_piece():
    import time

    big = '<tool_call>{"name": "get_weather", "arguments": {"city": "' + "x" * 100_000 + '"}}</tool_call>'
    scanner = tc.ToolCallScanner(OFFERED)
    started = time.monotonic()
    for ch in big:
        scanner.feed(ch)
    scanner.finish()
    assert len(scanner.calls) == 1 and time.monotonic() - started < 10

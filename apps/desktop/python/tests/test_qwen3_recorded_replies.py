"""What the gateway and the recorded checks make of qwen3-1.7b-q4's own replies.

The strings are exact output of the real model (tests/qwen3_recorded.py): it is a
hybrid thinking model with the Hermes tool template. With thinking off it opens a
<think> block in front of a tool call and never closes it; with thinking on it
closes the block; 17 x 23 takes 594 tokens of reasoning.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import qwen3_recorded as rec
from potluck_local.services import acceptance_gates as g
from potluck_local.services import gateway_reply as gr
from potluck_local.services.gateway_reply import ShapeOptions
from potluck_local.services.model_traits import TemplateTraits
from potluck_local.services.reasoning_split import split_reasoning
from potluck_local.services.tool_calls import CallDetector

OFFERED = frozenset({"get_weather"})
HYBRID = TemplateTraits(supports_tools=True, thinks=True)
CALL_TEXT = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>'


def reply(name):
    return getattr(rec, name)[0]


def completion(text, finish="stop"):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}]}


def shaped(text, finish="stop", **kw):
    choice = gr.shape_completion(completion(text, finish), ShapeOptions(**kw))["choices"][0]
    return choice["message"], choice["finish_reason"]


def pieces_of(text, cuts):
    last, out = 0, []
    for cut in [*cuts, len(text)]:
        out.append(text[last:cut])
        last = cut
    return out


def stream_result(text, cuts, finish="stop", **kw):
    async def source():
        yield {"id": "c", "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
        for piece in pieces_of(text, cuts):
            yield {"id": "c", "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
        yield {"id": "c", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}

    async def collect():
        return [c async for c in gr.shape_stream(source(), ShapeOptions(**kw))]

    reasoning = content = ""
    calls, last_finish = [], None
    for chunk in asyncio.run(collect()):
        delta = chunk["choices"][0]["delta"]
        reasoning += delta.get("reasoning_content") or ""
        content += delta.get("content") or ""
        calls += delta.get("tool_calls") or []
        last_finish = chunk["choices"][0]["finish_reason"] or last_finish
    return reasoning, content, [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls], last_finish


# ---- thinking off, tools offered: the block is opened and never closed ---------------


def test_the_recorded_shape_is_what_the_report_says():
    assert reply("TOOLS_NO_THINK") == "<think>\n\n" + CALL_TEXT
    assert rec.TOOLS_NO_THINK[1:] == ("stop", 21)


def test_a_call_in_an_unclosed_block_is_found_when_tools_were_offered():
    text = reply("TOOLS_NO_THINK")
    assert split_reasoning(text, calls=CallDetector(OFFERED)) == ("", CALL_TEXT)
    message, finish = shaped(text, offered=OFFERED)
    assert finish == "tool_calls" and message["content"] is None and "reasoning_content" not in message
    (call,) = message["tool_calls"]
    assert call["function"]["name"] == "get_weather" and json.loads(call["function"]["arguments"]) == {"city": "Paris"}


def test_the_same_call_is_found_in_a_stream_cut_at_every_position():
    text = reply("TOOLS_NO_THINK")
    want = ("", "", [("get_weather", {"city": "Paris"})], "tool_calls")
    for i in range(len(text) + 1):
        assert stream_result(text, [i], offered=OFFERED) == want, f"cut at {i}"
    assert stream_result(text, list(range(1, len(text))), offered=OFFERED) == want


def test_without_tools_in_the_request_a_block_that_never_closes_is_still_all_reasoning():
    message, finish = shaped(reply("TOOLS_NO_THINK"))
    assert message["reasoning_content"] == CALL_TEXT and message["content"] == "" and finish == "stop"
    assert stream_result(reply("TOOLS_NO_THINK"), [5, 12]) == (CALL_TEXT, "", [], "stop")


def test_a_raw_reply_keeps_the_text_before_the_call_untouched():
    """reasoning_format "none": the client asked for the model's text as it is, so the
    text before the call is `<think>` and the blank line the model wrote."""
    text = reply("TOOLS_NO_THINK")
    message, finish = shaped(text, split_reasoning=False, offered=OFFERED)
    assert message["content"] == "<think>\n\n" and finish == "tool_calls" and len(message["tool_calls"]) == 1
    assert "reasoning_content" not in message
    for i in range(len(text) + 1):
        assert stream_result(text, [i], split_reasoning=False, offered=OFFERED) == ("", "<think>\n\n", [("get_weather", {"city": "Paris"})], "tool_calls")


# ---- thinking on, tools offered ------------------------------------------------------


def test_a_closed_block_in_front_of_a_call_gives_reasoning_and_the_call():
    text = reply("TOOLS_THINKING")
    message, finish = shaped(text, offered=OFFERED)
    assert message["reasoning_content"].startswith("Okay, the user is asking for the current weather in Paris.")
    assert message["reasoning_content"].endswith("with Paris as the argument.")
    assert message["content"] is None and finish == "tool_calls" and message["tool_calls"][0]["function"]["name"] == "get_weather"
    got = stream_result(text, list(range(7, len(text), 7)), offered=OFFERED)
    assert got[0] == message["reasoning_content"] and got[1] == "" and got[2] == [("get_weather", {"city": "Paris"})]


# ---- the second turn, with the tool result ---------------------------------------------


def test_second_turn_with_thinking_off_is_an_empty_block_and_the_answer():
    message, finish = shaped(reply("SECOND_NO_THINK"), offered=OFFERED)
    assert message == {"role": "assistant", "content": "The current weather in Paris is 17°C with overcast skies."}
    assert finish == "stop"


def test_second_turn_with_thinking_on_has_the_reasoning_apart_from_the_reply():
    message, finish = shaped(reply("SECOND_THINKING"), offered=OFFERED)
    assert message["content"].startswith("The current weather in Paris is 17°C with overcast skies.")
    assert message["content"].endswith("🌧️") and "<think" not in message["content"] and "17" in message["content"]
    assert message["reasoning_content"].startswith("Okay, the user asked for the weather in Paris.") and finish == "stop"
    got = stream_result(reply("SECOND_THINKING"), list(range(5, 800, 5)), offered=OFFERED)
    assert got == (message["reasoning_content"], message["content"], [], "stop")


# ---- 17 x 23 -----------------------------------------------------------------------------


def test_512_tokens_end_inside_the_block_so_everything_is_reasoning_and_the_finish_reason_is_length():
    message, finish = shaped(reply("SUM_512"), finish="length")
    assert message["content"] == "" and message["reasoning_content"].startswith("Okay, let's see.") and finish == "length"
    assert not g.judge_reasoning(reply("SUM_512"), HYBRID)


def test_2048_tokens_finish_with_391_and_the_reasoning_apart():
    message, finish = shaped(reply("SUM_2048"))
    assert message["content"] == "391" and "391" in message["reasoning_content"] and finish == "stop"
    assert g.judge_reasoning(reply("SUM_2048"), HYBRID)
    assert stream_result(reply("SUM_2048"), list(range(9, 1500, 9))) == (message["reasoning_content"], "391", [], "stop")


def test_no_think_gives_an_empty_block_and_391():
    message, _ = shaped(reply("SUM_NO_THINK"))
    assert message == {"role": "assistant", "content": "391"}
    assert not g.judge_reasoning(reply("SUM_NO_THINK"), HYBRID)  # no reasoning came back: not a pass, and not a budget problem


@pytest.mark.parametrize("name,finish,why", [
    ("SUM_512", "length", "the reasoning did not finish in 512 tokens"),
    ("SUM_2048", "stop", None),
    ("SUM_NO_THINK", "stop", None),
])
def test_only_a_reply_cut_off_at_the_budget_counts_as_out_of_budget(name, finish, why):
    assert g.out_of_budget("reasoning", reply(name), "", finish, HYBRID) == why

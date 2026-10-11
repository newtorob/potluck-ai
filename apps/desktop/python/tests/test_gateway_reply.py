"""The shaping layer on its own: odd and hostile chunks, early ends, cancellation."""
from __future__ import annotations

import asyncio
import json

import pytest

from potluck_local.services import gateway_reply as gr
from potluck_local.services.gateway_reply import ShapeOptions

TOOLS = frozenset({"get_weather"})
HERMES = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>'


def run(coro):
    return asyncio.run(coro)


async def collect(source, options):
    return [c async for c in gr.shape_stream(source, options)]


def agen(items):
    async def gen():
        for item in items:
            yield item
    return gen()


def chunk(delta, finish=None, **extra):
    return {"id": "c", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}


# ---- whole replies --------------------------------------------------------------


@pytest.mark.parametrize("result", [
    None, 5, "text", [], {}, {"choices": None}, {"choices": []}, {"choices": [None]}, {"choices": [{}]},
    {"choices": [{"message": None}]}, {"choices": [{"message": {"content": None}}]},
    {"choices": [{"message": {"content": ["parts"]}}]}, {"choices": [{"message": {"content": 5}}]},
])
def test_odd_results_come_back_untouched_and_nothing_raises(result):
    before = json.dumps(result)
    out = gr.shape_completion(result, ShapeOptions(offered=TOOLS))
    assert json.dumps(out) == before


def test_shaping_that_fails_leaves_the_reply_as_it_was(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(gr, "split_reasoning", boom)
    result = {"choices": [{"message": {"role": "assistant", "content": "<think>x</think>y"}, "finish_reason": "stop"}]}
    assert gr.shape_completion(result, ShapeOptions())["choices"][0]["message"]["content"] == "<think>x</think>y"


def test_with_nothing_to_do_the_same_object_comes_back():
    result = {"choices": [{"message": {"content": "<think>x</think>y"}}]}
    assert gr.shape_completion(result, ShapeOptions(split_reasoning=False)) is result
    assert result["choices"][0]["message"]["content"] == "<think>x</think>y"


def test_content_is_null_only_when_there_are_calls_and_no_text():
    def shaped(text, **kw):
        result = {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}
        return gr.shape_completion(result, ShapeOptions(offered=TOOLS, **kw))["choices"][0]

    assert shaped(HERMES)["message"]["content"] is None
    assert shaped("")["message"]["content"] == ""
    assert shaped("<think>only thoughts")["message"]["content"] == ""
    assert shaped("hello")["message"]["content"] == "hello"


def test_a_finish_reason_other_than_stop_is_kept_with_calls():
    result = {"choices": [{"message": {"content": HERMES}, "finish_reason": "length"}]}
    out = gr.shape_completion(result, ShapeOptions(offered=TOOLS))["choices"][0]
    assert out["finish_reason"] == "length" and out["message"]["tool_calls"]


# ---- streams ---------------------------------------------------------------------


def test_chunks_that_need_no_change_are_the_same_objects():
    items = [chunk({"role": "assistant"}), chunk({"content": "Hello"}), chunk({"content": " there"}), chunk({}, "stop")]
    out = run(collect(agen(items), ShapeOptions(offered=TOOLS)))
    assert [id(c) for c in out] == [id(c) for c in items]


def test_content_and_finish_in_one_chunk_is_split_cleanly():
    items = [chunk({"content": "Hi <think>no"}, "stop", usage={"total_tokens": 3})]
    out = run(collect(agen(items), ShapeOptions()))
    assert out == items  # unchanged: a literal tag late in a reply is text
    items = [chunk({"role": "assistant", "content": "<think>a</think>b"}, "stop", usage={"total_tokens": 3})]
    out = run(collect(agen(items), ShapeOptions()))
    assert [c["choices"][0]["delta"] for c in out] == [{"role": "assistant", "reasoning_content": "a"}, {"content": "b"}, {}]
    assert out[-1]["choices"][0]["finish_reason"] == "stop" and out[-1]["usage"] == {"total_tokens": 3}
    assert "usage" not in out[0] and "usage" not in out[1]


def test_a_role_that_arrives_with_held_back_text_is_not_lost():
    out = run(collect(agen([chunk({"role": "assistant", "content": "<thi"}), chunk({"content": "nk>x</think>y"}), chunk({}, "stop")]),
                      ShapeOptions()))
    assert out[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert [c["choices"][0]["delta"] for c in out[1:]] == [{"reasoning_content": "x"}, {"content": "y"}, {}]


def test_a_source_that_ends_without_a_finish_chunk_still_releases_what_was_held():
    out = run(collect(agen([chunk({"content": "Look: "}), chunk({"content": "<tool_"})]), ShapeOptions(offered=TOOLS)))
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in out)
    assert text == "Look: <tool_"


def test_a_source_that_ends_in_the_middle_of_a_call_releases_it_as_text():
    out = run(collect(agen([chunk({"content": "<tool_call>{\"name\": \"get_wea"})]), ShapeOptions(offered=TOOLS)))
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in out) == "<tool_call>{\"name\": \"get_wea"


@pytest.mark.parametrize("odd", [
    5, "text", None, [], {}, {"choices": None}, {"choices": []}, {"choices": [None]}, {"choices": [5]},
    {"choices": [{"delta": None}]}, {"choices": [{"delta": {"content": None}}]}, {"choices": [{"delta": {"content": 5}}]},
    {"choices": [{"delta": {"content": ["x"]}}]}, {"usage": {"total_tokens": 1}},
    {"__error__": "device lost", "__category__": "inference_error"}, {"error": {"message": "x"}},
])
def test_odd_chunks_pass_through_and_nothing_raises(odd):
    items = [chunk({"content": "a"}), odd, chunk({"content": "b"}), chunk({}, "stop")]
    out = run(collect(agen(items), ShapeOptions(offered=TOOLS)))
    assert odd in out or odd is None and None in out
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in out if isinstance(c, dict) and c.get("choices")
                   and isinstance(c["choices"], list) and isinstance(c["choices"][0], dict)
                   and isinstance(c["choices"][0].get("delta"), dict) and isinstance(c["choices"][0]["delta"].get("content"), str))
    assert text.endswith("ab") or text == "ab"


def test_chunks_after_the_finish_chunk_pass_through_untouched():
    items = [chunk({"content": "x"}), chunk({}, "stop"), chunk({"content": "<think>late</think>"}), {"usage": {"total_tokens": 2}}]
    out = run(collect(agen(items), ShapeOptions()))
    assert out[2] is items[2] and out[3] is items[3]


def test_a_shaping_bug_mid_stream_falls_back_to_the_raw_chunks(monkeypatch):
    calls = {"n": 0}
    real = gr._Pipeline.feed

    def flaky(self, text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("bug")
        return real(self, text)

    monkeypatch.setattr(gr._Pipeline, "feed", flaky)
    items = [chunk({"content": "a"}), chunk({"content": "b"}), chunk({"content": "c"}), chunk({}, "stop")]
    out = run(collect(agen(items), ShapeOptions()))
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in out) == "abc"


def test_closing_the_shaped_stream_closes_the_source():
    closed = []

    async def source():
        try:
            for i in range(100):
                yield chunk({"content": f"{i} "})
        finally:
            closed.append(True)

    async def go():
        stream = gr.shape_stream(source(), ShapeOptions(offered=TOOLS))
        async for _ in stream:
            break
        await stream.aclose()

    run(go())
    assert closed == [True]


def test_an_error_from_the_source_reaches_the_caller():
    async def source():
        yield chunk({"content": "a"})
        raise ConnectionError("peer gone")

    with pytest.raises(ConnectionError):
        run(collect(source(), ShapeOptions()))


def test_a_hostile_stream_of_tiny_chunks_is_shaped_without_blowing_up():
    pieces = ["<", "t", "h", "i", "n", "k", ">"] * 3000 + list("</think>") + list(HERMES) * 3
    items = [chunk({"content": p}) for p in pieces] + [chunk({}, "stop")]
    out = run(collect(agen(items), ShapeOptions(offered=TOOLS)))
    assert out[-1]["choices"][0]["finish_reason"] in ("stop", "tool_calls")

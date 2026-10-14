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


# ---- the model a reply names ----------------------------------------------------

MODEL_PATH = "/Users/someone/.potluck/models/x/model.gguf"


def test_a_whole_reply_names_the_model_id_and_not_the_file_path():
    result = {"model": MODEL_PATH, "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
    for options in (ShapeOptions(model="x"), ShapeOptions(model="x", split_reasoning=False)):  # shaped, and not shaped
        assert gr.shape_completion(dict(result), options)["model"] == "x"


def test_a_whole_reply_without_a_model_gets_one():
    assert gr.shape_completion({"choices": []}, ShapeOptions(model="x"))["model"] == "x"


def test_without_a_model_to_name_the_engines_own_stays():
    result = {"model": MODEL_PATH, "choices": []}
    assert gr.shape_completion(result, ShapeOptions())["model"] == MODEL_PATH


@pytest.mark.parametrize("options", [
    ShapeOptions(model="x"), ShapeOptions(model="x", split_reasoning=False), ShapeOptions(model="x", offered=TOOLS),
])
def test_every_streamed_chunk_names_the_model_id_whatever_the_shaping(options):
    items = [chunk({"role": "assistant"}, model=MODEL_PATH), chunk({"content": "<think>a</think>"}, model=MODEL_PATH),
             chunk({"content": HERMES}, model=MODEL_PATH), chunk({}, "stop", model=MODEL_PATH, usage={"total_tokens": 3})]
    out = run(collect(agen(items), options))
    assert out and all(c["model"] == "x" for c in out)
    assert out[-1]["usage"] == {"total_tokens": 3}


def test_a_chunk_that_already_names_the_model_is_passed_on_as_the_same_object():
    items = [chunk({"content": "a"}, model="x"), chunk({}, "stop", model="x")]
    out = run(collect(agen(items), ShapeOptions(model="x", split_reasoning=False)))
    assert out[0] is items[0] and out[1] is items[1]


def test_the_chunk_the_source_made_is_not_changed_when_it_is_renamed():
    items = [chunk({"content": "a"}, model=MODEL_PATH)]
    run(collect(agen(items), ShapeOptions(model="x", split_reasoning=False)))
    assert items[0]["model"] == MODEL_PATH


def test_an_error_chunk_and_a_chunk_without_choices_are_not_renamed():
    items = [{"__error__": "boom", "__category__": "inference_error"}, {"usage": {"total_tokens": 2}}]
    assert run(collect(agen(items), ShapeOptions(model="x", split_reasoning=False))) == items


# ---- token counts of a whole reply ----------------------------------------------


def reply(text="hello from peer", usage=None, **message):
    out = {"choices": [{"message": {"role": "assistant", "content": text, **message}, "finish_reason": "stop"}]}
    if usage is not False:
        out["usage"] = usage if usage is not None else {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    return out


def test_counts_the_engine_did_not_give_are_estimated_from_text_length_rounded_up():
    result = reply("hello from peer")  # 15 characters
    assert gr.fill_usage(result, "abcdefghij") is True  # 10 characters
    assert result["usage"] == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}


def test_a_reply_with_no_usage_at_all_is_filled_in():
    result = reply("abcd", usage=False)
    assert gr.fill_usage(result, "abcde") is True
    assert result["usage"] == {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}


def test_counts_the_engine_gave_stay_and_nothing_says_estimated():
    result = reply(usage={"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12, "extra": 1})
    before = json.dumps(result)
    assert gr.fill_usage(result, "a prompt that is not counted") is False
    assert json.dumps(result) == before


def test_a_total_the_engine_left_out_is_the_sum_and_is_not_an_estimate():
    result = reply(usage={"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": None})
    assert gr.fill_usage(result, "x") is False
    assert result["usage"] == {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}


def test_one_count_given_and_one_not_keeps_the_given_one_and_says_estimated():
    result = reply("abcdefgh", usage={"prompt_tokens": None, "completion_tokens": 3, "total_tokens": None})
    assert gr.fill_usage(result, "abcd") is True
    assert result["usage"] == {"prompt_tokens": 1, "completion_tokens": 3, "total_tokens": 4}
    result = reply("abcdefgh", usage={"prompt_tokens": 9, "completion_tokens": None, "total_tokens": 99})
    assert gr.fill_usage(result, "abcd") is True
    assert result["usage"] == {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}


@pytest.mark.parametrize("bad", [-1, True, 2.5, "3", [], {}])
def test_a_figure_that_is_not_a_whole_number_is_no_count(bad):
    result = reply("abcd", usage={"prompt_tokens": bad, "completion_tokens": bad, "total_tokens": bad})
    assert gr.fill_usage(result, "abcd") is True
    assert result["usage"] == {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def test_the_reply_estimate_counts_reasoning_and_the_name_and_arguments_of_calls():
    calls = [{"id": "c", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}]
    result = reply("", reasoning_content="abcd", tool_calls=calls)
    gr.fill_usage(result, "")
    assert result["usage"]["completion_tokens"] == -(-(4 + len("get_weather") + len('{"city": "Paris"}')) // 4)


@pytest.mark.parametrize("result", [None, 5, "text", [], {}, {"choices": None}, {"choices": [None]}, {"choices": [{"message": None}]}])
def test_an_odd_result_never_raises(result):
    gr.fill_usage(result, "abcd")
    if isinstance(result, dict):
        assert result["usage"]["prompt_tokens"] == 1 and result["usage"]["completion_tokens"] == 0

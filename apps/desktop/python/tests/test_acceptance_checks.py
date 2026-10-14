"""The answer checks themselves: what they ask, what they accept, what they refuse.

A fake model stands in for llama.cpp, so these tests say nothing about a real
model. The real answers, and the numbers behind the repeats threshold, are in
the calibration notes of the change that added acceptance.py.
"""
from __future__ import annotations

import re

import pytest

from potluck_local.services import acceptance as a

SENTENCES = (
    "The ocean is a vast and mysterious body of water that covers over 70% of the Earth's surface. "
    "It is home to diverse marine life and plays a crucial role in regulating the Earth's climate."
)


def doubled(text: str) -> str:
    return " ".join(word for word in text.split() for _ in (0, 1))


def stream_of(text: str, finish: str):
    """The reply as llama-cpp-python streams it: a role chunk, one chunk per word, then the finish chunk."""
    yield {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]}
    for piece in re.findall(r"\S+\s*", text):
        yield {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
    yield {"choices": [{"delta": {}, "finish_reason": finish}]}


class FakeModel:
    """Answers the suite's prompts. `mode` picks how it misbehaves, if it does."""

    def __init__(self, mode: str = "good", *, template: str = "") -> None:
        self.mode = mode
        self.metadata = {"tokenizer.chat_template": template} if template else {}
        self.asked: list[dict] = []
        self.resets = 0
        self._vocab: list[str] = []

    def good_answer(self, prompt: str) -> str:
        if "58213" in prompt:
            return "58213"
        if "2 + 2" in prompt:
            return "4"
        if "France" in prompt:
            return "Paris"
        if "vault code" in prompt:
            return "73941"
        return SENTENCES

    def create_chat_completion(self, *, messages, temperature, max_tokens, stream):
        prompt = messages[0]["content"]
        self.asked.append({"prompt": prompt, "temperature": temperature, "max_tokens": max_tokens, "stream": stream})
        text, finish = self.answer(prompt, max_tokens)
        if stream:  # the checks that run after a load read the reply from a stream
            return stream_of(text, finish)
        return {"choices": [{"message": {"content": text}, "finish_reason": finish}],
                "usage": {"completion_tokens": max(1, len(text.split()))}}

    def answer(self, prompt: str, max_tokens: int) -> tuple[str, str]:
        text = self.good_answer(prompt)
        if self.mode == "empty":
            return "", "stop"
        if self.mode == "doubled":
            return doubled(text), "stop"
        if self.mode == "wrong":
            return "I am not sure about that.", "stop"
        if self.mode == "rambles":
            return text, "length"
        if self.mode == "empty_think":
            # What qwen3-8b sent in our runs with /no_think: an empty block, then the answer.
            return f"<think>\n\n</think>\n\n{text}", "stop"
        if self.mode == "thinks":
            # Reasons first. Too small a budget ends inside the reasoning.
            if max_tokens < 100:
                return "<think>\nOkay, the user wants me to", "length"
            return f"<think>\nOkay, so the answer is simple.\n</think>\n\n{text}", "stop"
        return text, "stop"

    # The fake tokenizer: one token per word, the leading space kept like a real one.
    def tokenize(self, data: bytes, add_bos: bool = True, special: bool = False):
        ids = []
        for piece in re.findall(r" ?\S+", data.decode("utf-8")):
            if piece not in self._vocab:
                self._vocab.append(piece)
            ids.append(self._vocab.index(piece))
        return ids

    def detokenize(self, ids):
        return b"".join(self._vocab[i].encode("utf-8") for i in ids)

    def reset(self) -> None:
        self.resets += 1


def run(model: FakeModel, level: str = "quick", *, n_ctx: int | None = 4096, on_cpu: bool = False) -> a.CheckRun:
    return a.run_checks(model, level, n_ctx=n_ctx, on_cpu=on_cpu)


def results(r: a.CheckRun) -> dict[str, bool]:
    return {o.id: o.passed for o in r.outcomes}


# ---- the suite ---------------------------------------------------------------


def test_right_answers_pass_every_check_and_only_three_are_required():
    r = run(FakeModel())
    assert r.passed
    assert [(o.id, o.required, o.passed) for o in r.outcomes] == [
        ("copy", True, True), ("arithmetic", True, True), ("repeats", True, True),
        ("fact", False, True), ("stops", False, True), ("long_prompt", False, True),
        ("reasoning", False, True), ("tools", False, True),  # skipped for this fake: no thinking, no tool template
    ]
    assert r.api()[0] == {"id": "copy", "passed": True, "required": True}


def test_every_prompt_is_greedy_and_short_and_goes_through_chat_completion():
    model = FakeModel()
    run(model)
    assert model.asked and all(q["temperature"] == 0.0 and q["stream"] is False for q in model.asked)
    assert [q["max_tokens"] for q in model.asked[:4]] == [24, 24, 48, 24]
    assert "58213" in model.asked[0]["prompt"] and "2 + 2" in model.asked[1]["prompt"]


def test_the_fake_model_is_left_with_no_checked_prompt_cached():
    model = FakeModel()
    run(model)
    assert model.resets == 1


def test_doubled_tokens_fail_repeats_and_the_run():
    """The incident: every reply came back with each token twice. The short
    answers still contain the code and the 4, so repeats is what catches it."""
    r = run(FakeModel("doubled"))
    assert results(r)["repeats"] is False
    assert results(r)["copy"] is True and results(r)["arithmetic"] is True
    assert r.passed is False


def test_empty_output_fails_every_required_check():
    r = run(FakeModel("empty"))
    assert r.passed is False
    assert {o.id: o.passed for o in r.outcomes if o.required} == {"copy": False, "arithmetic": False, "repeats": False}
    assert results(r)["stops"] is False


def test_a_weak_model_that_answers_wrongly_fails():
    r = run(FakeModel("wrong"))
    assert r.passed is False
    assert results(r)["copy"] is False and results(r)["arithmetic"] is False


def test_recorded_checks_do_not_decide_the_result():
    model = FakeModel()
    original = model.good_answer
    model.good_answer = lambda p: "Lyon" if "France" in p else ("no idea" if "vault" in p else original(p))
    r = run(model)
    assert results(r)["fact"] is False and results(r)["long_prompt"] is False
    assert r.passed is True


def test_an_answer_that_never_ends_fails_stops_only():
    r = run(FakeModel("rambles"))
    assert results(r)["stops"] is False and r.passed is True


def test_a_model_that_throws_has_failed_the_check_and_the_run_does_not_raise():
    class Throws(FakeModel):
        def create_chat_completion(self, **kw):
            raise RuntimeError("device lost")

    r = run(Throws())
    assert r.passed is False
    assert all(o.error == "RuntimeError" for o in r.outcomes if o.id != "stops" and not o.skipped)


@pytest.mark.parametrize("answer,expected", [
    ("4", True), ("2 + 2 = 4", True), ("The answer is 4.", True), ("4.", True),
    ("44", False), ("14", False), ("4.5", False), ("2,4", False), ("2.4", False), ("four", False), ("", False),
])
def test_arithmetic_wants_a_standalone_four(answer, expected):
    assert a._standalone_four(answer) is expected


def test_the_suite_version_is_part_of_every_key(monkeypatch):
    base = dict(sha256="a" * 64, backend="vulkan", device="GPU", device_kind="gpu", n_gpu_layers=-1,
                total_layers=32, cpu_only=False, driver="1.0", library="sig", level="quick")
    before = a.build_key(**base)
    monkeypatch.setattr(a, "SUITE_VERSION", a.SUITE_VERSION + 1)
    after = a.build_key(**base)
    assert before["suite"] != after["suite"] and a.key_id(before) != a.key_id(after)


# ---- thinking models ------------------------------------------------------------


@pytest.mark.parametrize("raw,visible,cut", [
    ("58213", "58213", False),
    ("<think>hmm</think>58213", "58213", False),
    ("<think>\nhmm\n</think>\n\n58213\n", "58213", False),
    ("hmm, the user wants a number.\n</think>\n\n58213", "58213", False),  # the template opened the block
    ("<think>hmm, and then", "", True),
    ("<think>a</think>first <think>b</think>second", "first second", False),
])
def test_reasoning_is_removed_before_judging(raw, visible, cut):
    assert a.visible_text(raw) == (visible, cut)


def test_a_thinking_model_that_runs_out_of_room_is_asked_again_with_more():
    model = FakeModel("thinks")
    r = run(model)
    assert r.passed
    copy_calls = [q for q in model.asked if "58213" in q["prompt"]]
    assert [q["max_tokens"] for q in copy_calls] == [24, a.THINK_RETRY_TOKENS]


def test_the_no_think_switch_is_added_only_where_the_template_has_one():
    plain = FakeModel()
    run(plain)
    assert not any("/no_think" in q["prompt"] for q in plain.asked)
    qwen3 = FakeModel(template="{%- if enable_thinking is defined and enable_thinking is false %}")
    run(qwen3)
    # The reasoning check is the one prompt asked with thinking on.
    assert all(q["prompt"].endswith(" /no_think") for q in qwen3.asked if "17 * 23" not in q["prompt"])
    assert any("17 * 23" in q["prompt"] and "/no_think" not in q["prompt"] for q in qwen3.asked)


# The reply shape qwen3-8b-q4 produced with /no_think in our runs.
EMPTY_THINK_ANSWER = ("<think>\n\n</think>\n\nA mesh network is a network where every node relays data for the others. "
                      "It keeps working when one link or machine fails.")


def test_an_empty_think_block_before_the_answer_is_not_a_repeat_and_not_length():
    assert a.visible_text(EMPTY_THINK_ANSWER) == (EMPTY_THINK_ANSWER.split("</think>\n\n", 1)[1], False)
    text = a.without_think_tags(EMPTY_THINK_ANSWER)
    assert "think" not in text.lower() and "<" not in text
    assert a.is_degenerate(EMPTY_THINK_ANSWER) is False  # the raw reply, markers and newlines included
    assert a.is_degenerate(text) is False
    # Even the markers alone make no pair: "think" ... "think" would be one adjacent repeat if they were words.
    assert a.adjacent_repeats(a._words(text)) == 0


def test_repeats_passes_a_reply_that_starts_with_an_empty_think_block():
    model = FakeModel("empty_think")
    model.good_answer = lambda p: EMPTY_THINK_ANSWER.split("</think>\n\n", 1)[1] if "two short sentences" in p else FakeModel.good_answer(model, p)
    r = run(model)
    assert results(r) == {"copy": True, "arithmetic": True, "repeats": True, "fact": True, "stops": True,
                          "long_prompt": True, "reasoning": True, "tools": True}
    assert r.passed


def test_the_empty_block_does_not_make_a_too_short_answer_long_enough():
    model = FakeModel("empty_think")
    model.good_answer = lambda p: "Waves crash." if "two short sentences" in p else FakeModel.good_answer(model, p)
    # With the markers the reply is 32 characters; the answer alone is 12.
    assert len(f"<think>\n\n</think>\n\nWaves crash.") >= a.MIN_CHARS > len("Waves crash.")
    assert results(run(model))["repeats"] is False


def test_a_doubled_answer_behind_an_empty_think_block_still_fails():
    model = FakeModel("empty_think")
    model.good_answer = lambda p: doubled(SENTENCES) if "two short sentences" in p else FakeModel.good_answer(model, p)
    assert results(run(model))["repeats"] is False


def test_a_whole_run_of_replies_in_that_shape_passes():
    r = run(FakeModel("empty_think"))
    assert r.passed and all(o.passed for o in r.outcomes)
    assert [o.excerpt for o in r.outcomes[:2]] == ["58213", "4"]


# ---- the repeats rule ------------------------------------------------------------


NATURAL = {
    "the the once": "I think the the old harbor town is lovely in spring, with its blue boats and the bakery that opens early.",
    "1, 1 in a list": "The first numbers are 1, 1, 2, 3, 5, 8, 13 and 21, and each one is the sum of the two before it.",
    "ha ha": "He laughed, ha ha, and then told the story again, slowly, so that everyone at the table could follow it.",
    "bye bye": "She waved and said bye bye to the children, then walked back along the canal to the old library.",
    "that that": "I know that that plan will work, because the council already agreed to the harbor budget last week.",
    "no no no": "The answer was no, no, no, and he said so three times before anyone would listen to the reasons.",
    "a real answer": SENTENCES,
}


@pytest.mark.parametrize("name", NATURAL)
def test_ordinary_repeats_are_not_degenerate(name):
    assert a.is_degenerate(NATURAL[name]) is False


def test_the_threshold_sits_between_ordinary_text_and_a_doubling_fault():
    """Ordinary text has at most two adjacent repeats in 20 words. A model that
    doubles every token has about one pair per word, and one that doubles every
    second word about one in three."""
    worst_natural = max(a.adjacent_repeats(a._words(t)) for t in NATURAL.values())
    assert worst_natural <= 2 < a.MIN_REPEATS
    every_second = " ".join(w for i, w in enumerate(SENTENCES.split()) for _ in range(2 if i % 2 == 0 else 1))
    assert a.adjacent_repeats(a._words(every_second)) >= 3 * a.MIN_REPEATS
    assert a.adjacent_repeats(a._words(every_second)) / (len(every_second.split()) - 1) > 2 * a.REPEAT_RATIO


@pytest.mark.parametrize("text", [
    doubled(SENTENCES),
    "A A mesh mesh network network connects connects every every machine machine here here today today",
    "The ocean is is is is is is is is is is vast.",
])
def test_doubled_words_are_degenerate(text):
    assert a.is_degenerate(text) is True


def test_a_doubling_with_no_space_between_the_copies_is_caught_by_the_token_pieces():
    words = "The ocean is vast and deep and it covers most of the Earth and holds most of its water".split()
    glued = " ".join(w + w for w in words)  # "TheThe oceanocean isis ..."
    pieces = [piece for w in words for piece in ((w if i == 0 else " " + w) for i in (0, 1))]
    assert a.is_degenerate(glued) is False  # no two words are equal
    assert a.is_degenerate(glued, pieces) is True


def test_numbers_and_punctuation_never_count_as_a_repeat():
    assert a.is_degenerate("1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1") is False
    assert a.is_degenerate("-- -- -- -- -- -- -- -- -- -- -- --") is False
    assert a.is_degenerate("ok 1 ok 1 ok 1 ok 1 ok 1 ok 1") is False


def test_a_phrase_repeated_between_commas_is_a_weak_model_not_a_broken_device():
    assert a.is_degenerate("It is vast，孕育着无数生命，孕育着无数生命，孕育着无数生命，孕育着无数生命，") is False
    assert a.is_degenerate("The ocean is vast, the ocean is vast, the ocean is vast, the ocean is vast.") is False


def test_a_reply_too_short_to_be_two_sentences_fails_repeats():
    model = FakeModel()
    original = model.good_answer
    model.good_answer = lambda p: "Waves." if "two short sentences" in p else original(p)
    assert results(run(model))["repeats"] is False


# ---- the long prompt ----------------------------------------------------------------


def test_quick_skips_the_long_prompt_on_a_cpu_only_load():
    assert "long_prompt" not in results(run(FakeModel(), on_cpu=True))


def test_quick_long_prompt_is_about_six_hundred_tokens_with_the_code_planted_inside():
    model = FakeModel()
    run(model)
    prompt = model.asked[-1]["prompt"]
    tokens = len(model.tokenize(prompt.encode()))
    assert 0.8 * a.QUICK_PROMPT_TOKENS <= tokens <= 1.4 * a.QUICK_PROMPT_TOKENS
    assert prompt.count("73941") == 1 and prompt.endswith("Reply with only the number.")
    body = prompt.split("\n\nWhat is the vault code?")[0]
    assert 0.2 < body.index("73941") / len(body) < 0.6


def test_full_fills_most_of_the_context_even_on_a_cpu_only_load():
    model = FakeModel()
    r = run(model, "full", n_ctx=8192, on_cpu=True)
    assert "long_prompt" in results(r)
    tokens = len(model.tokenize(model.asked[-1]["prompt"].encode()))
    assert 0.7 * 8192 <= tokens <= 0.95 * 8192


def test_full_is_capped_so_a_huge_context_is_not_filled():
    model = FakeModel()
    run(model, "full", n_ctx=262144)
    tokens = len(model.tokenize(model.asked[-1]["prompt"].encode()))
    assert tokens <= 1.2 * a.FULL_PROMPT_CAP


def test_no_long_prompt_when_the_context_cannot_hold_it():
    assert "long_prompt" not in results(run(FakeModel(), n_ctx=512))


# ---- model output is data ---------------------------------------------------------------


def test_excerpts_kept_for_the_owner_are_short_single_lines():
    model = FakeModel()
    model.good_answer = lambda p: "line one\n\nline two " + "x" * 5000
    r = run(model)
    for outcome in r.outcomes:
        assert len(outcome.excerpt) <= 80 and "\n" not in outcome.excerpt
    assert max(len(str(o.record())) for o in r.outcomes) < 400


def test_the_contract_view_of_a_run_carries_no_model_text():
    model = FakeModel()
    model.good_answer = lambda p: "SECRET-MODEL-TEXT " + "x " * 30
    assert "SECRET-MODEL-TEXT" not in repr(run(model).api())

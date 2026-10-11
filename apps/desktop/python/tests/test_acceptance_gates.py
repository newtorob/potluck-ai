"""The recorded `reasoning` and `tools` checks.

A fake model stands in for llama.cpp, with the Qwen tool section in its template
so the traits probe finds tool support. These tests say nothing about a real
model; the real runs are in the change notes.
"""
from __future__ import annotations

import json
import re

import pytest

from potluck_local.services import acceptance as a
from potluck_local.services import acceptance_gates as g
from potluck_local.services.model_traits import TemplateTraits


SENTENCES = (
    "The ocean is a vast and mysterious body of water that covers over 70% of the Earth's surface. "
    "It is home to diverse marine life and plays a crucial role in regulating the Earth's climate."
)


class BaseFake:
    """Answers the suite's required and recorded prompts. `mode` "doubled" repeats every word."""

    def __init__(self, mode: str = "good", *, template: str = "") -> None:
        self.mode = mode
        self.metadata = {"tokenizer.chat_template": template} if template else {}
        self.resets = 0
        self._vocab: list[str] = []

    def _answer(self, prompt: str) -> str:
        if "58213" in prompt:
            text = "58213"
        elif "2 + 2" in prompt:
            text = "4"
        elif "France" in prompt:
            text = "Paris"
        elif "vault code" in prompt:
            text = "73941"
        else:
            text = SENTENCES
        return " ".join(w for w in text.split() for _ in (0, 1)) if self.mode == "doubled" else text

    def create_chat_completion(self, *, messages, temperature, max_tokens, stream):
        text = self._answer(messages[0]["content"])
        return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": max(1, len(text.split()))}}

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


TOOL_TEMPLATE = """{%- if tools %}{%- for tool in tools %}{{- tool | tojson }}{%- endfor %}{%- endif %}
{%- for message in messages %}{{- message.role + ': ' + message.content + '\\n' }}{%- endfor %}
{%- if add_generation_prompt %}assistant: {%- endif %}"""
THINK_TEMPLATE = TOOL_TEMPLATE + "{%- if enable_thinking is defined and enable_thinking is false %}<think></think>{%- endif %}"
OPENED_TEMPLATE = "{%- for m in messages %}{{ m.content }}{%- endfor %}{%- if add_generation_prompt %}<|Assistant|><think>\n{%- endif %}"

CALL = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>'
THINKING_391 = "<think>\n17 times 20 is 340, 17 times 3 is 51, so 391.\n</think>\n\n391"


class Gated(BaseFake):
    """Adds what the two gates ask for: a reasoning prompt and a two-turn tool conversation."""

    def __init__(self, template="", *, reasoning=THINKING_391, call=CALL, reply="It is 17 degrees in Paris.", **kw):
        super().__init__(template=template, **kw)
        self.reasoning, self.call, self.reply = reasoning, call, reply
        self.gate_calls: list[dict] = []

    def create_chat_completion(self, *, messages, temperature, max_tokens, stream, tools=None):
        if tools is None and "17 * 23" not in messages[0]["content"]:
            return super().create_chat_completion(messages=messages, temperature=temperature, max_tokens=max_tokens, stream=stream)
        self.gate_calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens, "temperature": temperature})
        if tools is None:
            text = self.reasoning
        elif messages[-1]["role"] == "tool":
            text = self.reply
        else:
            text = self.call
        return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {"completion_tokens": 9}}


def gate_outcomes(model, level="quick", on_cpu=False):
    run = a.run_checks(model, level, n_ctx=4096, on_cpu=on_cpu)
    return run, {o.id: o for o in run.outcomes if o.id in ("reasoning", "tools")}


# ---- the judges ------------------------------------------------------------------


@pytest.mark.parametrize("raw,opened,ok", [
    (THINKING_391, False, True),
    ("Working: 17*23 = 391.\n</think>\n\n391", True, True),
    ("<think>\n\n</think>\n\n391", False, False),                     # no reasoning came back
    ("391", False, False),                                           # no reasoning at all
    ("<think>\nhmm\n</think>\n\n390", False, False),                 # reasoning but the wrong answer
    ("<think>\nstill working it out", False, False),                 # cut off inside the block
    ("<think>\nhmm\n</think>\n\nThe answer is 391 (see <think> tag)", False, False),  # a block marker left in the answer
    ("Working: 391.\n</think>\n\n391", False, False),                 # template did not open the block: a lone close tag is text
])
def test_reasoning_judge(raw, opened, ok):
    assert g.judge_reasoning(raw, TemplateTraits(opens_reasoning=opened, thinks=True)) is ok


def test_tools_judge_needs_the_result_in_the_reply():
    assert g.judge_tools("It is 17 degrees.") is True
    assert g.judge_tools("") is False
    assert g.judge_tools("It is sunny.") is False


# ---- which models are asked ---------------------------------------------------------


def test_a_model_that_neither_thinks_nor_has_tools_records_both_as_skipped_and_passed():
    run, got = gate_outcomes(BaseFake())
    assert got["reasoning"].skipped == g.SKIP_NOT_THINKING and got["tools"].skipped == g.SKIP_NO_TOOLS
    assert all(o.passed and not o.required for o in got.values()) and all(o.tokens == 0 for o in got.values())
    assert run.passed


def test_the_stored_record_gains_a_skipped_key_only_for_skipped_checks():
    run, got = gate_outcomes(BaseFake())
    assert got["reasoning"].record()["skipped"] == g.SKIP_NOT_THINKING
    assert "skipped" not in run.outcomes[0].record()
    assert got["reasoning"].api() == {"id": "reasoning", "passed": True, "required": False, "skipped": g.SKIP_NOT_THINKING}
    assert set(run.api()[0]) == {"id", "passed", "required"}  # an ordinary entry keeps its three keys
    assert run.api()[0] == {"id": "copy", "passed": True, "required": True}


def test_a_model_with_a_tool_template_is_asked_the_two_turn_weather_question():
    model = Gated(TOOL_TEMPLATE)
    run, got = gate_outcomes(model)
    assert got["tools"].passed and got["tools"].skipped is None and got["tools"].tokens == 18
    assert got["reasoning"].skipped == g.SKIP_NOT_THINKING
    first, second = model.gate_calls
    assert first["tools"][0]["function"]["name"] == "get_weather" and first["temperature"] == 0.0
    assert first["messages"] == [{"role": "user", "content": "What's the weather in Paris right now? Use the tool."}]
    assert first["max_tokens"] == g.FIRST_TURN_TOKENS and second["max_tokens"] == g.SECOND_TURN_TOKENS
    roles = [m["role"] for m in second["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assistant, tool = second["messages"][1:]
    assert assistant["tool_calls"][0]["function"] == {"name": "get_weather", "arguments": {"city": "Paris"}}
    assert tool["tool_call_id"] == assistant["tool_calls"][0]["id"] and json.loads(tool["content"])["temp_c"] == 17


@pytest.mark.parametrize("kw", [
    {"call": "It is probably sunny."},                                                         # no call
    {"call": '<tool_call>{"name": "get_weather", "arguments": {"city": "Berlin"}}</tool_call>'},  # wrong city
    {"call": '<tool_call>{"name": "get_forecast", "arguments": {"city": "Paris"}}</tool_call>'},  # not the offered tool
    {"call": '<tool_call>{"name": "get_weather", "arguments": {"city": </tool_call>'},            # malformed
    {"reply": "It is sunny in Paris."},                                                         # result not used
    {"reply": ""},
])
def test_the_tools_check_fails_when_any_step_goes_wrong_and_the_run_still_passes(kw):
    run, got = gate_outcomes(Gated(TOOL_TEMPLATE, **kw))
    assert got["tools"].passed is False and not got["tools"].skipped and run.passed


def test_a_failed_first_turn_does_not_ask_a_second_time_and_keeps_what_the_model_said_as_the_excerpt():
    model = Gated(TOOL_TEMPLATE, call="It is probably sunny.")
    _, got = gate_outcomes(model)
    assert len(model.gate_calls) == 1 and got["tools"].excerpt == "It is probably sunny."


def test_a_model_that_thinks_is_asked_for_reasoning_with_thinking_on():
    model = Gated(THINK_TEMPLATE)
    _, got = gate_outcomes(model)
    reasoning_calls = [c for c in model.gate_calls if c["tools"] is None]
    assert got["reasoning"].passed and got["reasoning"].skipped is None
    assert reasoning_calls[0]["messages"] == [{"role": "user", "content": g.REASONING_PROMPT}]  # no /no_think
    assert reasoning_calls[0]["max_tokens"] == g.REASONING_TOKENS
    tool_calls = [c for c in model.gate_calls if c["tools"] is not None]
    assert tool_calls[0]["messages"][0]["content"].endswith(" /no_think")  # the tool turn is asked with thinking off


def test_a_thinking_model_that_does_not_separate_its_reasoning_fails_the_recorded_check_only():
    run, got = gate_outcomes(Gated(THINK_TEMPLATE, reasoning="391"))
    assert got["reasoning"].passed is False and run.passed


def test_a_template_that_opens_the_block_is_judged_with_that_in_mind():
    model = Gated(OPENED_TEMPLATE, reasoning="17 x 23 = 391.\n</think>\n\n391")
    _, got = gate_outcomes(model)
    assert got["reasoning"].passed is True and got["tools"].skipped == g.SKIP_NO_TOOLS


def test_on_the_cpu_the_quick_check_skips_both_and_the_full_check_runs_them():
    quick_model = Gated(THINK_TEMPLATE)
    _, got = gate_outcomes(quick_model, "quick", on_cpu=True)
    assert got["reasoning"].skipped == g.SKIP_SLOW and got["tools"].skipped == g.SKIP_SLOW and quick_model.gate_calls == []
    _, got = gate_outcomes(Gated(THINK_TEMPLATE), "full", on_cpu=True)
    assert got["reasoning"].passed and got["tools"].passed and not got["tools"].skipped


def test_when_a_required_check_failed_the_recorded_ones_are_not_asked():
    model = Gated(THINK_TEMPLATE, mode="doubled")
    run, got = gate_outcomes(model)
    assert not run.passed and model.gate_calls == []
    assert got["reasoning"].skipped == g.SKIP_FAILED and got["tools"].skipped == g.SKIP_FAILED


def test_a_model_that_throws_in_a_gate_has_failed_that_check_and_the_run_does_not_raise():
    class Throws(Gated):
        def create_chat_completion(self, *, messages, temperature, max_tokens, stream, tools=None):
            if tools is not None:
                raise RuntimeError("template error")
            return super().create_chat_completion(messages=messages, temperature=temperature, max_tokens=max_tokens, stream=stream)

    run, got = gate_outcomes(Throws(TOOL_TEMPLATE))
    assert got["tools"].passed is False and got["tools"].error == "RuntimeError" and run.passed


def test_an_out_of_memory_in_a_gate_is_raised_like_any_other_check():
    class Oom(Gated):
        def create_chat_completion(self, *, messages, temperature, max_tokens, stream, tools=None):
            if tools is not None:
                raise RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB")
            return super().create_chat_completion(messages=messages, temperature=temperature, max_tokens=max_tokens, stream=stream)

    from potluck_local.services.inference_errors import InferenceFailure

    with pytest.raises(InferenceFailure):
        a.run_checks(Oom(TOOL_TEMPLATE), "quick", n_ctx=4096, on_cpu=False)


def test_the_gates_cannot_change_which_models_pass():
    base = a.run_checks(BaseFake(), "quick", n_ctx=4096, on_cpu=False)
    for kw in ({}, {"call": "no call"}, {"reply": "no"}):
        gated = a.run_checks(Gated(TOOL_TEMPLATE, **kw), "quick", n_ctx=4096, on_cpu=False)
        assert gated.passed is base.passed is True
    assert [o.id for o in a.run_checks(BaseFake(), "quick", n_ctx=4096, on_cpu=False).outcomes if o.required] == ["copy", "arithmetic", "repeats"]


def test_the_suite_version_was_bumped_so_stored_proofs_are_stale():
    assert a.SUITE_VERSION == 2


def test_excerpts_stay_short_and_nothing_is_logged_at_info(caplog):
    caplog.set_level("INFO")
    _, got = gate_outcomes(Gated(TOOL_TEMPLATE, reply="It is 17 degrees. " + "x" * 500))
    assert len(got["tools"].excerpt) <= 80
    assert not [r for r in caplog.records if "degrees" in r.getMessage() or "Paris" in r.getMessage()]


# ---- qwen3-1.7b-q4, replayed from its recorded replies ---------------------------------------------

import qwen3_recorded as rec  # noqa: E402


class Qwen3Replay(BaseFake):
    """Answers the required prompts, and the two recorded checks with what qwen3-1.7b-q4 really said."""

    def __init__(self, **kw):
        super().__init__(template=THINK_TEMPLATE, **kw)
        self.asked: list[dict] = []

    def create_chat_completion(self, *, messages, temperature, max_tokens, stream, tools=None):
        self.asked.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        if tools is None and "17 * 23" not in messages[0]["content"]:
            return super().create_chat_completion(messages=messages, temperature=temperature, max_tokens=max_tokens, stream=stream)
        if tools is None:
            text, finish, tokens = rec.SUM_512 if max_tokens <= 512 else rec.SUM_2048
        elif messages[-1]["role"] == "tool":
            text, finish, tokens = rec.SECOND_NO_THINK
        else:
            text, finish, tokens = rec.TOOLS_NO_THINK if messages[0]["content"].endswith("/no_think") else rec.TOOLS_THINKING
        return {"choices": [{"message": {"content": text}, "finish_reason": finish}], "usage": {"completion_tokens": tokens}}


def test_the_tools_check_passes_on_the_recorded_replies_of_a_model_that_opens_the_block_and_never_closes_it():
    model = Qwen3Replay()
    run, got = gate_outcomes(model)
    tools = got["tools"]
    assert tools.passed and not tools.skipped and "17" in tools.excerpt
    assert [m["role"] for m in [c for c in model.asked if c["tools"]][1]["messages"]] == ["user", "assistant", "tool"]
    assert run.passed


def test_the_reasoning_check_that_ran_out_of_its_budget_is_recorded_as_skipped_and_not_as_failed():
    run, got = gate_outcomes(Qwen3Replay())
    reasoning = got["reasoning"]
    assert reasoning.passed is True and reasoning.skipped == "the reasoning did not finish in 512 tokens"
    assert reasoning.finish_reason == "length" and reasoning.tokens == 512 and reasoning.excerpt.startswith("<think> Okay, let's see.")
    assert reasoning.api() == {"id": "reasoning", "passed": True, "required": False,
                               "skipped": "the reasoning did not finish in 512 tokens"}
    assert reasoning.record()["skipped"] == "the reasoning did not finish in 512 tokens"
    assert run.passed


def test_the_budget_is_not_raised_to_make_the_check_pass():
    model = Qwen3Replay()
    gate_outcomes(model)
    asked = [c for c in model.asked if c["tools"] is None and "17 * 23" in c["messages"][0]["content"]]
    assert [c["max_tokens"] for c in asked] == [g.REASONING_TOKENS] and g.REASONING_TOKENS == 512


def test_a_finished_wrong_reply_is_still_a_failure():
    class Wrong(Qwen3Replay):
        def create_chat_completion(self, *, messages, temperature, max_tokens, stream, tools=None):
            if tools is None and "17 * 23" in messages[0]["content"]:
                return {"choices": [{"message": {"content": "<think>\nHmm.\n</think>\n\n390"}, "finish_reason": "stop"}],
                        "usage": {"completion_tokens": 9}}
            return super().create_chat_completion(messages=messages, temperature=temperature, max_tokens=max_tokens,
                                                  stream=stream, tools=tools)

    _, got = gate_outcomes(Wrong())
    assert got["reasoning"].passed is False and got["reasoning"].skipped is None


def test_a_tool_reply_cut_off_at_the_budget_is_skipped_and_one_that_finished_without_a_call_fails():
    class Ramble(Qwen3Replay):
        finish = "length"

        def create_chat_completion(self, *, messages, temperature, max_tokens, stream, tools=None):
            if tools is not None:
                return {"choices": [{"message": {"content": "Let me think about the weather in Paris " * 5}, "finish_reason": self.finish}],
                        "usage": {"completion_tokens": max_tokens}}
            return super().create_chat_completion(messages=messages, temperature=temperature, max_tokens=max_tokens,
                                                  stream=stream, tools=tools)

    _, got = gate_outcomes(Ramble())
    assert got["tools"].skipped == f"the reply did not finish in {g.FIRST_TURN_TOKENS} tokens" and got["tools"].passed
    Ramble.finish = "stop"
    _, got = gate_outcomes(Ramble())
    assert got["tools"].passed is False and got["tools"].skipped is None


def test_a_second_turn_cut_off_at_the_budget_is_skipped_with_that_budget():
    class SlowSecond(Qwen3Replay):
        def create_chat_completion(self, *, messages, temperature, max_tokens, stream, tools=None):
            if tools is not None and messages[-1]["role"] == "tool":
                return {"choices": [{"message": {"content": "<think>\nThe result says"}, "finish_reason": "length"}],
                        "usage": {"completion_tokens": max_tokens}}
            return super().create_chat_completion(messages=messages, temperature=temperature, max_tokens=max_tokens,
                                                  stream=stream, tools=tools)

    _, got = gate_outcomes(SlowSecond())
    assert got["tools"].skipped == f"the reply did not finish in {g.SECOND_TURN_TOKENS} tokens"


# ---- the public form of a stored record ---------------------------------------------------------


def test_public_checks_carry_the_skipped_reason_only_where_there_is_one():
    stored = [
        {"id": "copy", "passed": True, "required": True, "finish_reason": "stop", "tokens": 5, "excerpt": "58213", "ms": 70},
        {"id": "reasoning", "passed": True, "required": False, "finish_reason": "length", "tokens": 512,
         "excerpt": "<think> Okay", "ms": 5000, "skipped": "the reasoning did not finish in 512 tokens"},
    ]
    assert a.public_checks(stored) == [
        {"id": "copy", "passed": True, "required": True},
        {"id": "reasoning", "passed": True, "required": False, "skipped": "the reasoning did not finish in 512 tokens"},
    ]
    assert a.public_checks(None) == [] and a.public_checks([]) == []


def test_a_stored_proof_reaches_the_public_summary_with_its_skipped_reasons():
    from potluck_local.services.runtime import LLMRuntime

    proof = {"checks": [{"id": "copy", "passed": True, "required": True, "excerpt": "x"},
                        {"id": "tools", "passed": True, "required": False, "skipped": g.SKIP_NO_TOOLS}],
             "checked_at": "2026-10-10T00:00:00Z", "output_tokens": 3}
    rt = LLMRuntime()
    rt._publish("quick", None, proof=proof)
    assert rt._acceptance["checks"] == [
        {"id": "copy", "passed": True, "required": True},
        {"id": "tools", "passed": True, "required": False, "skipped": g.SKIP_NO_TOOLS}]

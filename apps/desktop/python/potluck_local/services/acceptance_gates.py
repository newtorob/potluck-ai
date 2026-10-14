"""Two recorded checks that look at what the gateway offers on top of answers.

They follow the acceptance gates of Sybil's local-ai-registry:

  reasoning  the thinking comes back separately and the answer is right.
             "What is 17 * 23?" is answered 391, with the reasoning split out
             of the answer text by services/reasoning_split.py.
  tools      a weather tool is called with the right city, and its result is
             used in the reply. Two turns: the model is offered get_weather and
             asked about Paris; the call is read by services/tool_calls.py; then
             the tool result goes back and the reply must contain the
             temperature (17).

Both are recorded, never required (see acceptance.py), so they cannot change
which models pass. They only run where they mean something: reasoning for a
model that thinks, tools for a template that renders tools. Anything else, and
the CPU quick check, is recorded as skipped with the reason.

A reply that runs out of its token budget says nothing about the model or the
device, so it is not a failure either. When the engine stopped a reply for
length before the check could see what it needed (the answer after the
reasoning, the tool call, the sentence using the result), `out_of_budget`
gives the reason and the check is recorded as skipped. The budgets are not
raised to avoid this: the checks run on every first load and a slow machine
pays for every token.

This module is called by acceptance.run_checks and does not import acceptance.
The ``ask_*`` functions return ``(raw, visible, finish_reason, tokens)``, the
fields of acceptance.Reply. Model output is data: it is judged here and kept as
a short excerpt by the caller, never logged.

Run after a load, these checks give way to real work (acceptance.RecordedPlan).
The ``ask_*`` functions then take an ``interrupt`` event and read the reply from
a stream, so a request that arrives can stop a reply between two tokens. Without
an interrupt the call is the one these checks have always made.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from potluck_local.services.model_traits import TemplateTraits
from potluck_local.services.reasoning_split import split_reasoning
from potluck_local.services.tool_calls import CallDetector, extract_tool_calls

REASONING_PROMPT = "What is 17 * 23? Reply with only the number."
REASONING_ANSWER = "391"
# A thinking model reasons before it answers. This is room for a short sum.
REASONING_TOKENS = 512

TOOLS_PROMPT = "What's the weather in Paris right now? Use the tool."
TOOL_NAME = "get_weather"
TOOL_RESULT = {"city": "Paris", "temp_c": 17, "sky": "overcast"}
TOOL_RESULT_WORD = "17"
FIRST_TURN_TOKENS = 96
SECOND_TURN_TOKENS = 64
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Current weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}

SKIP_NOT_THINKING = "not a thinking model"
SKIP_NO_TOOLS = "the chat template has no tool section"
SKIP_SLOW = "skipped on the CPU in the quick check"
SKIP_FAILED = "a required check failed"


def reasoning_skip(traits: TemplateTraits | None, *, slow: bool) -> str | None:
    if traits is None or not traits.thinks:
        return SKIP_NOT_THINKING
    return SKIP_SLOW if slow else None


def tools_skip(traits: TemplateTraits | None, *, slow: bool) -> str | None:
    if traits is None or not traits.supports_tools:
        return SKIP_NO_TOOLS
    return SKIP_SLOW if slow else None


class Interrupted(Exception):
    """A reply was abandoned between two tokens because real work is waiting for the model."""


def chat_result(model: Any, interrupt: threading.Event | None, **kwargs: Any) -> dict:
    """``model.create_chat_completion(**kwargs)`` as one non-streaming result.

    With no ``interrupt`` that is the plain call, ``stream=False``. With one, the
    reply is read from a stream and put together here, and the stream is closed
    and Interrupted raised as soon as the event is set, before the reply is
    complete. Closing a stream early is what a client that disconnects does to
    the runtime, so the model is left in the state it is in then. The text is
    the same as the non-streaming one. ``completion_tokens`` counts the chunks
    that carried text, which is the token count for this wrapper's streams.
    """
    if interrupt is None:
        return model.create_chat_completion(stream=False, **kwargs)
    chunks = model.create_chat_completion(stream=True, **kwargs)
    text: list[str] = []
    finish: str | None = None
    count = 0
    try:
        for chunk in chunks:
            choice = (chunk.get("choices") or [{}])[0]
            piece = (choice.get("delta") or {}).get("content")
            if isinstance(piece, str) and piece:
                text.append(piece)
                count += 1
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            if finish is None and interrupt.is_set():
                raise Interrupted()
    finally:
        close = getattr(chunks, "close", None)
        if callable(close):
            close()
    return {"choices": [{"message": {"content": "".join(text)}, "finish_reason": finish}],
            "usage": {"completion_tokens": count}}


def _complete(model: Any, messages: list[dict], max_tokens: int, interrupt: threading.Event | None = None,
              **extra: Any) -> tuple[str, str | None, int]:
    result = chat_result(model, interrupt, messages=messages, temperature=0.0, max_tokens=max_tokens, **extra)
    choice = (result.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content")
    tokens = (result.get("usage") or {}).get("completion_tokens")
    return (text if isinstance(text, str) else ""), choice.get("finish_reason"), tokens if isinstance(tokens, int) else 0


# ---------------------------------------------------------------------------
# reasoning
# ---------------------------------------------------------------------------


def ask_reasoning(model: Any, traits: TemplateTraits,
                  interrupt: threading.Event | None = None) -> tuple[str, str, str | None, int]:
    raw, finish, tokens = _complete(model, [{"role": "user", "content": REASONING_PROMPT}], REASONING_TOKENS,
                                    interrupt)
    _, content = split_reasoning(raw, starts_in_reasoning=traits.opens_reasoning)
    return raw, content, finish, tokens


def judge_reasoning(raw: str, traits: TemplateTraits) -> bool:
    """Some reasoning, separate from the answer, and the answer is right."""
    reasoning, content = split_reasoning(raw, starts_in_reasoning=traits.opens_reasoning)
    return bool(reasoning.strip()) and REASONING_ANSWER in content and "<think>" not in content


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def _right_call(calls: list) -> bool:
    if not calls or calls[0].name != TOOL_NAME:
        return False
    try:
        city = json.loads(calls[0].arguments).get("city", "")
    except (ValueError, AttributeError):
        return False
    return "paris" in str(city).lower()


def _first_turn_calls(raw: str, traits: TemplateTraits) -> tuple[str, list]:
    # A model with thinking off can still open the block and go straight to the call.
    _, answer = split_reasoning(raw, starts_in_reasoning=traits.opens_reasoning, calls=CallDetector([TOOL_NAME]))
    return extract_tool_calls(answer, [TOOL_NAME])


def ask_tools(model: Any, traits: TemplateTraits, *, think_off: bool,
              interrupt: threading.Event | None = None) -> tuple[str, str, str | None, int]:
    """Both turns. ``visible`` is the second reply, or "" when the first turn did
    not make the right call (then ``raw`` is what the model said instead)."""
    prompt = f"{TOOLS_PROMPT} /no_think" if think_off else TOOLS_PROMPT
    messages: list[dict] = [{"role": "user", "content": prompt}]
    raw, finish, tokens = _complete(model, messages, FIRST_TURN_TOKENS, interrupt, tools=[WEATHER_TOOL])
    before, calls = _first_turn_calls(raw, traits)
    if not _right_call(calls):
        return raw, "", finish, tokens
    call = calls[0]
    second_turn = [
        *messages,
        {"role": "assistant", "content": before,
         "tool_calls": [{"id": call.id, "type": "function",
                         "function": {"name": call.name, "arguments": json.loads(call.arguments)}}]},
        {"role": "tool", "tool_call_id": call.id, "content": json.dumps(TOOL_RESULT)},
    ]
    second, finish, more = _complete(model, second_turn, SECOND_TURN_TOKENS, interrupt, tools=[WEATHER_TOOL])
    _, reply = split_reasoning(second, starts_in_reasoning=traits.opens_reasoning)
    return raw, reply, finish, tokens + more


def judge_tools(visible: str) -> bool:
    return TOOL_RESULT_WORD in visible


def out_of_budget(check_id: str, raw: str, visible: str, finish_reason: str | None, traits: TemplateTraits) -> str | None:
    """Why a check that did not pass is recorded as skipped: the engine cut the
    reply off at the token budget before the check could see what it needed.
    None when the reply finished, or the check had what it needed and was wrong."""
    if finish_reason != "length":
        return None
    if check_id == "reasoning":
        _, content = split_reasoning(raw, starts_in_reasoning=traits.opens_reasoning)
        return None if content.strip() else f"the reasoning did not finish in {REASONING_TOKENS} tokens"
    # The turn that was cut off is the second one when the first made the right call.
    budget = SECOND_TURN_TOKENS if _right_call(_first_turn_calls(raw, traits)[1]) else FIRST_TURN_TOKENS
    return f"the reply did not finish in {budget} tokens"

"""What a loaded model's chat template can do: render tools, open a reasoning block.

Whether the template supports tools is found by rendering it, not by reading its
text. A template that never mentions the ``tools`` variable would ignore a
request's tools without a word, and one that reads tools from somewhere else
(Phi-4 mini keeps them on the system message) would too. So the check renders a
one-message conversation with a probe tool and looks for the probe's name in the
prompt.

The same render, without tools, shows whether the template ends its prompt
inside a ``<think>`` block (DeepSeek R1 distills and Qwen3 Thinking do). The
model's output then starts inside the block and only ``</think>`` appears.

Rendering uses llama-cpp-python's own formatter, so the answer matches what the
model is really sent. It is cached by template text. Anything that goes wrong
reads as "no tools, no opened block": the safe answer is a clear refusal, not
a silent drop.

Two templates need more than a yes or no, and ``fit_model`` gives a freshly loaded
model a chat handler for them:

* Meta's Llama 3.x template writes the tool instructions into the first user
  message unless ``tools_in_user_message`` is false. The user's question then
  reads "respond with a JSON for a function call" on every turn, the one after a
  tool result included. Measured on Llama 3.1 8B Q4 (2026-10-10, 8 runs each):
  the reply used the tool result 4 to 6 times in 8, and never after two results.
  With the instructions in the system message it was 8 in 8 for both, and the
  second of two different tools was still called 8 in 8. Leaving tools out after
  a result (Ollama's template does) fixed the first case and lost the second tool.
* The same template refuses a history where one assistant message holds two
  calls, though the model writes two calls in one reply. Such a message is
  rendered as one call and its result, then the next.
"""

from __future__ import annotations

import logging
import weakref
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger(__name__)

_PROBE_TOOL_NAME = "probe_tool_zq91"
_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": _PROBE_TOOL_NAME,
        "description": "probe",
        "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    },
}
_PROBE_MESSAGES = [{"role": "user", "content": "hi"}]
_PROBE_SYSTEM = [{"role": "system", "content": "be brief"}, *_PROBE_MESSAGES]


def _probe_call(call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": _PROBE_TOOL_NAME, "arguments": {"x": "1"}}}


_PROBE_TWO_CALLS = [
    *_PROBE_MESSAGES,
    {"role": "assistant", "content": "", "tool_calls": [_probe_call("a"), _probe_call("b")]},
    {"role": "tool", "tool_call_id": "a", "content": "1"},
    {"role": "tool", "tool_call_id": "b", "content": "2"},
]

_TOOLS_IN_USER_SWITCH = "tools_in_user_message"
_TOOLS_IN_SYSTEM = "{%- set tools_in_user_message = false -%}"


@dataclass(frozen=True)
class TemplateTraits:
    supports_tools: bool = False
    # The prompt ends inside <think>: the model's own output starts in the block.
    opens_reasoning: bool = False
    # The model can reason: a hybrid switch (Qwen3's enable_thinking) or a
    # template that opens the block.
    thinks: bool = False
    # The template refuses two calls in one assistant message, and renders the
    # same calls given one message each.
    single_tool_call: bool = False


@lru_cache(maxsize=16)
def traits_of_template(template: str, bos_token: str = "", eos_token: str = "") -> TemplateTraits:
    thinks_switch = "enable_thinking" in template
    try:
        from llama_cpp.llama_chat_format import Jinja2ChatFormatter

        formatter = Jinja2ChatFormatter(template=template, eos_token=eos_token, bos_token=bos_token)
    except Exception as exc:  # noqa: BLE001 - an unreadable template supports nothing
        logger.debug("template_unreadable error=%s", type(exc).__name__)
        return TemplateTraits(thinks=thinks_switch)
    supports = opens = False
    try:
        supports = _PROBE_TOOL_NAME in formatter(messages=_PROBE_MESSAGES, tools=[_PROBE_TOOL]).prompt
    except Exception as exc:  # noqa: BLE001
        logger.debug("template_tool_probe_failed error=%s", type(exc).__name__)
    try:
        opens = formatter(messages=_PROBE_MESSAGES).prompt.rstrip().endswith("<think>")
    except Exception as exc:  # noqa: BLE001
        logger.debug("template_probe_failed error=%s", type(exc).__name__)
    single = supports and not _renders(formatter, _PROBE_TWO_CALLS) and _renders(formatter, split_tool_calls(_PROBE_TWO_CALLS))
    return TemplateTraits(supports_tools=supports, opens_reasoning=opens, thinks=thinks_switch or opens,
                          single_tool_call=single)


def _renders(formatter, messages: list[dict]) -> bool:
    try:
        formatter(messages=messages, tools=[_PROBE_TOOL])
    except Exception:  # noqa: BLE001 - a template says no by raising
        return False
    return True


def split_tool_calls(messages: list[dict]) -> list[dict]:
    """Give each call of a many-call assistant message a message of its own, with
    its result right after it. Results are matched by ``tool_call_id``, then by
    order. Messages with one call or none are passed through as they are."""
    out: list[dict] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        i += 1
        if not isinstance(calls, list) or len(calls) < 2:
            out.append(message)
            continue
        results = []
        while i < len(messages) and messages[i].get("role") == "tool":
            results.append(messages[i])
            i += 1
        by_id = {r["tool_call_id"]: r for r in results if isinstance(r.get("tool_call_id"), str)}
        matched = [by_id.get(c.get("id")) if isinstance(c, dict) else None for c in calls]
        spare = [r for r in results if not any(r is m for m in matched)]
        for n, call in enumerate(calls):
            out.append({**message, "content": message.get("content") if n == 0 else "", "tool_calls": [call]})
            result = matched[n] or (spare.pop(0) if spare else None)
            if result is not None:
                out.append(result)
        out.extend(spare)
    return out


@lru_cache(maxsize=16)
def fitted_template(template: str, bos_token: str = "", eos_token: str = "") -> str:
    """The template to render with: the model file's own, or the same with its tool
    instructions moved to the system message (see the module text). The move is
    kept only if a request without tools still renders exactly as before."""
    if _TOOLS_IN_USER_SWITCH not in template:
        return template
    moved = _TOOLS_IN_SYSTEM + template
    try:
        from llama_cpp.llama_chat_format import Jinja2ChatFormatter

        before = Jinja2ChatFormatter(template=template, eos_token=eos_token, bos_token=bos_token)
        after = Jinja2ChatFormatter(template=moved, eos_token=eos_token, bos_token=bos_token)
        same = all(before(messages=m).prompt == after(messages=m).prompt for m in (_PROBE_MESSAGES, _PROBE_SYSTEM))
        plain = after(messages=_PROBE_SYSTEM).prompt
        with_tools = after(messages=_PROBE_SYSTEM, tools=[_PROBE_TOOL]).prompt
    except Exception as exc:  # noqa: BLE001 - keep the template the model came with
        logger.debug("template_fit_failed error=%s", type(exc).__name__)
        return template
    # Moved means: the tool is in the prompt, and everything from the system
    # message's own words onward (the user's turn above all) reads as it does
    # with no tools at all.
    untouched = plain[plain.index(_PROBE_SYSTEM[0]["content"]):] if _PROBE_SYSTEM[0]["content"] in plain else None
    moved_ok = same and untouched is not None and _PROBE_TOOL_NAME in with_tools and with_tools.endswith(untouched)
    return moved if moved_ok else template


def _token_text(model, getter_name: str) -> str:
    try:
        token = getattr(model, getter_name)()
        return model._model.token_get_text(token)  # noqa: SLF001 - how llama-cpp-python reads it itself
    except Exception:  # noqa: BLE001 - only used inside the probe's output
        return ""


_by_model: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def traits_of_model(model) -> TemplateTraits | None:
    """Traits of a loaded llama.cpp model, or None when it cannot say (a split
    engine, a transformers backend, nothing loaded). Worked out once per model
    object, so a request does not touch the native model to answer it."""
    try:
        cached = _by_model.get(model)
    except TypeError:  # not weak-referenceable: work it out each time
        cached = None
    if cached is not None:
        return cached
    traits = _traits_of_model(model)
    if traits is not None:
        try:
            _by_model[model] = traits
        except TypeError:
            pass
    return traits


def _traits_of_model(model) -> TemplateTraits | None:
    metadata = getattr(model, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    chat_format = getattr(model, "chat_format", None)
    if chat_format is not None and not str(chat_format).startswith("chat_template."):
        # llama-cpp-python is using one of its built-in formats, which ignore tools.
        return TemplateTraits()
    template = metadata.get("tokenizer.chat_template")
    if not isinstance(template, str) or not template:
        return TemplateTraits()
    return traits_of_template(template, _token_text(model, "token_bos"), _token_text(model, "token_eos"))


def fit_model(model) -> bool:
    """Give a freshly loaded llama.cpp model a chat handler that suits its template,
    when llama-cpp-python's own does not (see the module text). True if one was set.
    A model whose template needs neither change is left exactly as it was loaded.
    Never raises."""
    try:
        if getattr(model, "chat_handler", None) is not None or getattr(model, "chat_format", None) != "chat_template.default":
            return False
        metadata = getattr(model, "metadata", None)
        template = metadata.get("tokenizer.chat_template") if isinstance(metadata, dict) else None
        if not isinstance(template, str) or not template:
            return False
        bos, eos = _token_text(model, "token_bos"), _token_text(model, "token_eos")
        fitted = fitted_template(template, bos, eos)
        split = traits_of_template(template, bos, eos).single_tool_call
        if fitted == template and not split:
            return False
        from llama_cpp.llama_chat_format import Jinja2ChatFormatter

        # Built the way llama-cpp-python builds the handler it would have used.
        handler = Jinja2ChatFormatter(template=fitted, eos_token=eos, bos_token=bos,
                                      stop_token_ids=[model.token_eos()]).to_chat_handler()
        model.chat_handler = _splitting(handler) if split else handler
        logger.info("chat_template_fitted tools_in_system=%s split_tool_calls=%s", fitted != template, split)
        return True
    except Exception as exc:  # noqa: BLE001 - the model still works with its own template
        logger.warning("chat_template_fit_failed error=%s", type(exc).__name__)
        return False


def _splitting(handler):
    def split_then_handle(*, messages, **kwargs):
        return handler(messages=split_tool_calls(messages), **kwargs)

    return split_then_handle


def traits_of_runtime(runtime) -> TemplateTraits | None:
    """Traits of the model a runtime has loaded. Never raises."""
    try:
        getter = getattr(runtime, "template_traits", None)
        return getter() if callable(getter) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("template_traits_failed error=%s", type(exc).__name__)
        return None

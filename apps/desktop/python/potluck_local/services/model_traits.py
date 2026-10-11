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


@dataclass(frozen=True)
class TemplateTraits:
    supports_tools: bool = False
    # The prompt ends inside <think>: the model's own output starts in the block.
    opens_reasoning: bool = False
    # The model can reason: a hybrid switch (Qwen3's enable_thinking) or a
    # template that opens the block.
    thinks: bool = False


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
    return TemplateTraits(supports_tools=supports, opens_reasoning=opens, thinks=thinks_switch or opens)


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


def traits_of_runtime(runtime) -> TemplateTraits | None:
    """Traits of the model a runtime has loaded. Never raises."""
    try:
        getter = getattr(runtime, "template_traits", None)
        return getter() if callable(getter) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("template_traits_failed error=%s", type(exc).__name__)
        return None

"""Explicit, evidence-bound generation profiles for investigations.

The Qwen3 settings follow https://huggingface.co/Qwen/Qwen3-8B. The
registered GGUF template is pinned because a model name alone does not prove
that the active formatter implements the thinking switch.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

QWEN3_NON_THINKING = "qwen3-non-thinking"
_QWEN3_TEMPLATE_SHA256 = "57f1fd00f0013a2be96aa79b857391f27e23df5b5f847072b524c897e24d0361"


@dataclass(frozen=True)
class InvestigationProfile:
    name: str
    temperature: float
    top_p: float
    top_k: int
    min_p: float
    template_sha256: str
    backend: str = "llama-cpp-python"
    enable_thinking: bool = False
    repeat_penalty: float = 1.0

    def metadata(self) -> dict:
        return asdict(self)


_QWEN3 = InvestigationProfile(QWEN3_NON_THINKING, 0.7, 0.8, 20, 0.0, _QWEN3_TEMPLATE_SHA256)


def resolve_profile(name: str) -> InvestigationProfile:
    if name != QWEN3_NON_THINKING:
        raise ValueError(f"Unknown investigation generation profile: {name}")
    return _QWEN3


def stock_handler(model, profile: InvestigationProfile | None, cancel_event):
    """Build a request-local handler around the actual registered GGUF formatter.

    Cancellation is checked between generated tokens; prompt evaluation may not
    stop until prefill completes. The registered formatter and EOS stop remain
    untouched, and tokenization still goes through the model's prompt guard.
    """
    import llama_cpp
    from llama_cpp import llama_chat_format

    if not isinstance(model, llama_cpp.Llama) or model.chat_handler is not None:
        raise RuntimeError("Investigation generation requires a verified stock chat handler")
    handler = model._chat_handlers.get(model.chat_format)
    if handler is None:
        try:
            handler = llama_chat_format.get_chat_completion_handler(model.chat_format)
        except (KeyError, ValueError) as exc:
            raise RuntimeError("Investigation generation requires a verified stock chat handler") from exc
    known = llama_chat_format.chat_formatter_to_chat_completion_handler(lambda **kwargs: None)
    if getattr(handler, "__code__", None) is not known.__code__:
        raise RuntimeError("Investigation generation requires a verified stock chat handler")
    closure = dict(zip(handler.__code__.co_freevars, handler.__closure__ or ()))
    formatter_cell = closure.get("chat_formatter")
    formatter = formatter_cell.cell_contents if formatter_cell else None
    if not callable(formatter):
        raise RuntimeError("Investigation generation requires a verified stock formatter")
    if profile is not None:
        metadata = getattr(model, "metadata", {})
        template = metadata.get("tokenizer.chat_template", "")
        if (model.chat_format != "chat_template.default"
                or metadata.get("general.architecture") != "qwen3"
                or not isinstance(template, str)
                or hashlib.sha256(template.encode()).hexdigest() != profile.template_sha256):
            raise RuntimeError("Investigation profile requires the verified Qwen3 GGUF template")
        if (not isinstance(formatter, llama_chat_format.Jinja2ChatFormatter)
                or formatter.template != template or not formatter.add_generation_prompt):
            raise RuntimeError("Investigation profile requires the verified GGUF formatter")

    def render(**kwargs):
        if profile is not None:
            kwargs["enable_thinking"] = False
        result = formatter(**kwargs)
        criteria = list(result.stopping_criteria or [])
        criteria.append(lambda tokens, logits: cancel_event.is_set())
        result.stopping_criteria = llama_cpp.StoppingCriteriaList(criteria)
        return result

    return llama_chat_format.chat_formatter_to_chat_completion_handler(render)

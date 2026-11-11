"""Explicit, evidence-bound generation profiles for investigations.

The Qwen3 settings follow https://huggingface.co/Qwen/Qwen3-8B. The
registered GGUF template is pinned because a model name alone does not prove
that the active formatter implements the thinking switch.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

QWEN3_NON_THINKING = "qwen3-non-thinking"
QWEN38_27B_NATIVE_NON_THINKING = "qwen3.8-27b-native-non-thinking"
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


@dataclass(frozen=True)
class NativeInvestigationProfile(InvestigationProfile):
    model_sha256: str = ""
    engine_commit: str = ""
    render_hashes: tuple[str, str] = ("", "")


_QWEN3 = InvestigationProfile(QWEN3_NON_THINKING, 0.7, 0.8, 20, 0.0, _QWEN3_TEMPLATE_SHA256)
_QWEN38_NATIVE = NativeInvestigationProfile(
    QWEN38_27B_NATIVE_NON_THINKING, 0.7, 0.8, 20, 0.0,
    "c3cf9e34abf4f9e36c2d72165aa9c132d3e2a725b6c2586aaa3a8af9d7a81041",
    backend="llama-server",
    model_sha256="96457164fd300e37e8cc4c611ad5f8f849db51e2d3b29bbede98012c724b90e3",
    engine_commit="d80619397c2ba5cf1366c29f7d43699ca3a159b9",
    render_hashes=("9f81e9d717249bef5fc543518009ba0e427864fbe56fe8c3e43db5a5a1fb8092",
                   "0a7393b3d4d034f93bf43512915604111ad1b8ac27edf386602eaf776556b061"),
)


def resolve_profile(name: str) -> InvestigationProfile:
    if name == QWEN3_NON_THINKING:
        return _QWEN3
    if name == QWEN38_27B_NATIVE_NON_THINKING:
        return _QWEN38_NATIVE
    raise ValueError(f"Unknown investigation generation profile: {name}")


def validate_native(model, profile: NativeInvestigationProfile, *, model_sha256: str | None,
                    engine_commit: str | None) -> None:
    """Require the loaded native renderer and both thinking-switch probe outputs."""
    import httpx
    from potluck_local.services.local_server import LocalServerModel

    render_hashes = profile.render_hashes
    if (profile.backend != "llama-server" or profile.enable_thinking is not False
            or not profile.model_sha256 or not profile.engine_commit
            or render_hashes is None or len(render_hashes) != 2 or render_hashes[0] == render_hashes[1]
            or type(model) is not LocalServerModel or model.closed
            or model.process.process.poll() is not None
            or model_sha256 != profile.model_sha256 or engine_commit != profile.engine_commit
            or model.investigation_template_sha256 != profile.template_sha256):
        raise RuntimeError("Investigation profile requires the verified native model and template")
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            for enabled, expected in zip((False, True), render_hashes):
                response = client.post(model.base + "/apply-template", headers=model.headers,
                    json={"messages": [{"role": "user", "content": "hi"}],
                          "chat_template_kwargs": {"enable_thinking": enabled}})
                response.raise_for_status()
                prompt = response.json().get("prompt")
                if not isinstance(prompt, str) or hashlib.sha256(prompt.encode()).hexdigest() != expected:
                    raise RuntimeError("Investigation profile requires the verified native renderer")
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise RuntimeError("Investigation profile requires the verified native renderer") from exc


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

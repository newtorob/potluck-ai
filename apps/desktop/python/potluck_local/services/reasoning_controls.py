"""Validated per-request controls for the native engine's chat template.

This is a transport capability, not a claim that every model supports every
effort level. Arbitrary template variables cannot cross this boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .gateway_request import RequestError


@dataclass(frozen=True)
class ReasoningControls:
    enable_thinking: bool | None = None
    effort: str | None = None
    budget_tokens: int | None = None

    @property
    def disabled(self) -> bool:
        return self.enable_thinking is False or self.effort == "none"

    def wire(self) -> dict:
        body = {}
        if self.enable_thinking is not None:
            body["chat_template_kwargs"] = {"enable_thinking": self.enable_thinking}
        if self.effort is not None:
            body["reasoning_effort"] = self.effort
        if self.budget_tokens is not None:
            body["reasoning_budget_tokens"] = self.budget_tokens
        return body


def parse(template: Any = None, effort: Any = None, budget: Any = None, *, max_tokens: int | None = None) -> ReasoningControls | None:
    if budget is not None:
        if type(budget) is not int or not 0 <= budget <= 65535:
            raise RequestError("invalid_reasoning_controls", "reasoning_budget_tokens must be an integer from 0 to 65535.")
        if type(max_tokens) is not int or budget >= max_tokens:
            raise RequestError("invalid_reasoning_controls", "An explicit output token limit greater than reasoning_budget_tokens is required.")
    enabled = None
    if template is not None:
        if not isinstance(template, dict) or set(template) - {"enable_thinking"}:
            raise RequestError("invalid_reasoning_controls",
                               "chat_template_kwargs accepts only enable_thinking.")
        if "enable_thinking" in template:
            enabled = template["enable_thinking"]
            if type(enabled) is not bool:
                raise RequestError("invalid_reasoning_controls", "enable_thinking must be true or false.")
    if effort is not None and (not isinstance(effort, str) or effort not in (
        "none", "minimal", "low", "medium", "high", "xhigh",
    )):
        raise RequestError("invalid_reasoning_controls", "Unsupported reasoning effort.")
    if enabled is not None and effort is not None and enabled != (effort != "none"):
        raise RequestError("conflicting_reasoning_controls",
                           "enable_thinking and reasoning_effort disagree. Send one control or matching values.")
    return None if enabled is None and effort is None and budget is None else ReasoningControls(enabled, effort, budget)


def supported(runtime: Any) -> bool:
    check = getattr(runtime, "supports_reasoning_controls", None)
    return callable(check) and check() is True


def require(runtime: Any) -> None:
    if not supported(runtime):
        raise RequestError("reasoning_controls_unsupported",
                           "Thinking controls require the native llama-server backend or a managed split head.")


def peer_supported(cap: dict, controls: ReasoningControls) -> bool:
    return cap.get("reasoning_controls_version") == 1 and (
        controls.budget_tokens is None or cap.get("reasoning_budget_version") == 1)

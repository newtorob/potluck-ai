"""Opt-in single-machine inference using Potluck's pinned, supervised llama-server.

The child listens only on loopback with a fresh key. All public requests still
pass through the runtime's admission, identity and gateway policy checks.
"""
from __future__ import annotations

import secrets
import sys
import threading
import time
from pathlib import Path

import httpx

from potluck_local.services import prompt_guard
from potluck_local.services.model_traits import TemplateTraits
from potluck_local.services.split_native import NativeProcess, SplitError, SplitModel, binary, engine_bundle, free_port

LOAD_TIMEOUT = 180.0
HEARTBEAT_INTERVAL = 5.0
PROBE_TOOL = {
    "type": "function",
    "function": {"name": "potluck_probe_zq91", "description": "Capability probe",
                 "parameters": {"type": "object", "properties": {}}},
}


def bundle() -> tuple[Path, dict]:
    # The watchdog uses POSIX process groups; do not pretend Windows is qualified.
    if sys.platform not in ("darwin", "linux"):
        raise SplitError("The native local engine is available on macOS and Linux builds only.",
                         503, code="native_engine_unavailable")
    try:
        return engine_bundle()
    except SplitError:
        raise SplitError("This build has no compatible native engine. Use llama-cpp-python or install a build containing the pinned native engine.",
                         503, code="native_engine_unavailable") from None


class LocalServerModel(SplitModel):
    managed_split = False
    managed_native = True

    def __init__(self, process, port, key, protected_tokens=None):
        self._stopped = threading.Event()
        # protected_tokens feeds prompt_guard, as it does for the managed split path.
        super().__init__(process, port, key, self._stopped.set, protected_tokens)
        self.template_traits = TemplateTraits()
        self._monitor = threading.Thread(target=self._watch, daemon=True)
        self._monitor.start()

    def _watch(self):
        while not self._stopped.wait(HEARTBEAT_INTERVAL):
            try:
                self.process.heartbeat()
            except Exception:
                self.failed("The native local engine stopped. Reload the model to continue.")
                return

    def failed(self, error):
        # The shared transport's split-specific recovery advice does not apply here.
        message = "The native local engine could not complete the request. Reload the model and retry."
        super().failed(message)
        return SplitError(message, 502, code="native_inference_failed")

    @staticmethod
    def _cancelled():
        return SplitError("Native inference was cancelled.", 499, code="native_cancelled")

    def initialize(self, context: int):
        """Require the engine's effective context and probe its own renderer.

        A Python template parser may disagree with the native engine; only the
        renderer that serves inference can establish native tool support.
        """
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.get(self.base + "/props", headers=self.headers)
            response.raise_for_status()
            props = response.json()
            actual = props.get("default_generation_settings", {}).get("n_ctx")
            if type(actual) is not int or actual != context or props.get("total_slots") != 1:
                raise SplitError("The native engine did not provide the requested context window. Reduce the context size and reload.",
                                 503, code="native_context_mismatch")
            if self._slot_busy(1) is not False:
                raise SplitError("The native engine is not ready for requests.", 503, code="native_not_ready")
            supports = False
            caps = props.get("chat_template_caps") or {}
            if caps.get("supports_tools") is True and caps.get("supports_tool_calls") is True:
                response = client.post(self.base + "/apply-template", headers=self.headers,
                                       json={"messages": [{"role": "user", "content": "hi"}],
                                             "tools": [PROBE_TOOL]})
                if response.status_code == 200:
                    prompt = response.json().get("prompt")
                    supports = isinstance(prompt, str) and PROBE_TOOL["function"]["name"] in prompt
            response = client.post(self.base + "/apply-template", headers=self.headers,
                                   json={"messages": [{"role": "user", "content": "hi"}]})
            response.raise_for_status()
            prompt = response.json().get("prompt")
            if not isinstance(prompt, str):
                raise SplitError("The native engine could not render the model's chat template.",
                                 503, code="native_template_invalid")
            opens = prompt.rstrip().endswith("<think>")
            # Preserve raw reasoning for the gateway's per-request formatting policy.
            template = props.get("chat_template", "")
            self.template_traits = TemplateTraits(
                supports_tools=supports, opens_reasoning=opens,
                thinks=opens or isinstance(template, str) and "enable_thinking" in template,
            )


def start(model_path: str, *, root: Path, n_ctx: int, n_gpu_layers: int,
          n_threads: int | None, cache_mib: int) -> LocalServerModel:
    port, key = free_port(), secrets.token_hex(32)
    args = [str(binary(root, "llama-server")), "--model", model_path,
            "--host", "127.0.0.1", "--port", str(port), "--ctx-size", str(n_ctx),
            "--parallel", "1", "--slots", "--no-context-shift", "--no-webui",
            "--jinja", "--reasoning-format", "none", "--flash-attn", "auto",
            "--cache-ram", str(cache_mib), "--no-warmup", "--log-verbosity", "4",
            "--gpu-layers", "auto" if n_gpu_layers == -1 else str(n_gpu_layers)]
    if n_threads:
        args += ["--threads", str(n_threads)]
    if n_gpu_layers == 0:
        args += ["--device", "none", "--no-op-offload"]
    process = NativeProcess(args, env={"LLAMA_API_KEY": key})
    model = LocalServerModel(process, port, key, lambda: prompt_guard.gguf_protected_tokens(model_path))
    try:
        deadline = time.monotonic() + LOAD_TIMEOUT
        with httpx.Client(timeout=0.5, trust_env=False) as client:
            while time.monotonic() < deadline:
                if model.closed or process.process.poll() is not None:
                    raise SplitError("The native engine stopped while loading. Check model compatibility and available memory.",
                                     503, code="native_load_failed")
                try:
                    response = client.get(model.base + "/health", headers=model.headers)
                    if response.status_code == 200:
                        model.initialize(n_ctx)
                        return model
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
        raise SplitError("The native model load timed out. Try a smaller model or context window.",
                         503, code="native_load_timeout")
    except BaseException:
        model.close()
        raise

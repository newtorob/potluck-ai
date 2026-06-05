"""Thin llama-cpp-python adapter for the inference benchmark.

Wraps Llama() with metric capture: TTFT, prompt eval tokens/sec,
generation tokens/sec, and peak RSS sampling.

Design constraints:
- No async: the benchmark runner is synchronous (streaming in a thread is
  fine; we don't need async queues for a single-process bench).
- No side effects on the sidecar venv or production runtime.
- Verbose=True on first load to confirm Metal is active; False after that.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import psutil

# llama-cpp-python imported lazily in LlamaCppRuntime.__init__
# to avoid Metal init at import time (matches production runtime pattern).


@dataclass
class InferenceMetrics:
    """Per-run metrics captured from one (model x prompt) inference call."""

    # Timing
    wall_time_s: float = 0.0
    ttft_s: float = 0.0           # time from request start to first token emitted
    prompt_eval_time_s: float = 0.0
    generation_time_s: float = 0.0

    # Tokens
    prompt_tokens: int = 0
    output_tokens: int = 0

    # Derived rates (filled in post-run)
    prompt_eval_toks_per_s: float = 0.0
    gen_toks_per_s: float = 0.0

    # Memory
    peak_rss_bytes: int = 0

    # Status
    success: bool = True
    error: str = ""

    # Full output (for spot-checking)
    output_text: str = ""


class RSSPoller:
    """Samples process RSS in a background thread.

    Polls every `interval_s` seconds and records the maximum observed value.
    Lightweight — just a psutil call in a daemon thread.
    """

    def __init__(self, pid: int | None = None, interval_s: float = 0.1) -> None:
        self._pid = pid or os.getpid()
        self._interval = interval_s
        self._proc = psutil.Process(self._pid)
        self._peak: int = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, daemon=True)

    def start(self) -> "RSSPoller":
        self._peak = self._proc.memory_info().rss
        self._thread.start()
        return self

    def stop(self) -> int:
        """Stop polling and return peak RSS in bytes."""
        self._stop.set()
        self._thread.join(timeout=2.0)
        return self._peak

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                rss = self._proc.memory_info().rss
                if rss > self._peak:
                    self._peak = rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
            self._stop.wait(timeout=self._interval)


class LlamaCppRuntime:
    """Wraps a single Llama model instance for benchmarking.

    One instance per model. Call `load()` before `run_prompt()`.
    Call `unload()` when done to free VRAM/RAM before loading the next model.
    """

    def __init__(
        self,
        model_path: str | Path,
        n_ctx: int = 8192,
        n_gpu_layers: int = -1,
        n_threads: int | None = None,
        verbose_load: bool = True,
    ) -> None:
        self.model_path = str(model_path)
        self.n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self.n_threads = n_threads
        self.verbose_load = verbose_load
        self._model = None
        self._llama_cpp_version: str = ""

    def load(self) -> dict:
        """Load model. Returns metadata dict with load time and version info."""
        from llama_cpp import Llama, __version__ as lcpp_version

        self._llama_cpp_version = lcpp_version

        if self._model is not None:
            self.unload()

        kwargs: dict = {
            "model_path": self.model_path,
            "n_ctx": self.n_ctx,
            "n_gpu_layers": self.n_gpu_layers,
            "verbose": self.verbose_load,
        }
        if self.n_threads is not None:
            kwargs["n_threads"] = self.n_threads

        t0 = time.monotonic()
        self._model = Llama(**kwargs)
        load_time_s = time.monotonic() - t0

        # After first load, suppress verbose for reruns
        self.verbose_load = False

        return {
            "model_path": self.model_path,
            "n_ctx": self.n_ctx,
            "n_gpu_layers": self.n_gpu_layers,
            "n_threads": self.n_threads,
            "load_time_s": round(load_time_s, 2),
            "llama_cpp_version": lcpp_version,
        }

    def unload(self) -> None:
        """Release model and free memory."""
        if self._model is not None:
            del self._model
            self._model = None

    def reset_kv_cache(self) -> None:
        """Clear the KV cache so the next run is a true cold start.

        llama-cpp-python caches prefix tokens across calls on the same model
        instance. Without resetting, run 2+ show near-zero TTFT because the
        prompt tokens are already cached. For a reproducible benchmark we need
        each run to evaluate from scratch.
        """
        if self._model is not None:
            self._model.reset()

    def run_prompt(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 500,
        temperature: float = 0.7,
        top_p: float = 0.9,
        seed: int = 42,
    ) -> InferenceMetrics:
        """Run one inference call, capturing all benchmark metrics.

        Resets the KV cache before every call so each run is a cold prompt
        evaluation — no prefix-match acceleration. This makes TTFT and
        prompt_eval_toks_per_s reproducible across runs.

        Metric capture strategy:
        - TTFT: measure via streaming; stop timing after first token received.
        - Prompt eval time / generation time: from the non-streaming response
          usage dict (timings.prompt_ms, timings.predicted_ms) when available,
          falling back to wall-clock splits.
        - Peak RSS: RSSPoller in a background thread.
        """
        if self._model is None:
            raise RuntimeError("Model not loaded. Call load() first.")

        # Reset KV cache so each run evaluates the prompt from scratch.
        # Without this, llama.cpp prefix-matches the cached tokens from the
        # prior run, making TTFT near-zero on runs 2+. We want cold numbers.
        self.reset_kv_cache()

        metrics = InferenceMetrics()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        # --- Pass 1: TTFT via streaming ---
        rss_poller = RSSPoller().start()
        wall_start = time.monotonic()

        try:
            ttft_s = self._measure_ttft(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                seed=seed,
            )
            metrics.ttft_s = ttft_s
        except Exception as exc:
            peak_rss = rss_poller.stop()
            metrics.success = False
            metrics.error = f"TTFT pass failed: {exc}"
            metrics.wall_time_s = time.monotonic() - wall_start
            metrics.peak_rss_bytes = peak_rss
            return metrics

        # --- Pass 2: Full non-streaming run for token counts + timings ---
        # Reset again: the TTFT streaming pass cached the prompt prefix.
        # Pass 2 must be a cold eval to produce accurate prompt_eval_toks_per_s.
        self.reset_kv_cache()
        try:
            response = self._model.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                seed=seed,
                stream=False,
            )
        except Exception as exc:
            peak_rss = rss_poller.stop()
            metrics.success = False
            metrics.error = f"Full run failed: {exc}"
            metrics.wall_time_s = time.monotonic() - wall_start
            metrics.peak_rss_bytes = peak_rss
            return metrics

        wall_end = time.monotonic()
        peak_rss = rss_poller.stop()

        metrics.wall_time_s = wall_end - wall_start
        metrics.peak_rss_bytes = peak_rss

        # Extract token counts from usage
        usage = response.get("usage", {})
        metrics.prompt_tokens = usage.get("prompt_tokens", 0)
        metrics.output_tokens = usage.get("completion_tokens", 0)

        # Extract timing from llama.cpp metadata if available
        # llama-cpp-python >= 0.2.x exposes timings in the response dict
        timings = response.get("timings", {})
        if timings:
            prompt_ms = timings.get("prompt_ms", 0.0)
            predicted_ms = timings.get("predicted_ms", 0.0)
            if prompt_ms > 0:
                metrics.prompt_eval_time_s = prompt_ms / 1000.0
            if predicted_ms > 0:
                metrics.generation_time_s = predicted_ms / 1000.0
        else:
            # Fallback: approximate from wall time and TTFT
            # Wall time ≈ prompt_eval + generation; TTFT ≈ prompt_eval
            metrics.prompt_eval_time_s = metrics.ttft_s
            metrics.generation_time_s = max(
                0.001, metrics.wall_time_s - metrics.ttft_s
            )

        # Derived rates
        if metrics.prompt_eval_time_s > 0 and metrics.prompt_tokens > 0:
            metrics.prompt_eval_toks_per_s = (
                metrics.prompt_tokens / metrics.prompt_eval_time_s
            )
        if metrics.generation_time_s > 0 and metrics.output_tokens > 0:
            metrics.gen_toks_per_s = metrics.output_tokens / metrics.generation_time_s

        # Capture output text
        choices = response.get("choices", [])
        if choices:
            metrics.output_text = choices[0].get("message", {}).get("content", "")

        return metrics

    def _measure_ttft(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        top_p: float,
        seed: int,
    ) -> float:
        """Stream just enough to capture TTFT. Returns TTFT in seconds.

        We run the generator to first non-empty token, record elapsed time,
        then exhaust the rest to avoid leaving the generator in a broken state.
        llama.cpp holds context state for the duration of the generator.
        """
        t0 = time.monotonic()
        ttft: float = 0.0
        got_first = False

        stream = self._model.create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content")
            if content and not got_first:
                ttft = time.monotonic() - t0
                got_first = True
                break  # stop after first token — exhaust below

        # Drain the generator to clean up internal llama.cpp state
        try:
            for _ in stream:
                pass
        except Exception:
            pass

        return ttft if got_first else (time.monotonic() - t0)

    @property
    def llama_cpp_version(self) -> str:
        return self._llama_cpp_version

    def __repr__(self) -> str:
        loaded = "loaded" if self._model is not None else "unloaded"
        return (
            f"LlamaCppRuntime(path={Path(self.model_path).name!r}, "
            f"n_ctx={self.n_ctx}, n_gpu_layers={self.n_gpu_layers}, "
            f"status={loaded})"
        )

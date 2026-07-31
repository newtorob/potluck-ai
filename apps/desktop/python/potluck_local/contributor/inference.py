"""Real-inference job handlers for the contributor sidecar (Phase G m3).

Two backends, same interface:

  - LlamaCppInferenceEngine: in-process inference via llama-cpp-python.
    Best for NVIDIA (CUDA wheels) and Apple Silicon (Metal). Lowest
    latency, no extra runtime dependency. The default.

  - OllamaInferenceEngine: HTTP roundtrip to a local Ollama daemon. Best
    for AMD and Intel GPUs on Windows where llama-cpp-python's prebuilt
    wheels don't cover GPU acceleration. Ollama handles GPU detection +
    Vulkan/ROCm/CUDA wrangling internally; we just send prompts to it.

Both backends expose:
  stream_completion(prompt, ...) -> AsyncIterator[str]  (single-turn, prompt string)
  stream_chat(messages, ...)     -> AsyncIterator[str]  (multi-turn, applies chat template)

The contributor's job dispatcher (client.py:_handle_real_job) uses stream_chat
when job.messages is populated; otherwise falls back to stream_completion.

Privacy invariant (memory 42a7e671 + 1fc85a49):
    Prompt content and generated tokens MUST NOT be written to disk,
    logs, or telemetry. Counter-only stats (token counts, durations) are
    fine. The Ollama HTTP client uses 127.0.0.1 only — never sends
    prompts off-machine even if Ollama is configured with a remote URL.
    (We refuse to construct an OllamaInferenceEngine with a non-loopback
    URL; see _validate_loopback below.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

logger = logging.getLogger("potluck.contributor.inference")

# A sentinel pushed onto the queue to signal "generator exhausted."
_DONE = object()


class InferenceBackend(Protocol):
    """The interface both engines satisfy. Used only for static typing
    + documentation; we don't enforce at runtime."""

    model_slug: str

    async def stream_completion(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.7,
        top_p: float | None = 0.95,
        top_k: int | None = None,
        repeat_penalty: float | None = None,
        stop_sequences: list[str] | None = None,
        seed: int | None = None,
    ) -> AsyncIterator[str]: ...

    async def stream_chat(
        self,
        messages: list[dict],
        max_tokens: int = 512,
        temperature: float = 0.7,
        top_p: float | None = 0.95,
        top_k: int | None = None,
        repeat_penalty: float | None = None,
        stop_sequences: list[str] | None = None,
        seed: int | None = None,
    ) -> AsyncIterator[str]: ...


# ---- llama-cpp-python backend -------------------------------------------- #


class LlamaCppInferenceEngine:
    """Lazy-loading wrapper around a single llama-cpp-python Llama instance.

    Thread safety: Llama is not thread-safe across calls. We serialize
    inference with an asyncio.Lock so concurrent jobs queue rather than
    racing the model. Multi-model + per-model locks are a future m4
    concern.
    """

    def __init__(
        self,
        model_path: Path,
        model_slug: str,
        n_ctx: int = 4096,
        n_gpu_layers: int = -1,  # offload all layers (Metal/CUDA)
    ) -> None:
        self.model_path = model_path
        self.model_slug = model_slug
        self.n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self._llama = None  # lazily loaded
        self._load_lock = asyncio.Lock()
        self._inference_lock = asyncio.Lock()

    async def _ensure_loaded(self) -> None:
        if self._llama is not None:
            return
        async with self._load_lock:
            if self._llama is not None:
                return
            if not self.model_path.exists():
                raise FileNotFoundError(
                    f"model_path does not exist: {self.model_path}"
                )

            logger.info(
                "loading_model backend=llama-cpp slug=%s path=%s n_ctx=%d n_gpu_layers=%d",
                self.model_slug, self.model_path, self.n_ctx, self.n_gpu_layers,
            )
            loop = asyncio.get_event_loop()
            self._llama = await loop.run_in_executor(None, self._load_sync)
            logger.info("model_loaded backend=llama-cpp slug=%s", self.model_slug)

    def _load_sync(self):
        from llama_cpp import Llama
        return Llama(
            model_path=str(self.model_path),
            n_ctx=self.n_ctx,
            n_gpu_layers=self.n_gpu_layers,
            verbose=False,
        )

    async def stream_completion(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.7,
        top_p: float | None = 0.95,
        top_k: int | None = None,
        repeat_penalty: float | None = None,
        stop_sequences: list[str] | None = None,
        seed: int | None = None,
    ) -> AsyncIterator[str]:
        await self._ensure_loaded()

        async with self._inference_lock:
            queue: asyncio.Queue = asyncio.Queue(maxsize=64)
            loop = asyncio.get_event_loop()
            error_box: list[BaseException] = []
            # Signals the producer thread to stop pulling more tokens from
            # llama.cpp. Checked once per token AND used, together with
            # draining the queue below, to unstick a producer that is
            # currently blocked inside queue.put(...).result() waiting for
            # room -- see the finally block below for why that matters.
            stop_event = threading.Event()

            def _produce() -> None:
                try:
                    assert self._llama is not None
                    completion_kwargs: dict = {
                        "prompt": prompt,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "stream": True,
                    }
                    if top_p is not None:
                        completion_kwargs["top_p"] = top_p
                    if top_k is not None:
                        completion_kwargs["top_k"] = top_k
                    if repeat_penalty is not None:
                        completion_kwargs["repeat_penalty"] = repeat_penalty
                    if stop_sequences:
                        completion_kwargs["stop"] = stop_sequences
                    if seed is not None:
                        completion_kwargs["seed"] = seed

                    for chunk in self._llama.create_completion(**completion_kwargs):
                        if stop_event.is_set():
                            break
                        choice = chunk["choices"][0]
                        text = choice.get("text", "")
                        if text:
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    queue.put(text), loop
                                ).result()
                            except RuntimeError:
                                # Event loop already closed/closing underneath us.
                                break
                except BaseException as exc:  # noqa: BLE001
                    error_box.append(exc)
                finally:
                    try:
                        asyncio.run_coroutine_threadsafe(queue.put(_DONE), loop).result()
                    except RuntimeError:
                        pass

            producer = threading.Thread(
                target=_produce, daemon=True, name="potluck-llamacpp-producer"
            )
            producer.start()

            try:
                while True:
                    item = await queue.get()
                    if item is _DONE:
                        break
                    yield item

                if error_box:
                    raise error_box[0]
            finally:
                # Reached on normal completion, on an exception raised above,
                # AND when this async generator is abandoned mid-stream (WS
                # send failure, disconnect, GC/aclose()) -- e.g. via a
                # GeneratorExit thrown in at `await queue.get()`. Without
                # this, a producer thread blocked inside queue.put(...)
                # .result() on a full queue (maxsize=64) would never see
                # anyone call queue.get() again and would block forever,
                # leaking the OS thread and leaving self._llama's generator
                # half-consumed -- then a LATER job reusing this singleton
                # engine could start a second producer thread that races the
                # first one on the same (not thread-safe) Llama instance.
                stop_event.set()
                # Drain so a producer currently blocked on a full queue can
                # complete its put() and observe stop_event on its next loop
                # iteration. asyncio.Queue.get_nowait() wakes up a waiting
                # put() the same way get() does.
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                if producer.is_alive():
                    await loop.run_in_executor(None, producer.join, 5.0)
                    if producer.is_alive():
                        logger.error(
                            "producer_thread_stuck backend=llama-cpp slug=%s "
                            "(did not stop within 5s of cancellation)",
                            self.model_slug,
                        )

    async def stream_chat(
        self,
        messages: list[dict],
        max_tokens: int = 512,
        temperature: float = 0.7,
        top_p: float | None = 0.95,
        top_k: int | None = None,
        repeat_penalty: float | None = None,
        stop_sequences: list[str] | None = None,
        seed: int | None = None,
    ) -> AsyncIterator[str]:
        """Multi-turn inference via llama-cpp-python create_chat_completion.

        Applies the GGUF's native chat template (stored in the model's metadata)
        so the conversation history is formatted correctly for the model family.
        Streams content deltas from the assistant turn.
        """
        await self._ensure_loaded()

        async with self._inference_lock:
            queue: asyncio.Queue = asyncio.Queue(maxsize=64)
            loop = asyncio.get_event_loop()
            error_box: list[BaseException] = []
            # See stream_completion's finally block for why this exists: it
            # closes the launch-blocking "stranded producer thread" bug.
            stop_event = threading.Event()

            def _produce() -> None:
                try:
                    assert self._llama is not None
                    chat_kwargs: dict = {
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "stream": True,
                    }
                    if top_p is not None:
                        chat_kwargs["top_p"] = top_p
                    if top_k is not None:
                        chat_kwargs["top_k"] = top_k
                    if repeat_penalty is not None:
                        chat_kwargs["repeat_penalty"] = repeat_penalty
                    if stop_sequences:
                        chat_kwargs["stop"] = stop_sequences
                    if seed is not None:
                        chat_kwargs["seed"] = seed

                    for chunk in self._llama.create_chat_completion(**chat_kwargs):
                        if stop_event.is_set():
                            break
                        delta = chunk["choices"][0].get("delta", {})
                        text = delta.get("content") or ""
                        if text:
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    queue.put(text), loop
                                ).result()
                            except RuntimeError:
                                break
                except BaseException as exc:  # noqa: BLE001
                    error_box.append(exc)
                finally:
                    try:
                        asyncio.run_coroutine_threadsafe(queue.put(_DONE), loop).result()
                    except RuntimeError:
                        pass

            producer = threading.Thread(
                target=_produce, daemon=True, name="potluck-llamacpp-producer"
            )
            producer.start()

            try:
                while True:
                    item = await queue.get()
                    if item is _DONE:
                        break
                    yield item

                if error_box:
                    raise error_box[0]
            finally:
                stop_event.set()
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                if producer.is_alive():
                    await loop.run_in_executor(None, producer.join, 5.0)
                    if producer.is_alive():
                        logger.error(
                            "producer_thread_stuck backend=llama-cpp slug=%s "
                            "(did not stop within 5s of cancellation)",
                            self.model_slug,
                        )


# ---- Ollama backend ------------------------------------------------------ #


def _validate_loopback(url: str) -> None:
    """Refuse to talk to a remote Ollama. The whole privacy story rests on
    inference happening on the contributor peer's own machine; an Ollama
    URL pointing at someone else's host means our prompt is leaving this
    box, which violates the manifesto promise. Loopback only."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"Ollama URL must be loopback-only (127.0.0.1 / localhost), got {host!r}. "
            "This is a hard privacy invariant: the in-memory-only rule from manifesto "
            "10.3 means prompts must not leave the peer process to a third party."
        )


class OllamaInferenceEngine:
    """Streams completion from a local Ollama daemon (default port 11434).

    Maps Potluck's SamplingParams to Ollama's options. The model_slug is
    the Ollama-native model name (e.g. "qwen2.5:3b-instruct-q4_K_M");
    callers are responsible for passing the slug Ollama recognises.
    """

    def __init__(
        self,
        base_url: str,
        model_slug: str,
        n_ctx: int = 4096,
    ) -> None:
        _validate_loopback(base_url)
        self.base_url = base_url.rstrip("/")
        self.model_slug = model_slug
        self.n_ctx = n_ctx
        self._verified = False
        self._verify_lock = asyncio.Lock()

    async def _ensure_model_available(self) -> None:
        """Lightweight liveness check: hit GET /api/tags and confirm our
        model_slug is in the installed list. Done once per engine
        lifetime so we don't pay the round-trip on every job."""
        if self._verified:
            return
        async with self._verify_lock:
            if self._verified:
                return
            import httpx
            logger.info(
                "checking_ollama backend=ollama url=%s slug=%s",
                self.base_url, self.model_slug,
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                r.raise_for_status()
                tags = r.json().get("models", [])
                names = {m.get("name", "") for m in tags}
                if self.model_slug not in names:
                    raise FileNotFoundError(
                        f"Ollama doesn't have '{self.model_slug}' installed. "
                        f"Run 'ollama pull {self.model_slug}' on this peer. "
                        f"Models present: {sorted(names) or '(none)'}"
                    )
            self._verified = True
            logger.info("ollama_ready slug=%s", self.model_slug)

    async def stream_completion(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.7,
        top_p: float | None = 0.95,
        top_k: int | None = None,
        repeat_penalty: float | None = None,
        stop_sequences: list[str] | None = None,
        seed: int | None = None,
    ) -> AsyncIterator[str]:
        await self._ensure_model_available()

        # Map Potluck SamplingParams -> Ollama options.
        # Reference: https://github.com/ollama/ollama/blob/main/docs/api.md
        options: dict = {
            "num_predict": max_tokens,
            "temperature": temperature,
            "num_ctx": self.n_ctx,
        }
        if top_p is not None:
            options["top_p"] = top_p
        if top_k is not None:
            options["top_k"] = top_k
        if repeat_penalty is not None:
            options["repeat_penalty"] = repeat_penalty
        if stop_sequences:
            options["stop"] = stop_sequences
        if seed is not None:
            options["seed"] = seed

        body = {
            "model": self.model_slug,
            "prompt": prompt,
            "stream": True,
            "options": options,
        }

        import httpx
        # No client-level timeout for the streaming response itself; the
        # coordinator's per-job 120s wall-clock catches stalls. We DO use
        # a connect timeout so a dead Ollama doesn't hang the request
        # forever.
        timeout = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{self.base_url}/api/generate", json=body
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        # Ollama occasionally emits keepalive blanks; ignore.
                        continue
                    text = chunk.get("response", "")
                    if text:
                        yield text
                    if chunk.get("done"):
                        break

    async def stream_chat(
        self,
        messages: list[dict],
        max_tokens: int = 512,
        temperature: float = 0.7,
        top_p: float | None = 0.95,
        top_k: int | None = None,
        repeat_penalty: float | None = None,
        stop_sequences: list[str] | None = None,
        seed: int | None = None,
    ) -> AsyncIterator[str]:
        """Multi-turn inference via Ollama's /api/chat endpoint.

        Ollama applies the model's native chat template server-side, so the
        messages array is passed as-is. Streams content from the assistant
        message delta in each NDJSON chunk.
        """
        await self._ensure_model_available()

        options: dict = {
            "num_predict": max_tokens,
            "temperature": temperature,
            "num_ctx": self.n_ctx,
        }
        if top_p is not None:
            options["top_p"] = top_p
        if top_k is not None:
            options["top_k"] = top_k
        if repeat_penalty is not None:
            options["repeat_penalty"] = repeat_penalty
        if stop_sequences:
            options["stop"] = stop_sequences
        if seed is not None:
            options["seed"] = seed

        body = {
            "model": self.model_slug,
            "messages": messages,
            "stream": True,
            "options": options,
        }

        import httpx
        timeout = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{self.base_url}/api/chat", json=body
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Ollama /api/chat: {"message": {"role": "assistant", "content": "..."}, "done": bool}
                    msg = chunk.get("message") or {}
                    text = msg.get("content") or ""
                    if text:
                        yield text
                    if chunk.get("done"):
                        break


# ---- Engine factory ------------------------------------------------------ #


_engine: InferenceBackend | None = None


def get_llamacpp_engine(
    model_path: Path,
    model_slug: str,
    n_ctx: int = 4096,
    n_gpu_layers: int = -1,
) -> LlamaCppInferenceEngine:
    """Module-level singleton for llama-cpp. Re-creating per job would
    re-load the GGUF every time."""
    global _engine
    # Recreate (which resets the lazily-loaded Llama, forcing a reload) whenever
    # ANY identity field changes — not just the slug. Keying on slug alone left a
    # stale GGUF loaded if model_path/n_ctx/n_gpu_layers changed under the same
    # slug.
    if (
        not isinstance(_engine, LlamaCppInferenceEngine)
        or _engine.model_slug != model_slug
        or _engine.model_path != model_path
        or _engine.n_ctx != n_ctx
        or _engine.n_gpu_layers != n_gpu_layers
    ):
        _engine = LlamaCppInferenceEngine(
            model_path=model_path,
            model_slug=model_slug,
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
        )
    return _engine


def get_ollama_engine(
    base_url: str,
    model_slug: str,
    n_ctx: int = 4096,
) -> OllamaInferenceEngine:
    """Module-level singleton for Ollama. Cached so we don't re-verify
    model presence on every job."""
    global _engine
    normalized_url = base_url.rstrip("/")
    if (
        not isinstance(_engine, OllamaInferenceEngine)
        or _engine.model_slug != model_slug
        or _engine.base_url != normalized_url
    ):
        _engine = OllamaInferenceEngine(
            base_url=base_url,
            model_slug=model_slug,
            n_ctx=n_ctx,
        )
    return _engine



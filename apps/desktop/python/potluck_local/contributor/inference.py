"""Real-inference job handler for the contributor sidecar (Phase G m3).

Wraps llama-cpp-python's Llama with lazy loading + an async-friendly
streaming surface. The streaming surface bridges Llama's blocking
generator (a synchronous iterator yielding completion chunks) to an
asyncio AsyncIterator the WebSocket message loop can iterate cleanly.

Privacy invariant (memory 42a7e671 + 1fc85a49):
    Prompt content and generated tokens MUST NOT be written to disk,
    logs, or telemetry. Counter-only stats (token counts, durations) are
    fine. The Llama instance keeps the prompt in process memory only as
    long as inference is running; after the generator is exhausted the
    memory is freed by GC.

Single-model-per-peer for m3. Multi-model LRU loading is a follow-up;
for now a peer registers exactly one model_slug at attestation time and
declines jobs requesting any other slug.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from pathlib import Path

logger = logging.getLogger("potluck.contributor.inference")

# A sentinel pushed onto the queue to signal "generator exhausted."
_DONE = object()


class InferenceEngine:
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

            # Llama.__init__ is blocking and slow (model load from disk to
            # VRAM). Run it in a thread so we don't freeze the heartbeat
            # task during cold-load.
            logger.info(
                "loading_model slug=%s path=%s n_ctx=%d n_gpu_layers=%d",
                self.model_slug, self.model_path, self.n_ctx, self.n_gpu_layers,
            )
            loop = asyncio.get_event_loop()
            self._llama = await loop.run_in_executor(None, self._load_sync)
            logger.info("model_loaded slug=%s", self.model_slug)

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
        """Yield generated tokens as they arrive from the model.

        Bridges Llama's sync streaming generator (called in a worker
        thread) to an async iterator via a thread-safe queue. The producer
        thread runs the generator and pushes each chunk onto an asyncio
        queue; this coroutine consumes them in the asyncio event loop.
        """
        await self._ensure_loaded()

        async with self._inference_lock:
            queue: asyncio.Queue = asyncio.Queue(maxsize=64)
            loop = asyncio.get_event_loop()
            error_box: list[BaseException] = []

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
                        # chunk shape: {"choices": [{"text": "...", "index": 0,
                        # "finish_reason": null|"stop"|"length"}], ...}
                        choice = chunk["choices"][0]
                        text = choice.get("text", "")
                        if text:
                            asyncio.run_coroutine_threadsafe(
                                queue.put(text), loop
                            ).result()
                except BaseException as exc:  # noqa: BLE001
                    error_box.append(exc)
                finally:
                    asyncio.run_coroutine_threadsafe(queue.put(_DONE), loop).result()

            # Run the producer in a daemon thread — daemon so an unclean
            # shutdown doesn't hang waiting for it.
            producer = threading.Thread(target=_produce, daemon=True)
            producer.start()

            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                yield item

            if error_box:
                raise error_box[0]


_engine: InferenceEngine | None = None


def get_engine(model_path: Path, model_slug: str) -> InferenceEngine:
    """Module-level singleton. Re-creating the engine on every job would
    re-load the model from disk every time, which is unworkable for
    multi-GB GGUFs. m3 ships one engine per peer; multi-model LRU is m4."""
    global _engine
    if _engine is None or _engine.model_slug != model_slug:
        _engine = InferenceEngine(model_path=model_path, model_slug=model_slug)
    return _engine

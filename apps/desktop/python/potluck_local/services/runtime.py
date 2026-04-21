"""LLM inference runtime.

Wraps llama-cpp-python for model loading, streaming, and non-streaming chat.

TODO: Implement full LLMRuntime per guide §4.2:
  - load_model(model_path, n_ctx, n_gpu_layers, n_threads)
  - unload_model()
  - stream_chat(messages, temperature, max_tokens) — async generator, SSE
  - chat(messages, temperature, max_tokens) — non-streaming coroutine
  - Run blocking Llama() calls via asyncio.get_event_loop().run_in_executor()
    so the event loop is never blocked during inference.
"""

from __future__ import annotations


class LLMRuntime:
    """Manages a single loaded GGUF model via llama-cpp-python.

    One instance per process; the sidecar serves one user at a time.
    """

    def __init__(self) -> None:
        # TODO: initialise Llama instance, threading lock, model path tracking
        pass

    def is_loaded(self) -> bool:
        """Return True if a model is currently loaded and ready for inference."""
        # TODO: return self._model is not None
        return False

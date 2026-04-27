"""Embedding service — wraps sentence-transformers with lazy loading and async safety.

The SentenceTransformer model is intentionally NOT loaded at import time or app
startup. It is downloaded the first time embed() is called. This keeps app startup
fast and avoids blocking uvicorn startup on model download.

All CPU-heavy operations (model load, encode) run in a ThreadPoolExecutor so they
don't block the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

import numpy as np

logger = logging.getLogger(__name__)

# Single thread for inference to avoid CPU contention during concurrent requests.
_embed_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embedder")


class EmbeddingService:
    """Lazy-loading wrapper around sentence-transformers all-MiniLM-L6-v2.

    Thread safety: the internal SentenceTransformer is only ever called from
    _embed_executor (a single-worker pool), so there is no concurrent access.
    """

    MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
    DIM = 384

    def __init__(self) -> None:
        self._model = None  # type: ignore[assignment]
        self._load_lock = asyncio.Lock()
        self._available: bool | None = None  # None = untested yet

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_model_sync(self) -> None:
        """Blocking model load — runs inside the thread pool."""
        from sentence_transformers import SentenceTransformer  # type: ignore[import]
        self._model = SentenceTransformer(self.MODEL_NAME)
        logger.info("embedder_loaded model=%s dim=%d", self.MODEL_NAME, self.DIM)

    async def _ensure_loaded(self) -> bool:
        """Load the model if not already loaded.  Returns False if unavailable."""
        if self._available is True:
            return True
        if self._available is False:
            return False

        async with self._load_lock:
            # Double-check after acquiring lock
            if self._available is not None:
                return self._available is True

            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(_embed_executor, self._load_model_sync)
                self._available = True
            except Exception as exc:
                logger.error(
                    "embedder_load_failed reason=%s — semantic search degraded",
                    exc,
                )
                self._available = False

        return self._available is True

    def _encode_sync(self, texts: list[str]) -> np.ndarray:
        """Blocking encode — runs inside the thread pool.

        Returns float32 matrix of shape (n, DIM), each row L2-normalised so
        that cosine similarity equals a plain dot product.
        """
        vecs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.array(vecs, dtype=np.float32)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True once the model has been loaded successfully."""
        return self._available is True

    async def embed(self, text: str) -> np.ndarray | None:
        """Embed a single string.  Returns None if the model is unavailable."""
        if not await self._ensure_loaded():
            return None
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _embed_executor, self._encode_sync, [text]
        )
        return result[0]  # shape (DIM,)

    async def embed_many(self, texts: list[str]) -> np.ndarray | None:
        """Embed a batch of strings.  Returns None if the model is unavailable.

        Returns float32 array of shape (len(texts), DIM).
        """
        if not texts:
            return np.empty((0, self.DIM), dtype=np.float32)
        if not await self._ensure_loaded():
            return None
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _embed_executor, self._encode_sync, texts
        )


# Module-level singleton
_embedder: EmbeddingService | None = None


def get_embedder() -> EmbeddingService:
    """Return the module-level EmbeddingService singleton."""
    global _embedder
    if _embedder is None:
        _embedder = EmbeddingService()
    return _embedder

"""Embedding service - wraps sentence-transformers with lazy loading and async safety.

The SentenceTransformer model is intentionally not loaded at import time or app
startup. It is downloaded the first time embed() is called. This keeps app startup
fast and avoids blocking uvicorn startup on model download.

All CPU-heavy operations (model load, encode) run in a ThreadPoolExecutor so they
don't block the asyncio event loop.

Model selection:
    Set POTLUCK_EMBED_MODEL to override the default at startup.
    e.g. POTLUCK_EMBED_MODEL=BAAI/bge-small-en-v1.5

Asymmetric embedding (BGE-style):
    Some models (notably BGE family) require a task instruction prepended to the
    query text only.  Document/memory texts are embedded without any prefix.
    The query prefix is controlled by QUERY_PREFIX, which is set per model in
    _MODEL_CONFIGS.  For symmetric models (all-MiniLM, GTE) the prefix is "".

    - embed_query(text) - applies the model's query prefix before encoding
    - embed(text) - raw encode, no prefix (use for document/memory side)
    - embed_many(texts) - raw batch encode, no prefix (use for document/memory side)
"""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

import numpy as np

logger = logging.getLogger(__name__)

# Single thread for inference to avoid CPU contention during concurrent requests.
_embed_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embedder")


class _ModelConfig(NamedTuple):
    dim: int
    query_prefix: str  # prepended to queries only; "" for symmetric models


# Per-model configuration.  Add entries here when adding new candidate models.
# Keys must match the HuggingFace model ID (or the value of POTLUCK_EMBED_MODEL).
_MODEL_CONFIGS: dict[str, _ModelConfig] = {
    # Default - symmetric, no prefix needed
    "sentence-transformers/all-MiniLM-L6-v2": _ModelConfig(dim=384, query_prefix=""),
    # BGE small - asymmetric: queries need the retrieval instruction prefix
    "BAAI/bge-small-en-v1.5": _ModelConfig(
        dim=384,
        query_prefix="Represent this sentence for searching relevant passages: ",
    ),
    # GTE small - symmetric, no prefix needed
    "thenlper/gte-small": _ModelConfig(dim=384, query_prefix=""),
    # MPNet base - 768-dim (breaks DIM=384 storage assumption; flagged, not recommended)
    "sentence-transformers/all-mpnet-base-v2": _ModelConfig(dim=768, query_prefix=""),
}

# bge-small-en-v1.5 is the default: same 384-dim storage as MiniLM (no schema
# change) and same Eval B recall@5 (0.72, no regression), but doubles precision@1
# on hard close-distractor pairs (0.40 -> 0.80) - fixes cases like ranking
# "Python linting" above "use pnpm" for a JS-package query. Asymmetric model:
# embed_query() prepends the retrieval prefix (see _MODEL_CONFIGS). Switching the
# default requires re-embedding existing memories (vectors are model-specific).
_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class EmbeddingService:
    """Lazy-loading wrapper around sentence-transformers.

    The active model is controlled by POTLUCK_EMBED_MODEL (read at instantiation).
    Default: BAAI/bge-small-en-v1.5

    Asymmetric models (BGE): use embed_query() for query-side encoding.
    Symmetric models (MiniLM, GTE): embed() and embed_query() are equivalent.

    Thread safety: the internal SentenceTransformer is only ever called from
    _embed_executor (a single-worker pool), so there is no concurrent access.
    """

    def __init__(self) -> None:
        # Read model name at instantiation so env-var overrides take effect
        # even when the singleton is created after the env is set.
        self.MODEL_NAME: str = os.environ.get("POTLUCK_EMBED_MODEL", _DEFAULT_MODEL)
        cfg = _MODEL_CONFIGS.get(self.MODEL_NAME)
        if cfg is not None:
            self.DIM: int = cfg.dim
            self._query_prefix: str = cfg.query_prefix
        else:
            # Unknown model - assume 384-dim, symmetric. Log a warning.
            logger.warning(
                "embedder_unknown_model model=%s, assuming dim=384 symmetric; "
                "add it to _MODEL_CONFIGS for correct asymmetric handling",
                self.MODEL_NAME,
            )
            self.DIM = 384
            self._query_prefix = ""

        self._model = None  # type: ignore[assignment]
        self._load_lock = asyncio.Lock()
        self._available: bool | None = None  # None = untested yet

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_model_sync(self) -> None:
        """Blocking model load - runs inside the thread pool."""
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
                    "embedder_load_failed reason=%s, semantic search degraded",
                    exc,
                )
                self._available = False

        return self._available is True

    def _encode_sync(self, texts: list[str]) -> np.ndarray:
        """Blocking encode - runs inside the thread pool.

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
        """Embed a single string without a query prefix.

        Use this for document/memory-side encoding.
        Returns None if the model is unavailable.
        """
        if not await self._ensure_loaded():
            return None
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _embed_executor, self._encode_sync, [text]
        )
        return result[0]  # shape (DIM,)

    async def embed_query(self, text: str) -> np.ndarray | None:
        """Embed a query string, applying the model's query prefix if any.

        For symmetric models (MiniLM, GTE) this is identical to embed().
        For asymmetric models (BGE) this prepends the retrieval instruction.
        Returns None if the model is unavailable.
        """
        if not await self._ensure_loaded():
            return None
        prefixed = self._query_prefix + text if self._query_prefix else text
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _embed_executor, self._encode_sync, [prefixed]
        )
        return result[0]  # shape (DIM,)

    async def embed_many(self, texts: list[str]) -> np.ndarray | None:
        """Embed a batch of strings without a query prefix (document/memory side).

        Returns None if the model is unavailable.
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


# Module-level singleton - reset when POTLUCK_EMBED_MODEL changes between runs
# by calling _reset_embedder() (used by the eval harness).
_embedder: EmbeddingService | None = None


def get_embedder() -> EmbeddingService:
    """Return the module-level EmbeddingService singleton."""
    global _embedder
    if _embedder is None:
        _embedder = EmbeddingService()
    return _embedder


def _reset_embedder() -> None:
    """Reset the singleton so a new POTLUCK_EMBED_MODEL value is picked up.

    Only intended for use by the eval harness and tests.  Production code
    should never call this.
    """
    global _embedder
    _embedder = None


def schedule_embed_memory(memory_id: str, content: str) -> None:
    """Schedule embedding (re)generation for a memory as a fire-and-forget task.

    Shared by every write path that inserts or changes memory content
    (explicit save, extraction, import upsert, content edit) so they all
    regenerate + persist the vector and invalidate the cluster cache the same
    way.  Safe to call from any async context - if the event loop isn't
    running (shouldn't happen in production) this is a no-op.  Any failure
    (model unavailable, etc.) is logged and swallowed: keyword search still
    works without a vector, and services.embedding_backfill.run_embedding_backfill
    retries rows that are still missing an embedding for the active model.
    """

    async def _embed_and_save() -> None:
        try:
            from potluck_local.services.memory_clustering import get_cluster_cache
            from potluck_local.services.memory_db import save_embedding

            embedder = get_embedder()
            from potluck_local.services.memory_safety import redact_credentials
            vec = await embedder.embed(redact_credentials(content))
            if vec is not None:
                await save_embedding(memory_id, vec, embedder.MODEL_NAME)
                await get_cluster_cache().invalidate_all()
        except Exception as exc:
            logger.warning("embed_fire_and_forget_failed id=%s reason=%s", memory_id, exc)

    asyncio.create_task(_embed_and_save())

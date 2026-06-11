"""Embedding backfill — one-shot background job run at sidecar startup.

Finds all active memories that have no embedding yet and generates them in
batches of 64.  Runs entirely in the background; startup is never blocked.

If sentence-transformers fails to load (no internet on first run, etc.) the
job logs the error and exits gracefully.  The rest of the app keeps working;
semantic search will fall back to keyword mode.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_BATCH_SIZE = 64


async def run_embedding_backfill() -> None:
    """Generate embeddings for any active memories that are missing them.

    Should be called from app lifespan as asyncio.create_task(...) so it runs
    in the background without blocking startup.
    """
    from potluck_local.services.embeddings import get_embedder
    from potluck_local.services.memory_db import (
        get_memory_db_path,
        memories_missing_embeddings,
        save_embedding,
    )

    embedder = get_embedder()

    # Re-embed memories that are missing an embedding OR were embedded by a
    # different model (stale after an embedding-model switch). Passing the
    # active model name makes the switch self-healing: stale rows are rebuilt
    # with the current model so retrieval (which filters by model) sees them.
    missing_ids = await memories_missing_embeddings(model=embedder.MODEL_NAME)
    n_total = len(missing_ids)
    logger.info("embedding_backfill_started n_memories=%d", n_total)

    if n_total == 0:
        logger.info("embedding_backfill_complete n_memories=0")
        return

    # Attempt to load the model early so we can bail fast on failure.
    if not await embedder._ensure_loaded():
        logger.error(
            "embedding_backfill_aborted reason=model_unavailable "
            "— semantic search will degrade to keyword mode"
        )
        return

    # Fetch content for all missing IDs in one query.
    import aiosqlite

    db_path = get_memory_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in missing_ids)
        cursor = await db.execute(
            f"SELECT id, content FROM memories WHERE id IN ({placeholders})",  # noqa: S608
            missing_ids,
        )
        rows = await cursor.fetchall()

    id_to_content: dict[str, str] = {row["id"]: row["content"] for row in rows}

    n_done = 0
    for batch_start in range(0, len(missing_ids), _BATCH_SIZE):
        batch_ids = missing_ids[batch_start : batch_start + _BATCH_SIZE]
        batch_texts = [id_to_content.get(mid, "") for mid in batch_ids]

        try:
            vectors = await embedder.embed_many(batch_texts)
        except Exception as exc:
            logger.error("embedding_backfill_batch_error reason=%s", exc)
            continue

        if vectors is None:
            logger.error(
                "embedding_backfill_aborted reason=embedder_unavailable "
                "— semantic search will degrade to keyword mode"
            )
            return

        for mid, vec in zip(batch_ids, vectors):
            await save_embedding(mid, vec, embedder.MODEL_NAME)
            n_done += 1

        logger.info(
            "embedding_backfill_progress done=%d total=%d",
            n_done,
            n_total,
        )
        # Yield control between batches to keep the event loop responsive.
        await asyncio.sleep(0)

    logger.info("embedding_backfill_complete n_memories=%d", n_done)

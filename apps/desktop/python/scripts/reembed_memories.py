"""Re-embed all memories with the currently-configured embedder.

Needed when the embedding model changes (e.g. all-MiniLM-L6-v2 -> bge-small):
stored vectors are model-specific, and semantic search does not filter by model,
so a mix of old + new vectors silently corrupts ranking. This re-embeds every
memory that is active or already has an embedding row, overwriting in place
(save_embedding uses INSERT OR REPLACE).

SAFETY: run this only when no sidecar is serving searches with a different
model than the one used here. Concretely, for a MiniLM -> bge migration:
  1. Quit the Potluck desktop app (its running sidecar still embeds queries
     with the old model until relaunched).
  2. Run this with the new model active, e.g.:
       POTLUCK_EMBED_MODEL=BAAI/bge-small-en-v1.5 \
         .venv/bin/python -m scripts.reembed_memories
     (POTLUCK_EMBED_MODEL is optional once it's the source default, but passing
      it explicitly makes the migration unambiguous.)
  3. Relaunch the app with the same model (set POTLUCK_EMBED_MODEL in its
     environment, or use a build whose default is the new model).

Idempotent: safe to run repeatedly. Verifies the model_used column is uniform
on completion.
"""

from __future__ import annotations

import asyncio
import sys

import aiosqlite

from potluck_local.services.embeddings import get_embedder
from potluck_local.services.memory_db import get_memory_db_path, save_embedding

_BATCH = 64


async def _model_distribution(db_path: str) -> dict[str, int]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (
            await db.execute(
                "SELECT model_used, COUNT(*) n FROM memory_embeddings GROUP BY model_used"
            )
        ).fetchall()
    return {r["model_used"]: r["n"] for r in rows}


async def _targets(db_path: str) -> list[tuple[str, str]]:
    """(id, content) for every memory that is active or already has an embedding."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (
            await db.execute(
                """
                SELECT m.id, m.content
                FROM memories m
                WHERE m.is_active = 1
                   OR m.id IN (SELECT memory_id FROM memory_embeddings)
                """
            )
        ).fetchall()
    return [(r["id"], r["content"]) for r in rows]


async def main() -> int:
    db_path = str(get_memory_db_path())
    embedder = get_embedder()

    if not await embedder._ensure_loaded():
        print("ERROR: embedder model could not be loaded, aborting.", file=sys.stderr)
        return 2

    model = embedder.MODEL_NAME
    print(f"DB:           {db_path}")
    print(f"Target model: {model}")
    print(f"Before:       {await _model_distribution(db_path)}")

    targets = await _targets(db_path)
    n_total = len(targets)
    print(f"Re-embedding {n_total} memories...")

    n_done = 0
    for start in range(0, n_total, _BATCH):
        batch = targets[start : start + _BATCH]
        ids = [mid for mid, _ in batch]
        texts = [content for _, content in batch]
        vectors = await embedder.embed_many(texts)
        if vectors is None:
            print("ERROR: embedder returned no vectors, aborting.", file=sys.stderr)
            return 2
        for mid, vec in zip(ids, vectors):
            await save_embedding(mid, vec, model, db_path=db_path)
            n_done += 1
        print(f"  {n_done}/{n_total}")

    dist = await _model_distribution(db_path)
    print(f"After:        {dist}")
    stale = {m: c for m, c in dist.items() if m != model}
    if stale:
        print(f"WARNING: stale embeddings from other models remain: {stale}", file=sys.stderr)
        return 1
    print(f"Done: all {n_done} embeddings are now {model}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

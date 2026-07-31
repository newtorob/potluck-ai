"""Regression test — 2026-07-31 P2 sidecar audit, finding #7.

MemoryRetriever.update_memory() persisted the new content but never touched
memory_embeddings, so an edited memory kept pointing at the vector computed
from its OLD text. services.memory_db.memories_missing_embeddings() only
catches rows with NO embedding at all (or one from a different model) — it
has no way to know a same-model embedding is stale relative to content it no
longer matches — so the periodic backfill never re-embedded it either. The
result: semantic search silently ranked/matched an edited memory on its
pre-edit meaning forever.

The fix: update_memory() clears the embedding row synchronously (in the same
edit) whenever content actually changes, and schedules a fresh embedding via
services.embeddings.schedule_embed_memory. Clearing it also makes
memories_missing_embeddings() pick the row up as a safety net even if the
fire-and-forget regen never runs (e.g. process exits right after the edit).

Isolated via POTLUCK_MEMORY_DB_PATH + POTLUCK_DATA_DIR so this test can never
touch a real ~/.potluck. The embedder is monkeypatched to a fast deterministic
fake — no network/model download required.
"""
from __future__ import annotations

import asyncio

import aiosqlite
import numpy as np
import pytest

from potluck_local.services.embedding_backfill import run_embedding_backfill
from potluck_local.services.memory_db import (
    get_embedding,
    init_memory_db,
    memories_missing_embeddings,
    save_embedding,
)
from potluck_local.services.memory_extraction import get_memory_extractor
from potluck_local.services.memory_retrieval import get_memory_retriever

_FAKE_MODEL_NAME = "test-fake-embed-model"


def _run(coro):
    return asyncio.run(coro)


def _vector_for(text: str) -> np.ndarray:
    """Deterministic per-text vector so different content -> different vector."""
    h = abs(hash(text)) % 997
    return np.array([h / 997.0, 1.0 - h / 997.0], dtype=np.float32)


class _FakeEmbedder:
    MODEL_NAME = _FAKE_MODEL_NAME

    async def _ensure_loaded(self) -> bool:
        return True

    async def embed(self, text: str) -> np.ndarray:
        return _vector_for(text)

    async def embed_many(self, texts: list[str]) -> np.ndarray:
        return np.stack([_vector_for(t) for t in texts])


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(db_path))
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path / "data_dir"))
    monkeypatch.setattr(
        "potluck_local.services.embeddings.get_embedder", lambda: _FakeEmbedder()
    )
    # Neutralise the fire-and-forget regen task everywhere it's scheduled
    # (extract_explicit's insert path AND update_memory's edit path) so this
    # test deterministically exercises only the synchronous "clear on edit"
    # fix plus the explicit, awaited backfill call — not asyncio scheduling
    # order between a background task and the test's own assertions.
    monkeypatch.setattr(
        "potluck_local.services.embeddings.schedule_embed_memory", lambda *a, **k: None
    )
    _run(init_memory_db(db_path))
    return db_path


def test_editing_content_clears_the_stale_embedding_and_backfill_refreshes_it(isolated_env):
    extractor = get_memory_extractor()
    retriever = get_memory_retriever()

    original_content = "the deploy script lives at scripts/deploy.sh"
    mem = _run(
        extractor.extract_explicit(content=original_content, category="fact")
    )

    # Simulate the embedding the (fire-and-forget) insert-path would have
    # produced for the ORIGINAL content, using the same model name the
    # backfill/edit path will check against.
    original_vector = _vector_for(original_content)
    _run(save_embedding(mem.id, original_vector, _FAKE_MODEL_NAME))

    stored = _run(get_embedding(mem.id))
    assert stored is not None
    assert np.allclose(stored[0], original_vector)

    # Edit the content — this is the auditor's repro step.
    new_content = "the deploy script lives at ops/deploy.sh (moved)"
    updated = _run(retriever.update_memory(mem.id, content=new_content))
    assert updated is not None
    assert updated.content == new_content

    # 1. The stale vector must be gone immediately — not silently kept.
    assert _run(get_embedding(mem.id)) is None

    # 2. The backfill's missing-embeddings query must now catch this row
    #    (the safety net referenced in the fix, independent of whether the
    #    fire-and-forget regen task has run yet).
    missing = _run(memories_missing_embeddings(model=_FAKE_MODEL_NAME))
    assert mem.id in missing

    # 3. Confirm edited content actually gets a FRESH vector: run the
    #    established backfill path (awaited, deterministic) and check the
    #    new embedding matches the NEW content, not the old one.
    _run(run_embedding_backfill())
    refreshed = _run(get_embedding(mem.id))
    assert refreshed is not None
    refreshed_vector, model_used = refreshed
    assert model_used == _FAKE_MODEL_NAME
    assert np.allclose(refreshed_vector, _vector_for(new_content))
    assert not np.allclose(refreshed_vector, original_vector)


def test_editing_non_content_fields_leaves_the_embedding_untouched(isolated_env):
    """Regression guard: don't over-clear — a category/confidence-only edit
    must not blow away a perfectly valid embedding."""
    extractor = get_memory_extractor()
    retriever = get_memory_retriever()

    content = "prefers dark mode"
    mem = _run(extractor.extract_explicit(content=content, category="preference"))
    vector = _vector_for(content)
    _run(save_embedding(mem.id, vector, _FAKE_MODEL_NAME))

    updated = _run(retriever.update_memory(mem.id, confidence=0.99))
    assert updated is not None
    assert updated.content == content  # unchanged

    stored = _run(get_embedding(mem.id))
    assert stored is not None
    assert np.allclose(stored[0], vector)

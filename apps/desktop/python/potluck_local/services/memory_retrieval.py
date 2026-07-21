"""Memory retrieval service — keyword-based and semantic retrieval.

Retrieves relevant memories for context injection before each model call.
Also provides semantic search, related-memory lookup, and keyword frequency.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from collections import Counter

import aiosqlite
import numpy as np

from potluck_local.services.ai_contracts import RETRIEVAL_PROFILE_VERSION
from potluck_local.services.memory_db import project_clause
from potluck_local.services.memory_extraction import Memory, MemoryCategory
from potluck_local.utils.time import now_iso as _now_iso

logger = logging.getLogger(__name__)

# In-memory cache of the stacked embedding matrix, keyed by (project, model).
# Value: (memory_ids, matrix, change_key). Avoids reloading and re-stacking
# every active vector on every chat turn — the previous per-call cost was O(N)
# blob reads plus a full np.stack. A cheap COUNT + MAX(updated_at) probe per call
# decides whether the cached matrix is still valid.
_MATRIX_CACHE: "dict[tuple[str | None, str], tuple[list[str], np.ndarray | None, tuple]]" = {}


async def _embeddings_change_key(
    project: str | None, model: str, db_path=None
) -> tuple[int, str | None]:
    """Cheap fingerprint of the model+project-scoped embedding set:
    (count, max memories.updated_at). It changes when a row is added, removed,
    re-embedded to this model, or its memory is updated — so the matrix cache
    can be reused safely without rescanning every blob."""
    from potluck_local.services.memory_db import get_memory_db_path

    path = db_path or get_memory_db_path()
    proj_clause, proj_params = project_clause(project, alias="m.")
    params = [model, *proj_params]
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            f"""SELECT COUNT(*), MAX(m.updated_at)
                FROM memory_embeddings e
                JOIN memories m ON m.id = e.memory_id
                WHERE m.is_active = 1 AND e.model_used = ? AND {proj_clause}""",
            params,
        )
        row = await cur.fetchone()
    return (int(row[0] or 0), row[1])


_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "ought", "used",
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "as",
    "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further", "then",
    "once", "here", "there", "when", "where", "why", "how", "all", "both",
    "each", "few", "more", "most", "other", "some", "such", "no", "nor",
    "not", "only", "own", "same", "so", "than", "too", "very", "just",
    "now", "i", "me", "my", "we", "our", "you", "your", "he", "she", "it",
    "they", "them", "his", "her", "its", "this", "that", "what", "which",
    "who", "whom", "these", "those", "am", "but", "if", "or", "because",
    "about", "up", "and", "don", "also",
})


def _memory_enabled() -> bool:
    try:
        from potluck_local.services.settings_manager import get_settings
        settings = get_settings()
        return getattr(settings.privacy, "memory_enabled", True)
    except Exception:
        return True


def _row_to_memory(row: dict) -> Memory:
    # updated_at may be NULL on rows predating the migration (the backfill in
    # init_memory_db sets it to created_at, but rows written between schema add
    # and process restart could theoretically be NULL in theory).  Fall back to
    # created_at so the model invariant (non-None str) is always satisfied.
    updated_at: str = row.get("updated_at") or row["created_at"]
    return Memory(
        id=row["id"],
        content=row["content"],
        category=row["category"],
        confidence=row["confidence"],
        source_type=row["source_type"],
        source_conversation_id=row["source_conversation_id"],
        created_at=row["created_at"],
        last_accessed_at=row["last_accessed_at"],
        updated_at=updated_at,
        access_count=row["access_count"],
        is_active=bool(row["is_active"]),
        superseded_by=row["superseded_by"],
        project=row["project"],
        extraction_prompt_version=row.get("extraction_prompt_version"),
        pinned=bool(row.get("pinned", 0)),
    )


def _extract_keywords(messages: list[dict], max_keywords: int = 15) -> list[str]:
    """Extract meaningful keywords from the last 3 user messages."""
    recent = [m for m in messages[-3:] if m.get("role") == "user"]
    text = " ".join(m.get("content", "") for m in recent)

    words = text.lower().split()
    cleaned = [w.strip(".,!?;:'\"()[]{}") for w in words if len(w) > 2]
    filtered = [w for w in cleaned if w and w not in _STOPWORDS]

    seen: set[str] = set()
    unique: list[str] = []
    for kw in filtered:
        if kw not in seen:
            seen.add(kw)
            unique.append(kw)

    return unique[:max_keywords]


# project_clause(project, alias) is imported from memory_db — shared with
# task_state + memory_clustering.


from pydantic import BaseModel


class KeywordFrequency(BaseModel):
    keyword: str
    count: int
    memory_ids: list[str]  # up to 20 representative memory IDs


# ---------------------------------------------------------------------------
# Module-level semantic / keyword helpers
# ---------------------------------------------------------------------------


async def semantic_search(
    query: str,
    project: str | None = None,
    limit: int = 20,
    min_confidence: float = 0.0,
    include_pinned: bool = False,
) -> list[tuple[Memory, float]]:
    """Embed the query and return top-K memories by cosine similarity.

    Returns [] (not raises) if the embedder is unavailable — callers should
    fall back to keyword search in that case.

    include_pinned: when True, pinned memories (in scope) are prepended to the
    results regardless of cosine score — ambient/standing context that should
    always be present. Off by default so the RRF retriever's internal call
    isn't double-counted (it injects pinned via its own Tier-0).
    """
    from potluck_local.services.embeddings import get_embedder
    from potluck_local.services.memory_db import (
        get_memory_db_path,
        load_all_embeddings,
    )

    embedder = get_embedder()
    # Use embed_query() so asymmetric models (e.g. BGE) apply their retrieval
    # instruction prefix.  For symmetric models this is identical to embed().
    query_vec = await embedder.embed_query(query)
    if query_vec is None:
        return []

    project = project or None
    model = embedder.MODEL_NAME

    # Matrix cache — rebuild only when the model+project-scoped embedding set
    # actually changed (cheap COUNT + MAX(updated_at) probe), instead of
    # reloading and re-stacking every active vector on every call. Filtering by
    # `model` also guarantees a single embedding dimension in the matrix, so a
    # mixed-model table (after a model switch) can never crash np.stack.
    change_key = await _embeddings_change_key(project, model)
    cache_key = (project, model)
    cached = _MATRIX_CACHE.get(cache_key)
    if cached is not None and cached[2] == change_key:
        memory_ids, matrix = cached[0], cached[1]
    else:
        pairs = await load_all_embeddings(project=project, model=model)
        memory_ids = [p[0] for p in pairs]
        matrix = np.stack([p[1] for p in pairs], axis=0) if pairs else None  # (n, DIM)
        # Drop any stale-model matrices cached for this project (e.g. after an
        # embedding-model switch) so they don't linger keyed by the old model.
        for stale in [c for c in _MATRIX_CACHE if c[0] == project and c[1] != model]:
            del _MATRIX_CACHE[stale]
        _MATRIX_CACHE[cache_key] = (memory_ids, matrix, change_key)

    if not memory_ids:
        return []

    # All vectors are L2-normalised at insert time, so dot product == cosine sim.
    scores = matrix @ query_vec  # (n,)
    top_indices = np.argsort(scores)[::-1][:limit]

    # Fetch memory rows for the top IDs
    top_ids = [memory_ids[i] for i in top_indices]
    top_scores = [float(scores[i]) for i in top_indices]

    db_path = get_memory_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in top_ids)
        cursor = await db.execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders}) AND is_active = 1",  # noqa: S608
            top_ids,
        )
        rows = {row["id"]: dict(row) for row in await cursor.fetchall()}

    results: list[tuple[Memory, float]] = []
    for mid, sim in zip(top_ids, top_scores):
        row = rows.get(mid)
        if row is None:
            continue
        mem = _row_to_memory(row)
        if mem.confidence < min_confidence:
            continue
        results.append((mem, sim))

    if include_pinned:
        # Pinned memories always inject, regardless of cosine score or
        # min_confidence. Prepend them (deduped) and re-cap at `limit`.
        pclause, pparams = project_clause(project or None)
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"""SELECT * FROM memories
                   WHERE is_active = 1 AND pinned = 1 AND {pclause}
                   ORDER BY confidence DESC, last_accessed_at DESC
                   LIMIT ?""",
                pparams + [limit],
            )
            pinned_rows = [dict(r) for r in await cur.fetchall()]
        if pinned_rows:
            have = {m.id for m, _ in results}
            prepend = [(_row_to_memory(r), 1.0) for r in pinned_rows if r["id"] not in have]
            results = (prepend + results)[:limit]

    logger.info("semantic_search query_len=%d results=%d project=%s", len(query), len(results), project)
    return results


async def get_related(
    memory_id: str,
    limit: int = 8,
) -> list[tuple[Memory, float]]:
    """Return the most similar active memories to a given memory (excludes self).

    Uses the target memory's stored embedding.  Returns [] if no embedding exists.
    """
    from potluck_local.services.memory_db import get_embedding, get_memory_db_path

    embed_result = await get_embedding(memory_id)
    if embed_result is None:
        return []

    target_vec, target_model = embed_result

    # Load all embeddings without project filter so related-memory lookup
    # searches the broadest possible set (cross-project relatedness is useful).
    # Restrict to the target's embedding model: vectors from a different model
    # live in a different space (and may have a different dimension), so mixing
    # them is both meaningless for cosine and would crash np.stack below.
    db_path = get_memory_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT e.memory_id, e.embedding
               FROM memory_embeddings e
               JOIN memories m ON m.id = e.memory_id
               WHERE m.is_active = 1 AND e.memory_id != ? AND e.model_used = ?""",
            (memory_id, target_model),
        )
        rows = await cursor.fetchall()

    if not rows:
        return []

    candidate_ids = [row["memory_id"] for row in rows]
    matrix = np.stack(
        [np.frombuffer(row["embedding"], dtype=np.float32).reshape(-1) for row in rows],
        axis=0,
    )

    scores = matrix @ target_vec
    top_indices = np.argsort(scores)[::-1][:limit]
    top_ids = [candidate_ids[i] for i in top_indices]
    top_scores = [float(scores[i]) for i in top_indices]

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in top_ids)
        cursor = await db.execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders}) AND is_active = 1",  # noqa: S608
            top_ids,
        )
        id_to_row = {row["id"]: dict(row) for row in await cursor.fetchall()}

    results: list[tuple[Memory, float]] = []
    for mid, sim in zip(top_ids, top_scores):
        row = id_to_row.get(mid)
        if row is None:
            continue
        results.append((_row_to_memory(row), sim))

    return results


async def get_keywords(
    project: str | None = None,
    limit: int = 100,
    min_count: int = 2,
) -> list[KeywordFrequency]:
    """Compute term frequency over active memory content.

    Filters stopwords (same list as _extract_keywords) and tokens shorter
    than 3 characters.  Returns top-K by count descending.
    """
    from potluck_local.services.memory_db import get_memory_db_path

    project = project or None
    db_path = get_memory_db_path()
    proj_clause, proj_params = project_clause(project)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT id, content FROM memories WHERE is_active = 1 AND {proj_clause}",
            proj_params,
        )
        rows = await cursor.fetchall()

    # tokenise: split on non-word chars, lowercase, filter
    token_memories: dict[str, list[str]] = {}  # token → memory IDs that contain it
    for row in rows:
        mid = row["id"]
        tokens = re.split(r"\W+", row["content"].lower())
        for tok in tokens:
            if len(tok) < 3:
                continue
            if tok in _STOPWORDS:
                continue
            if tok not in token_memories:
                token_memories[tok] = []
            # Append the memory ID once per memory (not per occurrence)
            if not token_memories[tok] or token_memories[tok][-1] != mid:
                token_memories[tok].append(mid)

    # Build count from number of distinct memories containing each token
    counts: Counter[str] = Counter({tok: len(mids) for tok, mids in token_memories.items()})

    results: list[KeywordFrequency] = []
    for keyword, count in counts.most_common(limit):
        if count < min_count:
            break
        results.append(
            KeywordFrequency(
                keyword=keyword,
                count=count,
                memory_ids=token_memories[keyword][:20],
            )
        )

    return results


class MemoryRetriever:
    """Retrieves and formats memories for prompt injection."""

    async def get_relevant_memories(
        self,
        current_messages: list[dict],
        max_memories: int = 20,
        min_confidence: float = 0.5,
        project: str | None = None,
        conversation_id: str | None = None,
        category: MemoryCategory | None = None,
    ) -> list[Memory]:
        """Retrieve memories relevant to the current conversation context.

        project: when given, returns project-scoped + global memories.
        When None, returns only global memories (project IS NULL).

        category: when given, restricts results to that category only.
        Tier 1 (preference + instruction preloading) is skipped entirely when a
        category is specified — the caller asked for something specific and we
        push the filter to the DB rather than post-filtering results.
        """
        if not _memory_enabled():
            return []

        # Normalise empty string to None
        project = project or None

        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        # How many of the max_memories slots Tier 1 (standing preference +
        # instruction preload) may take. Lowered from max_memories//2 (=10) to
        # 3 on 2026-05-28 after the eval showed the half-budget preload buried
        # query-relevant results below the recall@5 cutoff: capping it 10->0
        # roughly doubled recall@5 (0.40->0.80) with no loss to standing-pref
        # recall. We keep a small floor of 3 (not 0) so the top-3-confidence
        # standing rules still inject as ambient context on unrelated queries.
        # NOTE: the eval also showed this confidence-ranked preload only ever
        # delivers ambient context for the top-~3 rules at a tight budget;
        # mid-confidence standing rules never reach the top-5 via preload. True
        # always-inject ambient context needs a separate pinned-instructions
        # mechanism (tracked as a follow-up), not this budget knob.
        # Overridable per-call via POTLUCK_ALWAYS_BUDGET (eval sweeps / tuning).
        _default_always = 3
        always_budget = int(os.environ.get("POTLUCK_ALWAYS_BUDGET", str(_default_always)))
        always_budget = max(0, min(always_budget, max_memories))

        proj_clause, proj_params = project_clause(project)

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")

            always_rows: list[dict] = []

            # Tier 0 — PINNED: standing/ambient memories that always inject,
            # regardless of query OR category. Capped at max_memories so they
            # can't overflow the budget; query results fill whatever remains.
            pinned_cursor = await db.execute(
                f"""SELECT * FROM memories
                   WHERE is_active = 1 AND pinned = 1 AND {proj_clause}
                   ORDER BY confidence DESC, last_accessed_at DESC
                   LIMIT ?""",
                proj_params + [max_memories],
            )
            always_rows = [dict(r) for r in await pinned_cursor.fetchall()]
            pinned_ids = {r["id"] for r in always_rows}

            if category is None:
                # Tier 1: top high-confidence preferences + instructions as
                # ambient context (excludes already-pinned rows so they don't
                # double-count).
                #
                # SECURITY (2026-07-17 audit, MEDIUM): auto-extracted
                # ('source_type=extracted') instruction memories are NOT
                # granted Tier-1 always-inject status here. The extractor
                # pulls 'instruction' memories out of raw conversation text,
                # which may include pasted/quoted untrusted content (e.g. a
                # copied doc containing "ignore previous instructions..."). If
                # that text scores >=0.7 confidence it used to become a
                # standing instruction force-injected into every future
                # session and every MCP-connected agent, with no review. It
                # is still stored and still retrievable via Tier 2 (keyword +
                # semantic ranking, subject to relevance and confidence like
                # any other memory) -- it just no longer bypasses relevance
                # ranking for free. Explicitly user-confirmed instructions
                # (source_type='explicit', i.e. saved via memory_save because
                # the user or agent directly asked to remember them) are
                # unaffected and still preload here, as do preference-category
                # memories of either source_type. The reversible opt-in path
                # for an auto-extracted instruction to become standing/ambient
                # context is the existing `pinned` mechanism (memory_retrieval.
                # set_pinned) -- the same mechanism already used for Tier 0.
                cursor = await db.execute(
                    f"""SELECT * FROM memories
                       WHERE is_active = 1
                         AND pinned = 0
                         AND confidence >= ?
                         AND (
                             category = 'preference'
                             OR (category = 'instruction' AND source_type != 'extracted')
                         )
                         AND {proj_clause}
                       ORDER BY confidence DESC, last_accessed_at DESC
                       LIMIT ?""",
                    [min_confidence] + proj_params + [always_budget],
                )
                for r in await cursor.fetchall():
                    if r["id"] not in pinned_ids:
                        always_rows.append(dict(r))
            # When category is specified, skip the Tier 1 heuristic — but the
            # pinned rows above still inject (that's the whole point of pinning).

            # Tier 2: RRF-fused keyword + semantic retrieval.
            # Both arms apply the same project scope, is_active, category, and
            # min_confidence filters so neither arm can surface results the other
            # would exclude.  Results are merged with Reciprocal Rank Fusion
            # (k=60, 1-based ranks) which is rank-ordinal and requires no score
            # normalisation between cosine similarity and keyword confidence rank.
            #
            # Graceful degradation: if semantic_search returns [] (embedder
            # unavailable or zero stored embeddings), RRF reduces to pure keyword
            # ranking — identical to the previous v1 behavior.
            keywords = _extract_keywords(current_messages)
            # Query results get whatever budget the pinned + ambient rows leave.
            tier2_budget = max(0, max_memories - len(always_rows))

            if category is not None:
                # Restrict to the caller's requested category
                cat_clause = "AND category = ?"
                cat_params: list = [category]
            else:
                # Exclude Tier-1 categories to avoid duplicating those rows --
                # EXCEPT auto-extracted ('source_type=extracted') instruction
                # memories. As of the 2026-07-17 security fix those no longer
                # get Tier-1 always-inject status (see the Tier-1 query
                # above), so Tier 2 is their only path to being surfaced by
                # relevance. Without this carve-out they would be excluded
                # from BOTH tiers -- contradicting the fix's explicit
                # contract that they "are still stored and still retrievable
                # by relevance", not force-injected.
                cat_clause = (
                    "AND (category NOT IN ('preference', 'instruction') "
                    "OR (category = 'instruction' AND source_type = 'extracted'))"
                )
                cat_params = []

            # --- Keyword arm (same SQL as before) ---
            async def _keyword_query() -> list[dict]:
                if not keywords or tier2_budget <= 0:
                    return []
                # Build parameterized OR conditions — no f-string SQL injection risk
                conditions = " OR ".join("content LIKE ?" for _ in keywords)
                like_params = [f"%{kw}%" for kw in keywords]
                # params order: like values, min_confidence, category params, project params, limit
                kw_params: list = like_params + [min_confidence] + cat_params + proj_params + [tier2_budget]
                async with aiosqlite.connect(db_path) as _db:
                    _db.row_factory = aiosqlite.Row
                    cur = await _db.execute(
                        f"""SELECT * FROM memories
                            WHERE is_active = 1
                              AND ({conditions})
                              AND confidence >= ?
                              {cat_clause}
                              AND {proj_clause}
                            ORDER BY confidence DESC, last_accessed_at DESC
                            LIMIT ?""",  # noqa: S608 — conditions built safely above
                        kw_params,
                    )
                    return [dict(r) for r in await cur.fetchall()]

            # --- Semantic arm ---
            # semantic_search already applies: project scope, is_active=1, min_confidence.
            # Category is NOT applied inside semantic_search, so we post-filter here.
            async def _semantic_query() -> list[tuple[Memory, float]]:
                if tier2_budget <= 0 or not keywords:
                    return []
                try:
                    pairs = await semantic_search(
                        query=" ".join(keywords),
                        project=project,
                        limit=tier2_budget,
                        min_confidence=min_confidence,
                    )
                except Exception as _exc:
                    logger.warning("semantic_search_error_in_rrf reason=%s — falling back to keyword", _exc)
                    return []
                # Post-filter by category if required
                if category is not None:
                    pairs = [(m, s) for m, s in pairs if m.category == category]
                return pairs

            # Run both arms in parallel — neither touches the open db connection
            # from the outer context (each opens its own).
            keyword_rows, semantic_pairs = await asyncio.gather(
                _keyword_query(), _semantic_query()
            )

            # --- RRF fusion (1-based ranks) ---
            # Score each memory id by summing 1/(k + rank) across lists it appears in.
            # k is read per-call from POTLUCK_RRF_K (default 60) so the eval can
            # sweep it without a code change — and so an eval-driven tuning loop
            # can later set the winning value. Lower k lets top-ranked hits in
            # either arm dominate (matters at tight budgets); higher k flattens.
            _RRF_K = int(os.environ.get("POTLUCK_RRF_K", "60"))
            rrf_scores: dict[str, float] = {}
            # Build id → row lookup from keyword results
            id_to_row: dict[str, dict] = {}

            for rank, row in enumerate(keyword_rows, start=1):
                mid = row["id"]
                rrf_scores[mid] = rrf_scores.get(mid, 0.0) + 1.0 / (_RRF_K + rank)
                id_to_row[mid] = row

            for rank, (mem, _sim) in enumerate(semantic_pairs, start=1):
                mid = mem.id
                rrf_scores[mid] = rrf_scores.get(mid, 0.0) + 1.0 / (_RRF_K + rank)
                # Reconstruct a dict for memories only seen in the semantic arm
                if mid not in id_to_row:
                    id_to_row[mid] = {
                        "id": mem.id,
                        "content": mem.content,
                        "category": mem.category,
                        "confidence": mem.confidence,
                        "source_type": mem.source_type,
                        "source_conversation_id": mem.source_conversation_id,
                        "created_at": mem.created_at,
                        "last_accessed_at": mem.last_accessed_at,
                        "updated_at": mem.updated_at,
                        "access_count": mem.access_count,
                        "is_active": int(mem.is_active),
                        "superseded_by": mem.superseded_by,
                        "project": mem.project,
                        "extraction_prompt_version": mem.extraction_prompt_version,
                    }

            # Sort by fused score descending, take the tier-2 budget
            sorted_ids = sorted(rrf_scores, key=lambda mid: rrf_scores[mid], reverse=True)
            keyword_rows = [id_to_row[mid] for mid in sorted_ids[:tier2_budget]]

            logger.debug(
                "rrf_tier2 keyword_hits=%d semantic_hits=%d fused=%d budget=%d",
                len(keyword_rows),  # after slice — use pre-slice counts below would be clearer
                len(semantic_pairs),
                len(rrf_scores),
                tier2_budget,
            )

            # Merge Tier 1 + fused Tier 2, deduplicate by id
            seen_ids: set[str] = set()
            combined: list[dict] = []
            for row in always_rows + keyword_rows:
                if row["id"] not in seen_ids:
                    seen_ids.add(row["id"])
                    combined.append(row)

            combined = combined[:max_memories]

            # Update access metadata for returned memories AND log the
            # access with the retrieval profile version. The version stamp
            # lets the debugger answer "what retrieval rules ranked this
            # memory at this point in history?" even after we bump the
            # profile.
            if combined:
                ts = _now_iso()
                conversation_id = conversation_id or ""  # log requires a string
                for row in combined:
                    await db.execute(
                        """UPDATE memories
                           SET last_accessed_at = ?, access_count = access_count + 1
                           WHERE id = ?""",
                        (ts, row["id"]),
                    )
                    await db.execute(
                        """INSERT INTO memory_access_log
                           (id, memory_id, conversation_id, was_useful,
                            accessed_at, retrieval_profile_version)
                           VALUES (?, ?, ?, NULL, ?, ?)""",
                        (
                            str(uuid.uuid4()),
                            row["id"],
                            conversation_id,
                            ts,
                            RETRIEVAL_PROFILE_VERSION,
                        ),
                    )
                await db.commit()

        memories = [_row_to_memory(r) for r in combined]
        logger.info("memory_retrieval_done count=%d project=%s", len(memories), project)
        return memories

    async def list_memories(
        self,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
        project: str | None = None,
        include_all: bool = False,
        since: str | None = None,
    ) -> list[Memory]:
        """Browse active memories without updating access timestamps.

        project: scope filter. Ignored when include_all=True.
        include_all: when True, bypasses the project filter entirely (browse mode).
        since: ISO-8601 string. When given, only rows with updated_at > since are
               returned. Pushed to SQL — not post-filtered.
        """
        # Normalise empty string to None
        project = project or None

        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row

            conditions = ["is_active = 1"]
            params: list = []

            if category is not None:
                conditions.append("category = ?")
                params.append(category)

            if not include_all:
                proj_clause, proj_params = project_clause(project)
                conditions.append(proj_clause)
                params.extend(proj_params)

            if since is not None:
                conditions.append("updated_at > ?")
                params.append(since)

            where = " AND ".join(conditions)
            # When a since cursor is active, order by recency of change so the
            # caller gets the delta newest-first. Without since, preserve the
            # original confidence-descending browse order.
            order = "updated_at DESC, confidence DESC" if since is not None else "confidence DESC, created_at DESC"
            params.extend([limit, offset])

            cursor = await db.execute(
                f"""SELECT * FROM memories
                   WHERE {where}
                   ORDER BY {order}
                   LIMIT ? OFFSET ?""",  # noqa: S608
                params,
            )
            rows = await cursor.fetchall()

        return [_row_to_memory(dict(r)) for r in rows]

    async def get_memory(self, memory_id: str) -> Memory | None:
        """Fetch a single memory by ID regardless of is_active state."""
        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        return _row_to_memory(dict(row))

    async def set_pinned(self, memory_id: str, pinned: bool) -> bool:
        """Pin/unpin a memory so retrieval always (or no longer) injects it
        regardless of query. Returns True if an active row was affected."""
        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "UPDATE memories SET pinned = ?, updated_at = ? WHERE id = ? AND is_active = 1",
                (1 if pinned else 0, _now_iso(), memory_id),
            )
            await db.commit()
            affected = cursor.rowcount > 0
        if affected:
            logger.info("memory_pin_set id=%s pinned=%s", memory_id, pinned)
        return affected

    async def delete_memory(self, memory_id: str) -> bool:
        """Permanently delete a memory and its embedding. Returns True if removed.

        Hard delete by design: "delete" must mean the content is actually gone
        from disk, not just flagged inactive — that's the privacy contract for a
        local-first store the user owns. A prior soft delete left the row (and
        its content) in memory.db and still surfaced it, with content, in the
        /agents "memories touched" debugger.

        Removes the row from `memories` and its vector from `memory_embeddings`.
        Deletes regardless of is_active so already-soft-deleted rows can be
        purged too. Any agent tool-call history that referenced this id
        (mcp_tool_calls.memory_ids_touched) degrades gracefully: the debugger
        shows the touch count but can no longer resurrect the deleted content.

        Note: SQLite leaves freed bytes in free pages until VACUUM; run a VACUUM
        as a periodic maintenance op for stronger "unrecoverable" guarantees —
        not per-delete (a full-DB rewrite is too costly to do on every call).
        """
        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "DELETE FROM memories WHERE id = ?", (memory_id,)
            )
            await db.execute(
                "DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,)
            )
            await db.commit()
            affected = cursor.rowcount > 0

        if affected:
            logger.info("memory_hard_deleted id=%s", memory_id)
            from potluck_local.services.memory_clustering import get_cluster_cache
            await get_cluster_cache().invalidate_all()
        return affected

    async def update_memory(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        category: MemoryCategory | None = None,
        confidence: float | None = None,
        project: str | None = ...,  # type: ignore[assignment]
    ) -> Memory | None:
        """Partially update an active memory's mutable fields.

        project uses a sentinel default (...) so callers can distinguish between
        "not provided" and "explicitly set to None" (which clears the scope to global).
        """
        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        existing = await self.get_memory(memory_id)
        if existing is None or not existing.is_active:
            return None

        new_content = content if content is not None else existing.content
        new_category = category if category is not None else existing.category
        new_confidence = (
            min(max(confidence, 0.0), 1.0) if confidence is not None else existing.confidence
        )
        # If project was not provided (sentinel ...), keep existing value.
        # If explicitly passed (including None), use the new value.
        if project is ...:  # type: ignore[comparison-overlap]
            new_project: str | None = existing.project
        else:
            new_project = project or None  # coerce "" -> None

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """UPDATE memories
                   SET content = ?, category = ?, confidence = ?, project = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (new_content, new_category, new_confidence, new_project,
                 _now_iso(), memory_id),
            )
            await db.commit()

        from potluck_local.services.memory_clustering import get_cluster_cache
        await get_cluster_cache().invalidate_all()

        return await self.get_memory(memory_id)

    def format_memories_for_prompt(self, memories: list[Memory]) -> str:
        """Format memories into a system-prompt section, grouped by category."""
        if not memories:
            return ""

        sections: dict[str, list[str]] = {
            "instruction": [],
            "preference": [],
            "person": [],
            "project": [],
            "fact": [],
            "decision": [],
        }

        for mem in memories:
            cat = mem.category if mem.category in sections else "fact"
            sections[cat].append(mem.content)

        lines = ["\n--- Memory Context (from previous conversations) ---"]

        if sections["instruction"]:
            lines.append("\nStanding instructions from the user:")
            lines.extend(f"- {item}" for item in sections["instruction"])

        if sections["preference"]:
            lines.append("\nUser preferences:")
            lines.extend(f"- {item}" for item in sections["preference"])

        if sections["person"]:
            lines.append("\nPeople the user has mentioned:")
            lines.extend(f"- {item}" for item in sections["person"])

        if sections["project"]:
            lines.append("\nProject context:")
            lines.extend(f"- {item}" for item in sections["project"])

        remaining = sections["fact"] + sections["decision"]
        if remaining:
            lines.append("\nRelevant context:")
            lines.extend(f"- {item}" for item in remaining)

        lines.append("\n--- End Memory Context ---")
        return "\n".join(lines)


_retriever: MemoryRetriever | None = None


def get_memory_retriever() -> MemoryRetriever:
    """Return the module-level MemoryRetriever singleton."""
    global _retriever
    if _retriever is None:
        _retriever = MemoryRetriever()
    return _retriever

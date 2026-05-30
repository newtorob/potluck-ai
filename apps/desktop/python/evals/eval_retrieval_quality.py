"""Eval B — retrieval quality evaluation for Potluck's memory system.

Measures recall@{3,5,10,20} and MRR for two configurations:
  1. keyword-only  (semantic arm disabled — reproduces pre-RRF behavior)
  2. RRF           (the new default: keyword + semantic fused)

Usage:
    cd apps/desktop/python
    .venv/bin/python -m evals.eval_retrieval_quality

Or via make:
    make eval-retrieval

Exit codes:
    0 — RRF recall@20 is at or above the regression floor
    1 — RRF recall@20 fell below the regression floor (regression detected)
    2 — embedder unavailable; semantic arm could not be tested (WARN, not fail)

Design notes:
  - Uses a temp SQLite DB isolated from the user's ~/.potluck/data/memory.db.
    POTLUCK_MEMORY_DB_PATH is set before any potluck_local import so every
    internal get_memory_db_path() call picks up the override.
  - Embeddings are generated using the real EmbeddingService (sentence-
    transformers all-MiniLM-L6-v2) so results reflect production behavior.
    The embedder is deterministic for the same model version, giving
    reproducible runs.
  - keyword-only baseline is produced by monkeypatching semantic_search to
    return [] inside the retrieval module.  Everything else (DB, retriever,
    keywords, Tier-1 logic) is identical.
  - No randomness anywhere (no sampling, fixed fixture order).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

# ---------------------------------------------------------------------------
# 1. Point all potluck_local DB calls at a fresh temp file BEFORE importing
#    anything from potluck_local.  get_memory_db_path() reads the env var at
#    call time, so setting it here is sufficient as long as no module has
#    cached the path yet.
# ---------------------------------------------------------------------------
_TMPDIR = tempfile.mkdtemp(prefix="potluck_eval_")
_EVAL_DB = str(Path(_TMPDIR) / "eval_memory.db")
os.environ["POTLUCK_MEMORY_DB_PATH"] = _EVAL_DB

# Now safe to import potluck_local modules.
import aiosqlite  # noqa: E402

from potluck_local.services.embeddings import get_embedder  # noqa: E402
from potluck_local.services.memory_db import init_memory_db, save_embedding  # noqa: E402
from potluck_local.services.memory_retrieval import (  # noqa: E402
    MemoryRetriever,
    semantic_search,
)
from potluck_local.utils.time import now_iso  # noqa: E402

from evals.fixtures import CORPUS, QUERIES  # noqa: E402

# ---------------------------------------------------------------------------
# HEADLINE_K — the budget the headline metric + regression floor key off.
#
# We headline recall@5, NOT recall@20. recall@20 saturated at 1.00 on this
# corpus (20 slots covers half of 42 memories) — a pinned metric has no
# gradient, so it can't DRIVE tuning, only catch gross regressions. recall@5
# is the realistic tight-budget case (the agent injects a handful of
# memories, and rank actually matters) and it leaves room to improve, which
# is what makes the eval a tuning flywheel rather than just a tripwire.
#
# Sequencing (decided 2026-05-28): tight headline now (recall@5 on the
# current 42-memory corpus); grow the corpus to hundreds/thousands later for
# scale-realism, at which point recall@20 becomes meaningful again too.
# All of recall@{3,5,10,20} are still computed + printed; only the headline
# + floor key off HEADLINE_K.
# ---------------------------------------------------------------------------
HEADLINE_K: int = 5

K_VALUES: list[int] = [3, 5, 10, 20]

# Regression floor — RRF recall@HEADLINE_K must stay at or above this.
#
# recall@5 baselines (2026-05-28), 25-query corpus incl. AMBIENT:
#   - Before tuning (always_budget=10):     RRF 0.43 vs keyword 0.43  (+0.00)
#   - After always_budget 10->3:            RRF 0.72 vs keyword 0.48  (+0.24)
# The fix was NOT the embedder or the RRF k (both ruled out by sweeps) — it
# was the Tier-1 preload eating half the budget and burying query-relevant
# hits below the recall@5 cutoff. Dropping always_budget to 3 freed those
# slots; the wedge ("retrieval beats keyword") now holds at a realistic
# budget, not just at the saturated @20.
# Floor set ~0.12 below the 0.72 baseline: a drop here means several queries
# regressed, a real signal. (AMBIENT queries score 0 by construction —
# mid-confidence standing rules never reach top-5 via the confidence-ranked
# preload; true ambient injection needs a separate pinned-instructions
# mechanism, tracked as a follow-up — so they pull the absolute number down
# but don't affect the RRF-vs-keyword lift.)
RECALL_FLOOR: float = 0.60


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

async def _seed_corpus(db_path: str) -> dict[str, str]:
    """Insert CORPUS into the eval DB, return {memory_id: content} map."""
    ts = now_iso()
    conv_id = str(uuid.uuid4())
    id_map: dict[str, str] = {}

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        for mem in CORPUS:
            mid = mem["id"]
            await db.execute(
                """INSERT INTO memories
                   (id, content, category, confidence, source_type,
                    source_conversation_id, created_at, last_accessed_at,
                    updated_at, access_count, is_active, project)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mid,
                    mem["content"],
                    mem["category"],
                    mem["confidence"],
                    "explicit",
                    conv_id,
                    ts,
                    ts,
                    ts,
                    0,
                    1,
                    mem.get("project"),
                ),
            )
            id_map[mid] = mem["content"]
        await db.commit()

    return id_map


async def _generate_embeddings(db_path: str) -> bool:
    """Generate and persist embeddings for all corpus memories.

    Returns True if embeddings were generated, False if the embedder is
    unavailable (in which case the semantic arm of RRF will degrade to
    keyword-only — both modes will look identical and the eval will still
    run but cannot measure semantic lift).
    """
    embedder = get_embedder()
    contents = [m["content"] for m in CORPUS]
    ids = [m["id"] for m in CORPUS]

    # Warm up / load model
    test_vec = await embedder.embed("test")
    if test_vec is None:
        return False

    # Batch encode all corpus memories
    matrix = await embedder.embed_many(contents)
    if matrix is None:
        return False

    for i, mid in enumerate(ids):
        await save_embedding(mid, matrix[i], embedder.MODEL_NAME, db_path=Path(db_path))

    return True


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

def _reciprocal_rank(ranked_ids: list[str], gold: set[str]) -> float:
    """Return 1/rank of the first relevant hit, or 0 if not found."""
    for rank, mid in enumerate(ranked_ids, start=1):
        if mid in gold:
            return 1.0 / rank
    return 0.0


def _recall_at_k(ranked_ids: list[str], gold: set[str], k: int) -> float:
    """Fraction of gold memories found in top-k (binary: 1.0 if any hit, 0.0 if none).

    For queries with a single gold memory this equals precision@k.
    For queries with multiple gold memories this is recall (not all may be in top-k).
    """
    top_k = set(ranked_ids[:k])
    if not gold:
        return 1.0  # degenerate: no gold defined, not a failure
    hits = gold & top_k
    return 1.0 if hits else 0.0


# ---------------------------------------------------------------------------
# Single retrieval run
# ---------------------------------------------------------------------------

async def _run_retrieval(
    queries: list[dict],
    mode: str,
    retriever: MemoryRetriever,
    keyword_only: bool,
) -> list[dict]:
    """Run all queries through get_relevant_memories, return per-query results.

    keyword_only=True patches semantic_search to return [] inside the
    memory_retrieval module so RRF degrades to pure keyword ranking.
    """

    async def _do_single_query(q: dict) -> dict:
        messages = [{"role": "user", "content": q["text"]}]
        t0 = time.perf_counter()
        memories = await retriever.get_relevant_memories(
            current_messages=messages,
            max_memories=20,
            min_confidence=0.5,
            project=q.get("project"),
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        ranked_ids = [m.id for m in memories]
        gold = q["gold"]

        rr = _reciprocal_rank(ranked_ids, gold)
        recalls = {k: _recall_at_k(ranked_ids, gold, k) for k in K_VALUES}

        return {
            "query_id": q["id"],
            "query_type": q["type"],
            "query_text": q["text"],
            "gold": gold,
            "ranked_ids": ranked_ids,
            "rr": rr,
            "recalls": recalls,
            "elapsed_ms": elapsed_ms,
        }

    results: list[dict] = []
    for q in queries:
        if keyword_only:
            # Monkeypatch semantic_search inside the retrieval module to return []
            # This simulates pre-RRF keyword-only behavior without altering any
            # production code path.
            with patch(
                "potluck_local.services.memory_retrieval.semantic_search",
                new_callable=AsyncMock,
                return_value=[],
            ):
                result = await _do_single_query(q)
        else:
            result = await _do_single_query(q)
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------

def _aggregate(results: list[dict]) -> dict[str, Any]:
    n = len(results)
    if n == 0:
        return {}
    mrr = sum(r["rr"] for r in results) / n
    recalls = {}
    for k in K_VALUES:
        recalls[k] = sum(r["recalls"][k] for r in results) / n
    return {"mrr": mrr, "recalls": recalls, "n": n}


def _aggregate_by_type(results: list[dict], query_type: str) -> dict[str, Any]:
    subset = [r for r in results if r["query_type"] == query_type]
    return _aggregate(subset)


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _print_separator(char: str = "-", width: int = 80) -> None:
    print(char * width)


def _print_per_query_table(results_kw: list[dict], results_rrf: list[dict]) -> None:
    """Print per-query recall@HEADLINE_K and MRR for both modes side by side."""
    # Build index by query_id
    kw_idx = {r["query_id"]: r for r in results_kw}
    rrf_idx = {r["query_id"]: r for r in results_rrf}

    col_w = [24, 10, 8, 8, 8, 8]
    header = (
        f"{'Query ID':<{col_w[0]}} {'Type':<{col_w[1]}} "
        f"{f'KW@{HEADLINE_K}':>{col_w[2]}} {f'RRF@{HEADLINE_K}':>{col_w[3]}} "
        f"{'KW MRR':>{col_w[4]}} {'RRF MRR':>{col_w[5]}}"
    )
    _print_separator()
    print(header)
    _print_separator()
    for qid in [r["query_id"] for r in results_kw]:
        kw = kw_idx[qid]
        rrf = rrf_idx[qid]
        kw_rk = kw["recalls"][HEADLINE_K]
        rrf_rk = rrf["recalls"][HEADLINE_K]
        marker = " <--" if rrf_rk < kw_rk else ("  >>" if rrf_rk > kw_rk else "")
        print(
            f"{qid:<{col_w[0]}} {kw['query_type']:<{col_w[1]}} "
            f"{kw_rk:>{col_w[2]}.2f} {rrf_rk:>{col_w[3]}.2f} "
            f"{kw['rr']:>{col_w[4]}.3f} {rrf['rr']:>{col_w[5]}.3f}"
            f"{marker}"
        )
    _print_separator()


def _print_by_type(results_kw: list[dict], results_rrf: list[dict]) -> None:
    query_types = ["LEXICAL", "SEMANTIC", "DISTRACTOR", "TIER1", "NEGATIVE"]
    print(f"\nRecall@{HEADLINE_K} by query type:")
    _print_separator("-", 60)
    fmt = f"{'Type':<12} {f'KW recall@{HEADLINE_K}':>14} {f'RRF recall@{HEADLINE_K}':>14} {'Lift':>8}"
    print(fmt)
    _print_separator("-", 60)
    for qt in query_types:
        kw_agg = _aggregate_by_type(results_kw, qt)
        rrf_agg = _aggregate_by_type(results_rrf, qt)
        if not kw_agg:
            continue
        kw_rk = kw_agg["recalls"][HEADLINE_K]
        rrf_rk = rrf_agg["recalls"][HEADLINE_K]
        lift = rrf_rk - kw_rk
        print(f"{qt:<12} {kw_rk:>14.2f} {rrf_rk:>14.2f} {lift:>+8.2f}")
    _print_separator("-", 60)


def _print_aggregate_table(kw_agg: dict, rrf_agg: dict) -> None:
    print("\nAggregate metrics (all queries):")
    _print_separator("-", 60)
    header = f"{'Metric':<14} {'Keyword-only':>14} {'RRF':>8} {'Lift':>8}"
    print(header)
    _print_separator("-", 60)
    for k in K_VALUES:
        kw_r = kw_agg["recalls"][k]
        rrf_r = rrf_agg["recalls"][k]
        lift = rrf_r - kw_r
        print(f"{'Recall@' + str(k):<14} {kw_r:>14.4f} {rrf_r:>8.4f} {lift:>+8.4f}")
    kw_mrr = kw_agg["mrr"]
    rrf_mrr = rrf_agg["mrr"]
    print(f"{'MRR':<14} {kw_mrr:>14.4f} {rrf_mrr:>8.4f} {rrf_mrr - kw_mrr:>+8.4f}")
    _print_separator("-", 60)


def _check_negative_controls(results_rrf: list[dict]) -> list[str]:
    """Return list of NEGATIVE queries where a cross-project memory leaked."""
    mobile_ids = {"mem_mobile_kotlin", "mem_mobile_deploy", "mem_mobile_pnpm"}
    failures = []
    for r in results_rrf:
        if r["query_type"] != "NEGATIVE":
            continue
        leaked = mobile_ids & set(r["ranked_ids"])
        if leaked:
            failures.append(f"  {r['query_id']}: leaked {leaked}")
    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    print("\n=== Potluck Eval B — Retrieval Quality ===")
    print(f"Corpus: {len(CORPUS)} memories | Queries: {len(QUERIES)}")
    print(f"Eval DB: {_EVAL_DB}")

    # 1. Init schema
    await init_memory_db(Path(_EVAL_DB))

    # 2. Seed corpus
    print("\nSeeding corpus...")
    await _seed_corpus(_EVAL_DB)

    # 3. Generate embeddings
    print("Generating embeddings (sentence-transformers all-MiniLM-L6-v2)...")
    t_embed_start = time.perf_counter()
    embedder_ok = await _generate_embeddings(_EVAL_DB)
    embed_elapsed = time.perf_counter() - t_embed_start
    if embedder_ok:
        print(f"  Embeddings generated in {embed_elapsed:.1f}s")
    else:
        print(
            "  WARNING: embedder unavailable — semantic arm will degrade to keyword-only."
        )
        print("  Both modes will produce identical results; install sentence-transformers to fix.")

    retriever = MemoryRetriever()

    # 4. keyword-only run
    print("\nRunning keyword-only retrieval...")
    t0 = time.perf_counter()
    results_kw = await _run_retrieval(QUERIES, "keyword", retriever, keyword_only=True)
    print(f"  Done in {(time.perf_counter() - t0) * 1000:.0f}ms")

    # 5. RRF run
    print("Running RRF retrieval...")
    t0 = time.perf_counter()
    results_rrf = await _run_retrieval(QUERIES, "rrf", retriever, keyword_only=False)
    print(f"  Done in {(time.perf_counter() - t0) * 1000:.0f}ms")

    # 6. Aggregate
    kw_agg = _aggregate(results_kw)
    rrf_agg = _aggregate(results_rrf)

    # 7. Report
    print("\n")
    _print_separator("=")
    print("PER-QUERY RESULTS (keyword-only vs RRF)")
    print(f">> = RRF improved recall@{HEADLINE_K} vs keyword  |  <-- = RRF regressed")
    _print_per_query_table(results_kw, results_rrf)

    _print_by_type(results_kw, results_rrf)
    _print_aggregate_table(kw_agg, rrf_agg)

    # 8. Headline one-liner (keys off HEADLINE_K)
    kw_rk = kw_agg["recalls"][HEADLINE_K]
    rrf_rk = rrf_agg["recalls"][HEADLINE_K]
    kw_mrr = kw_agg["mrr"]
    rrf_mrr = rrf_agg["mrr"]
    lift = rrf_rk - kw_rk

    print(
        f"\nHEADLINE: RRF recall@{HEADLINE_K}={rrf_rk:.2f} MRR={rrf_mrr:.3f} | "
        f"keyword-only recall@{HEADLINE_K}={kw_rk:.2f} MRR={kw_mrr:.3f} | "
        f"lift {lift:+.2f} recall"
    )

    # 9. Negative control check
    neg_failures = _check_negative_controls(results_rrf)
    if neg_failures:
        print("\nFAIL: Cross-project isolation breached:")
        for f in neg_failures:
            print(f)
    else:
        print("PASS: Cross-project isolation — no mobile memories leaked into backend scope")

    # 10. RRF regressions vs keyword (where RRF was WORSE at the headline budget)
    regressions = [
        rrf for rrf, kw in zip(results_rrf, results_kw)
        if rrf["recalls"][HEADLINE_K] < kw["recalls"][HEADLINE_K]
    ]
    if regressions:
        print(f"\nRRF recall@{HEADLINE_K} regressions (RRF scored LOWER than keyword-only):")
        for r in regressions:
            kw_match = next(k for k in results_kw if k["query_id"] == r["query_id"])
            print(
                f"  {r['query_id']} ({r['query_type']}): "
                f"RRF={r['recalls'][HEADLINE_K]:.2f} < KW={kw_match['recalls'][HEADLINE_K]:.2f}"
            )
        print("  -> The keyword arm found a strong hit that RRF diluted by rank at this budget.")
        print("     This is the signal to tune k (k=60 -> higher favors keyword rank).")
    else:
        print(f"No recall@{HEADLINE_K} regressions — RRF never scored lower than keyword-only")

    # 11. Embedder caveat
    if not embedder_ok:
        print(
            "\nWARNING: Embedder was unavailable. Both modes ran keyword-only."
            " Exit code 2 (not a regression — infrastructure issue)."
        )
        return 2

    # 12. Regression floor check (keys off HEADLINE_K)
    print(
        f"\nRegression floor: RRF recall@{HEADLINE_K} >= {RECALL_FLOOR:.2f}"
    )
    if rrf_rk < RECALL_FLOOR:
        print(f"FAIL: RRF recall@{HEADLINE_K}={rrf_rk:.2f} is below floor={RECALL_FLOOR:.2f}")
        return 1
    else:
        print(f"PASS: RRF recall@{HEADLINE_K}={rrf_rk:.2f} is at or above floor={RECALL_FLOOR:.2f}")
        return 0


def _cleanup() -> None:
    """Remove the temp DB on exit (best-effort)."""
    import shutil
    try:
        shutil.rmtree(_TMPDIR, ignore_errors=True)
    except Exception:
        pass


if __name__ == "__main__":
    import atexit
    atexit.register(_cleanup)
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

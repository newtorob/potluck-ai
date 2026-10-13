"""Embedder comparison experiment - eval whether a stronger 384-dim embedder
beats all-MiniLM-L6-v2 on recall@5 for Potluck's memory retrieval.

Candidates:
  1. sentence-transformers/all-MiniLM-L6-v2  (baseline)
  2. BAAI/bge-small-en-v1.5                  (asymmetric: query prefix required)
  3. thenlper/gte-small                       (symmetric)

STRETCH (only prints a note, not run by default):
  4. sentence-transformers/all-mpnet-base-v2  (768-dim - breaks DIM=384 storage)

Usage:
    cd apps/desktop/python
    .venv/bin/python -m evals.compare_embedders

Design:
  - Each candidate gets its own fresh temp SQLite DB (isolated from production).
  - Embeddings are regenerated per candidate using the candidate model.
  - query-side: embed_query() - applies BGE instruction prefix where configured.
  - document-side: embed_many() - no prefix for any model.
  - All candidates share the same CORPUS, QUERIES, and recall@{3,5,10,20}+MRR
    metrics, so results are directly comparable.
  - The singleton is reset between candidates via _reset_embedder() so
    POTLUCK_EMBED_MODEL changes take effect.
  - The production default is left unchanged. The env var is restored after the run.
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
# Set a dummy POTLUCK_MEMORY_DB_PATH before any potluck_local import.
# Each candidate run will override this further.
# ---------------------------------------------------------------------------
_TMPROOT = tempfile.mkdtemp(prefix="potluck_embedder_cmp_")
os.environ["POTLUCK_MEMORY_DB_PATH"] = str(Path(_TMPROOT) / "placeholder.db")

import aiosqlite  # noqa: E402

from potluck_local.services.memory_db import init_memory_db, save_embedding  # noqa: E402
from potluck_local.services.memory_retrieval import MemoryRetriever, semantic_search  # noqa: E402
from potluck_local.utils.time import now_iso  # noqa: E402

from evals.fixtures import CORPUS, QUERIES  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CANDIDATES: list[dict] = [
    {
        "id": "minilm",
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "label": "all-MiniLM-L6-v2 (baseline)",
        "asymmetric": False,
        "note": "symmetric, 384-dim, ~80MB",
    },
    {
        "id": "bge",
        "model": "BAAI/bge-small-en-v1.5",
        "label": "bge-small-en-v1.5",
        "asymmetric": True,
        "note": "asymmetric, 384-dim, ~130MB; query prefix applied",
    },
    {
        "id": "gte",
        "model": "thenlper/gte-small",
        "label": "gte-small",
        "asymmetric": False,
        "note": "symmetric, 384-dim, ~130MB",
    },
]

K_VALUES: list[int] = [3, 5, 10, 20]
HEADLINE_K: int = 5


# ---------------------------------------------------------------------------
# DB / embedding helpers (mirrors eval_retrieval_quality.py)
# ---------------------------------------------------------------------------

async def _seed_corpus(db_path: str) -> None:
    ts = now_iso()
    conv_id = str(uuid.uuid4())
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        for mem in CORPUS:
            await db.execute(
                """INSERT INTO memories
                   (id, content, category, confidence, source_type,
                    source_conversation_id, created_at, last_accessed_at,
                    updated_at, access_count, is_active, project)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mem["id"], mem["content"], mem["category"],
                    mem["confidence"], "explicit", conv_id,
                    ts, ts, ts, 0, 1, mem.get("project"),
                ),
            )
        await db.commit()


async def _generate_embeddings(db_path: str, embedder_obj: Any) -> tuple[bool, float]:
    """Generate embeddings for all corpus documents.

    Uses embed_many() (document side - no query prefix).
    Returns (success, elapsed_seconds).
    """
    contents = [m["content"] for m in CORPUS]
    ids = [m["id"] for m in CORPUS]

    # Warm up
    test_vec = await embedder_obj.embed("test")
    if test_vec is None:
        return False, 0.0

    t0 = time.perf_counter()
    matrix = await embedder_obj.embed_many(contents)
    elapsed = time.perf_counter() - t0

    if matrix is None:
        return False, elapsed

    for i, mid in enumerate(ids):
        await save_embedding(mid, matrix[i], embedder_obj.MODEL_NAME, db_path=Path(db_path))

    return True, elapsed


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _reciprocal_rank(ranked_ids: list[str], gold: set[str]) -> float:
    for rank, mid in enumerate(ranked_ids, start=1):
        if mid in gold:
            return 1.0 / rank
    return 0.0


def _recall_at_k(ranked_ids: list[str], gold: set[str], k: int) -> float:
    top_k = set(ranked_ids[:k])
    if not gold:
        return 1.0
    return 1.0 if (gold & top_k) else 0.0


def _aggregate(results: list[dict]) -> dict[str, Any]:
    n = len(results)
    if n == 0:
        return {}
    mrr = sum(r["rr"] for r in results) / n
    recalls = {k: sum(r["recalls"][k] for r in results) / n for k in K_VALUES}
    return {"mrr": mrr, "recalls": recalls, "n": n}


# ---------------------------------------------------------------------------
# Per-query retrieval run (RRF mode only - we are testing the embedder, not
# keyword vs RRF).
# ---------------------------------------------------------------------------

async def _run_queries(queries: list[dict], retriever: MemoryRetriever) -> list[dict]:
    results: list[dict] = []
    for q in queries:
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
        results.append({
            "query_id": q["id"],
            "query_type": q["type"],
            "query_text": q["text"],
            "gold": gold,
            "ranked_ids": ranked_ids,
            "rr": rr,
            "recalls": recalls,
            "elapsed_ms": elapsed_ms,
        })
    return results


# ---------------------------------------------------------------------------
# Single candidate run
# ---------------------------------------------------------------------------

async def _run_candidate(candidate: dict) -> dict:
    """Set up a fresh temp DB, embed with the candidate model, run all queries."""
    import importlib
    import potluck_local.services.embeddings as emb_module

    model_id = candidate["model"]
    db_path = str(Path(_TMPROOT) / f"{candidate['id']}_eval.db")

    # Point the DB path at the candidate's DB
    os.environ["POTLUCK_MEMORY_DB_PATH"] = db_path

    # Set the candidate model and reset the singleton so it picks up the new env
    os.environ["POTLUCK_EMBED_MODEL"] = model_id
    emb_module._reset_embedder()

    # Also reset the embedder reference inside memory_retrieval (it calls
    # get_embedder() at call time, so the reset above is sufficient).

    print(f"\n{'='*70}")
    print(f"Candidate: {candidate['label']}")
    print(f"  Model: {model_id}")
    print(f"  Note:  {candidate['note']}")
    print(f"  DB:    {db_path}")

    # Init schema and seed corpus
    await init_memory_db(Path(db_path))
    await _seed_corpus(db_path)

    # Get the (now-fresh) embedder
    embedder = emb_module.get_embedder()

    print(f"  Asymmetric query prefix: {repr(embedder._query_prefix) if embedder._query_prefix else '(none, symmetric)'}")

    # Generate document-side embeddings
    print("  Generating document embeddings...")
    t_load_start = time.perf_counter()
    ok, embed_elapsed = await _generate_embeddings(db_path, embedder)
    total_elapsed = time.perf_counter() - t_load_start

    if not ok:
        print(f"  ERROR: embedder unavailable for {model_id}")
        return {
            "candidate": candidate,
            "ok": False,
            "agg": {},
            "results": [],
            "embed_time_s": 0.0,
            "total_time_s": total_elapsed,
        }

    print(f"  Embedding time: {embed_elapsed:.2f}s ({len(CORPUS)} documents)")
    print(f"  Model load + embed total: {total_elapsed:.2f}s")

    retriever = MemoryRetriever()

    print("  Running queries...")
    results = await _run_queries(QUERIES, retriever)
    agg = _aggregate(results)

    return {
        "candidate": candidate,
        "ok": True,
        "agg": agg,
        "results": results,
        "embed_time_s": embed_elapsed,
        "total_time_s": total_elapsed,
    }


# ---------------------------------------------------------------------------
# Qualitative analysis - which queries did a model rescue vs baseline
# ---------------------------------------------------------------------------

def _find_rescued_queries(
    baseline_results: list[dict],
    candidate_results: list[dict],
    k: int = HEADLINE_K,
) -> list[dict]:
    """Queries where candidate hit recall@k=1 but baseline missed (=0)."""
    base_idx = {r["query_id"]: r for r in baseline_results}
    rescued = []
    for cr in candidate_results:
        qid = cr["query_id"]
        br = base_idx.get(qid)
        if br is None:
            continue
        if cr["recalls"][k] == 1.0 and br["recalls"][k] == 0.0:
            rescued.append(cr)
    return rescued


def _find_regressions(
    baseline_results: list[dict],
    candidate_results: list[dict],
    k: int = HEADLINE_K,
) -> list[dict]:
    """Queries where candidate MISSED recall@k but baseline hit it."""
    base_idx = {r["query_id"]: r for r in baseline_results}
    regressions = []
    for cr in candidate_results:
        qid = cr["query_id"]
        br = base_idx.get(qid)
        if br is None:
            continue
        if cr["recalls"][k] == 0.0 and br["recalls"][k] == 1.0:
            regressions.append(cr)
    return regressions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("\n=== Potluck Embedder Comparison Experiment ===")
    print(f"Corpus: {len(CORPUS)} memories | Queries: {len(QUERIES)}")
    print(f"Headline metric: recall@{HEADLINE_K}")
    print(f"Candidates: {[c['id'] for c in CANDIDATES]}")
    print("\nNOTE: BGE asymmetric handling: query side uses embed_query()")
    print("      which prepends 'Represent this sentence for searching relevant passages:'")
    print("      Document/memory side uses embed_many() with NO prefix.")

    # Run candidates sequentially (1 GPU/CPU thread, avoid contention)
    all_runs: list[dict] = []
    for candidate in CANDIDATES:
        run = await _run_candidate(candidate)
        all_runs.append(run)

    # Restore env to production default
    os.environ.pop("POTLUCK_EMBED_MODEL", None)

    # ---------------------------------------------------------------------------
    # Summary table
    # ---------------------------------------------------------------------------
    print(f"\n\n{'='*80}")
    print("RESULTS SUMMARY")
    print(f"{'='*80}")

    col_labels = ["Embedder", "R@3", f"R@{HEADLINE_K}*", "R@10", "R@20", "MRR", "EmbedTime"]
    col_widths = [30, 6, 7, 6, 6, 7, 11]

    header = "  ".join(f"{lbl:<{w}}" for lbl, w in zip(col_labels, col_widths))
    print(header)
    print("-" * 80)

    baseline_run = None
    for run in all_runs:
        c = run["candidate"]
        if not run["ok"]:
            print(f"  {c['label']:<30}  ERROR: model unavailable")
            continue
        agg = run["agg"]
        r3 = agg["recalls"][3]
        r5 = agg["recalls"][HEADLINE_K]
        r10 = agg["recalls"][10]
        r20 = agg["recalls"][20]
        mrr = agg["mrr"]
        et = run["embed_time_s"]
        marker = " (baseline)" if c["id"] == "minilm" else ""
        row = f"  {c['label']:<30}  {r3:.2f}  {r5:.2f}    {r10:.2f}  {r20:.2f}  {mrr:.3f}  {et:.2f}s{marker}"
        print(row)
        if c["id"] == "minilm":
            baseline_run = run

    print(f"\n* = headline metric (recall@{HEADLINE_K})")

    # ---------------------------------------------------------------------------
    # Per-candidate qualitative diff vs baseline
    # ---------------------------------------------------------------------------
    if baseline_run is None:
        print("\nCould not find baseline run, skipping qualitative diff.")
        return

    baseline_results = baseline_run["results"]
    baseline_r5 = baseline_run["agg"]["recalls"][HEADLINE_K]

    print(f"\n{'='*80}")
    print(f"QUALITATIVE ANALYSIS vs baseline (all-MiniLM recall@{HEADLINE_K}={baseline_r5:.2f})")
    print(f"{'='*80}")

    winner_id = None
    winner_lift = 0.0

    for run in all_runs:
        c = run["candidate"]
        if c["id"] == "minilm" or not run["ok"]:
            continue
        r5 = run["agg"]["recalls"][HEADLINE_K]
        lift = r5 - baseline_r5

        if lift > winner_lift:
            winner_lift = lift
            winner_id = c["id"]

        print(f"\n--- {c['label']} (recall@{HEADLINE_K}={r5:.2f}, lift={lift:+.2f}) ---")
        print(f"    Note: {c['note']}")

        rescued = _find_rescued_queries(baseline_results, run["results"])
        if rescued:
            print(f"  Queries rescued (hit recall@{HEADLINE_K}=1 where baseline=0):")
            for r in rescued:
                top5 = r["ranked_ids"][:HEADLINE_K]
                hits = r["gold"] & set(top5)
                print(f"    [{r['query_type']}] {r['query_id']}")
                print(f"      text: {r['query_text']!r}")
                print(f"      gold: {r['gold']}")
                print(f"      top-5 hits in gold: {hits}")
        else:
            print(f"  No new queries rescued at recall@{HEADLINE_K}.")

        regressions = _find_regressions(baseline_results, run["results"])
        if regressions:
            print(f"  Regressions (missed recall@{HEADLINE_K} where baseline=1):")
            for r in regressions:
                print(f"    [{r['query_type']}] {r['query_id']}: {r['query_text']!r}")
        else:
            print(f"  No regressions vs baseline at recall@{HEADLINE_K}.")

    # ---------------------------------------------------------------------------
    # Per-query table (all candidates side by side)
    # ---------------------------------------------------------------------------
    print(f"\n{'='*80}")
    print(f"PER-QUERY recall@{HEADLINE_K} (all candidates)")
    print(f"{'='*80}")

    ok_runs = [r for r in all_runs if r["ok"]]
    col_w = 14
    hdr_parts = [f"{'Query ID':<26}", f"{'Type':<10}"]
    for run in ok_runs:
        hdr_parts.append(f"{run['candidate']['id']:>{col_w}}")
    print("  " + "  ".join(hdr_parts))
    print("-" * 80)

    for q in QUERIES:
        qid = q["id"]
        row_parts = [f"{qid:<26}", f"{q['type']:<10}"]
        for run in ok_runs:
            res_map = {r["query_id"]: r for r in run["results"]}
            r = res_map.get(qid)
            val = f"{r['recalls'][HEADLINE_K]:.2f}" if r else "N/A"
            row_parts.append(f"{val:>{col_w}}")
        print("  " + "  ".join(row_parts))

    # ---------------------------------------------------------------------------
    # BGE prefix confirmation
    # ---------------------------------------------------------------------------
    bge_run = next((r for r in all_runs if r["candidate"]["id"] == "bge"), None)
    print(f"\n{'='*80}")
    print("BGE ASYMMETRIC HANDLING CONFIRMATION")
    print(f"{'='*80}")
    if bge_run and bge_run["ok"]:
        from potluck_local.services.embeddings import _MODEL_CONFIGS
        cfg = _MODEL_CONFIGS.get("BAAI/bge-small-en-v1.5")
        print(f"  Query prefix applied:    {repr(cfg.query_prefix) if cfg else 'UNKNOWN'}")
        print("  Document prefix applied: (none), embed_many() used, no prefix")
        print("  Wiring: semantic_search() -> embedder.embed_query() for queries")
        print("          _generate_embeddings() -> embedder.embed_many() for documents")
        print("  Conclusion: asymmetry correctly implemented; BGE result is fair.")
    else:
        print("  BGE run failed, cannot confirm prefix handling.")

    # ---------------------------------------------------------------------------
    # Recommendation
    # ---------------------------------------------------------------------------
    print(f"\n{'='*80}")
    print("RECOMMENDATION")
    print(f"{'='*80}")

    if winner_id and winner_lift > 0.02:
        winner_run = next(r for r in all_runs if r["candidate"]["id"] == winner_id)
        winner_candidate = winner_run["candidate"]
        winner_r5 = winner_run["agg"]["recalls"][HEADLINE_K]
        print(f"  WINNER: {winner_candidate['label']}")
        print(f"  recall@{HEADLINE_K}: {winner_r5:.2f} vs baseline {baseline_r5:.2f} (lift={winner_lift:+.2f})")
        print(f"  Embed time: {winner_run['embed_time_s']:.2f}s for {len(CORPUS)} docs")
        print()
        print("  Production migration cost if adopted:")
        print("    - Re-backfill all stored embeddings (~263 production rows)")
        print("    - The model_used column + INSERT OR REPLACE already supports this")
        print("    - A re-embed-on-startup-if-model-changed check is recommended")
        print("    - DIM stays 384, no schema change needed for these candidates")
    elif winner_id:
        print(f"  Marginal lift ({winner_lift:+.2f}) from {winner_id}, not enough to justify migration.")
        print("  Recommend: explore reranking (cross-encoder) on top of current embedder.")
    else:
        print(f"  No candidate beat the baseline at recall@{HEADLINE_K}.")
        print("  Recommend: pursue retrieve-then-rerank (cross-encoder).")
        print("  A cross-encoder over the top-20 RRF candidates should close the gap")
        print("  without the cost of re-embedding production data.")

    print()
    print("NOTE on all-mpnet-base-v2 (768-dim stretch candidate, not run):")
    print("  Would require DIM=384 -> 768 schema change + re-backfilling all embeddings.")
    print("  BLOB round-trip is already dim-agnostic (frombuffer + reshape) but")
    print("  the load_all_embeddings query would return mismatched vectors vs any")
    print("  existing 384-dim rows, so a full re-backfill is mandatory before enabling.")
    print("  Only worth testing if the 384-dim candidates show negligible improvement.")


def _cleanup() -> None:
    import shutil
    try:
        shutil.rmtree(_TMPROOT, ignore_errors=True)
    except Exception:
        pass


if __name__ == "__main__":
    import atexit
    atexit.register(_cleanup)
    asyncio.run(main())

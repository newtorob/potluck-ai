"""LongMemEval adapter for Potluck's memory retriever.

Maps LongMemEval's session-based corpus onto Potluck's memory store, runs
retrieval via get_relevant_memories() / semantic_search(), and returns ranked
session IDs in the format LongMemEval's eval_utils expects.

Design notes:

  Representation mismatch:
    LongMemEval stores evidence as multi-turn chat sessions (each session = a
    list of {role, content} turns). Potluck stores extracted facts as atomic
    memory rows (one sentence, one category).

    We do NOT run memory extraction (which would require the LLM). Instead we
    index each session as a single memory whose content is the concatenated
    user turns from that session. This is the "flat dense" approach — the same
    strategy LongMemEval's own flat-contriever / flat-bm25 baselines use. It
    keeps the adapter dependency-free (no LLM needed) and makes the comparison
    fair against those baselines.

  Isolation:
    Each eval instance gets a fresh temporary SQLite file. POTLUCK_MEMORY_DB_PATH
    is set before any potluck_local import to redirect all internal DB calls.
    The user's real ~/.potluck/ store is never touched.

  Read-only on services/:
    This file does not modify any file under apps/desktop/python/potluck_local/services/.
    It calls the retriever's public API (semantic_search, get_relevant_memories)
    via normal imports.

  Retrieval mode:
    We use semantic_search() directly rather than get_relevant_memories(). Reason:
    get_relevant_memories() is designed for chat-session context injection — it
    extracts keywords from the last 3 user messages, applies Tier-0/Tier-1
    ambient preloading, and writes access metadata. For a standalone retrieval
    benchmark we want:
      a) raw cosine similarity ranking (no ambient preloading to bias results)
      b) to pass the question text directly as the query
      c) to avoid writing access_log rows into the temp DB

    semantic_search(query, project=None, limit=k, min_confidence=0.0) gives us
    exactly that: embed the question, rank all session memories by cosine sim,
    return top-k with scores.

  Known gap (documented, not fixed):
    get_relevant_memories() RRF + Tier-1 preloading is NOT exercised here. That
    path is covered by the internal Eval B (make eval-retrieval). LongMemEval
    does not carry the Tier-0/Tier-1 ambient memory concept (it has no pinned
    preferences), so running get_relevant_memories() here would add noise
    (the always_budget slots would be wasted on random corpus rows) without
    measuring anything real. If we later extend LongMemEval with preference
    questions that map onto Potluck's Tier-1, we can switch retrieval modes.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: redirect all potluck_local DB calls to a temp file BEFORE any
# potluck_local import. The env var is set here; get_memory_db_path() reads
# it at call time, so this is safe even if potluck_local is already imported
# elsewhere — the singleton will be updated by set_db_path() if needed.
# ---------------------------------------------------------------------------

def _setup_tmpdir() -> tuple[str, str]:
    """Return (tmpdir, db_path) for an isolated eval run."""
    tmpdir = tempfile.mkdtemp(prefix="potluck_bench_")
    db_path = str(Path(tmpdir) / "bench_memory.db")
    return tmpdir, db_path


# ---------------------------------------------------------------------------
# Potluck sidecar on sys.path — the adapter is called from the repo root, but
# the potluck_local package lives in apps/desktop/python. We add it here so
# the adapter can be run from bench/run_longmemeval.py without requiring the
# caller to manage PYTHONPATH.
# ---------------------------------------------------------------------------
def _ensure_sidecar_on_path() -> None:
    sidecar_root = str(
        Path(__file__).resolve().parent.parent.parent
        / "apps" / "desktop" / "python"
    )
    if sidecar_root not in sys.path:
        sys.path.insert(0, sidecar_root)


_ensure_sidecar_on_path()


# ---------------------------------------------------------------------------
# Session → memory content representation
# ---------------------------------------------------------------------------

def session_to_memory_content(session: list[dict], max_chars: int = 2000) -> str:
    """Flatten a session's user turns into a single indexable string.

    We concatenate user turns only (not assistant turns) because:
    - User turns carry the factual content LongMemEval tests
    - Assistant turns are generic and add noise for embedding-based retrieval
    - Keeping only user turns reduces token count for the embedder

    Truncated at max_chars to avoid blowing up embedding time on long sessions.
    The oracle split sessions are short (median 2 sessions, few turns each),
    so truncation rarely fires.
    """
    parts: list[str] = []
    for turn in session:
        if turn.get("role") == "user":
            content = turn.get("content", "").strip()
            if content:
                parts.append(content)
    text = " ".join(parts)
    return text[:max_chars]


# ---------------------------------------------------------------------------
# Per-instance retrieval harness
# ---------------------------------------------------------------------------

async def _run_instance(
    question: str,
    haystack_sessions: list[list[dict]],
    haystack_session_ids: list[str],
    answer_session_ids: list[str],
    k: int,
    db_path: str,
) -> dict:
    """Seed the corpus, embed, retrieve, return ranked ids + latency.

    This is the inner async loop for one LongMemEval instance. A fresh DB is
    passed in; the caller owns its lifecycle.
    """
    import numpy as np  # noqa: F401 — imported for type checking only
    import aiosqlite

    from potluck_local.services.embeddings import get_embedder
    from potluck_local.services.memory_db import init_memory_db, save_embedding
    from potluck_local.services.memory_retrieval import semantic_search
    from potluck_local.utils.time import now_iso

    # 1. Ensure schema exists (idempotent)
    await init_memory_db(Path(db_path))

    # 2. Insert each haystack session as one memory row
    ts = now_iso()
    conv_id = str(uuid.uuid4())
    memory_ids: list[str] = []

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        for sess_id, session in zip(haystack_session_ids, haystack_sessions):
            content = session_to_memory_content(session)
            if not content:
                content = f"[empty session {sess_id}]"
            mid = sess_id  # use the session id as memory id for easy mapping
            await db.execute(
                """INSERT OR REPLACE INTO memories
                   (id, content, category, confidence, source_type,
                    source_conversation_id, created_at, last_accessed_at,
                    updated_at, access_count, is_active, project)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                # source_type must be one of the SourceType Literal values:
                # 'extracted' | 'explicit' | 'agent_state'. Using 'explicit'
                # because these are directly-provided sessions, not LLM-extracted.
                (mid, content, "fact", 1.0, "explicit",
                 conv_id, ts, ts, ts, 0, 1, None),
            )
            memory_ids.append(mid)
        await db.commit()

    # 3. Embed all sessions (batch)
    embedder = get_embedder()
    contents = [session_to_memory_content(s) for s in haystack_sessions]
    matrix = await embedder.embed_many(contents)
    embedder_ok = matrix is not None

    if embedder_ok:
        for i, mid in enumerate(memory_ids):
            await save_embedding(mid, matrix[i], embedder.MODEL_NAME, db_path=Path(db_path))

    # 4. Retrieve: time the semantic_search call
    t0 = time.perf_counter()
    if embedder_ok:
        pairs = await semantic_search(
            query=question,
            project=None,
            limit=k,
            min_confidence=0.0,
            include_pinned=False,
        )
        ranked_ids = [m.id for m, _ in pairs]
    else:
        # Embedder unavailable — fall back to ordering by insertion order (random baseline).
        # This will score poorly; the caller reports embedder_ok=False so the user knows.
        ranked_ids = memory_ids[:k]
    elapsed_ms = (time.perf_counter() - t0) * 1000

    # 5. Compute retrieval metrics using LongMemEval's eval_utils definitions.
    # evaluate_retrieval() expects: rankings (list of indices into corpus_ids),
    # correct_docs (set of answer session ids), corpus_ids (list of all session ids).
    # We translate our ranked_ids into index-space for eval_utils compatibility.
    id_to_idx = {mid: i for i, mid in enumerate(memory_ids)}
    # Pad rankings if semantic_search returned fewer than k (e.g. tiny corpus)
    ranked_indices = [id_to_idx[rid] for rid in ranked_ids if rid in id_to_idx]
    # Fill remaining slots with unseen indices (they won't match correct_docs)
    seen = set(ranked_indices)
    for i in range(len(memory_ids)):
        if len(ranked_indices) >= k:
            break
        if i not in seen:
            ranked_indices.append(i)

    correct_docs = set(answer_session_ids)
    corpus_ids_list = memory_ids  # ordered list for index mapping

    # Inline the LongMemEval metric logic (avoids importing from eval_utils which
    # uses numpy.asfarray deprecated in NumPy 2.0).
    recalled_at_k = set(corpus_ids_list[i] for i in ranked_indices[:k])
    recall_any = float(any(d in recalled_at_k for d in correct_docs))
    recall_all = float(all(d in recalled_at_k for d in correct_docs))

    # nDCG@k
    relevances = [1 if corpus_ids_list[i] in correct_docs else 0 for i in range(len(corpus_ids_list))]
    sorted_rel = [relevances[i] for i in ranked_indices[:k]]
    ideal_rel = sorted(relevances, reverse=True)

    def _dcg(rels: list[int], k: int) -> float:
        import math
        rels = rels[:k]
        if not rels:
            return 0.0
        return rels[0] + sum(r / math.log2(i + 2) for i, r in enumerate(rels[1:], start=1))

    ideal_dcg = _dcg(ideal_rel, k)
    actual_dcg = _dcg(sorted_rel, k)
    ndcg = actual_dcg / ideal_dcg if ideal_dcg > 0 else 0.0

    return {
        "ranked_ids": ranked_ids,
        "ranked_all": memory_ids,  # full ordered corpus for post-hoc analysis
        "recall_any": recall_any,
        "recall_all": recall_all,
        "ndcg": ndcg,
        "elapsed_ms": elapsed_ms,
        "embedder_ok": embedder_ok,
        "n_sessions": len(haystack_sessions),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_longmemeval(
    dataset: list[dict],
    k: int = 10,
    max_instances: int | None = None,
    verbose: bool = False,
) -> dict:
    """Run Potluck retriever against LongMemEval oracle dataset.

    Args:
        dataset: parsed longmemeval_oracle.json (list of instance dicts)
        k: retrieval cutoff for recall/nDCG computation
        max_instances: if set, cap the run to the first N instances (dev mode)
        verbose: print per-instance progress

    Returns a dict with:
        per_instance: list of per-instance result dicts
        aggregate: {recall_any, recall_all, ndcg, p50_ms, p95_ms, total_ms}
        by_type: {question_type: aggregate}
        embedder_ok: bool (False means semantic arm was unavailable)
        k: the cutoff used
        n_instances: total instances evaluated
    """
    instances = dataset[:max_instances] if max_instances else dataset

    all_results: list[dict] = []
    latencies: list[float] = []

    for i, instance in enumerate(instances):
        tmpdir, db_path = _setup_tmpdir()
        # Redirect DB for this instance
        os.environ["POTLUCK_MEMORY_DB_PATH"] = db_path

        # Reset the embedder singleton's internal DB-path cache (it reads env
        # at embed time, not at load time, so this is sufficient). We also
        # reset the retrieval module's module-level path cache if any.
        # The simplest approach: just let the env var do the work, since
        # get_memory_db_path() reads it fresh on every call.

        try:
            result = await _run_instance(
                question=instance["question"],
                haystack_sessions=instance["haystack_sessions"],
                haystack_session_ids=instance["haystack_session_ids"],
                answer_session_ids=instance["answer_session_ids"],
                k=k,
                db_path=db_path,
            )
            result["question_id"] = instance["question_id"]
            result["question_type"] = instance["question_type"]
            result["question"] = instance["question"]
            result["answer"] = instance.get("answer", "")
            all_results.append(result)
            latencies.append(result["elapsed_ms"])
        except Exception as exc:
            # Log failure but keep going — one bad instance shouldn't abort
            all_results.append({
                "question_id": instance["question_id"],
                "question_type": instance["question_type"],
                "error": str(exc),
                "recall_any": 0.0,
                "recall_all": 0.0,
                "ndcg": 0.0,
                "elapsed_ms": 0.0,
                "embedder_ok": False,
                "n_sessions": 0,
            })
            if verbose:
                print(f"  ERROR instance {instance['question_id']}: {exc}")
        finally:
            # Clean up temp DB
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

        if verbose and (i + 1) % 50 == 0:
            done = [r for r in all_results if "error" not in r]
            cur_recall = sum(r["recall_any"] for r in done) / len(done) if done else 0
            print(f"  [{i+1}/{len(instances)}] recall_any@{k}={cur_recall:.3f} running avg")

    # Aggregate
    valid = [r for r in all_results if "error" not in r]
    n = len(valid)

    def _mean(key: str) -> float:
        return sum(r[key] for r in valid) / n if n > 0 else 0.0

    import statistics
    sorted_lat = sorted(r["elapsed_ms"] for r in valid)
    p50_ms = statistics.median(sorted_lat) if sorted_lat else 0.0
    p95_ms = sorted_lat[int(len(sorted_lat) * 0.95)] if sorted_lat else 0.0

    aggregate = {
        "recall_any": _mean("recall_any"),
        "recall_all": _mean("recall_all"),
        "ndcg": _mean("ndcg"),
        "p50_ms": p50_ms,
        "p95_ms": p95_ms,
        "total_ms": sum(r["elapsed_ms"] for r in valid),
        "n_valid": n,
        "n_errors": len(all_results) - n,
    }

    # Aggregate by question_type
    qtypes = sorted(set(r.get("question_type", "unknown") for r in valid))
    by_type: dict[str, dict] = {}
    for qt in qtypes:
        subset = [r for r in valid if r.get("question_type") == qt]
        m = len(subset)
        by_type[qt] = {
            "recall_any": sum(r["recall_any"] for r in subset) / m,
            "recall_all": sum(r["recall_all"] for r in subset) / m,
            "ndcg": sum(r["ndcg"] for r in subset) / m,
            "n": m,
        }

    embedder_ok = all(r.get("embedder_ok", False) for r in valid)

    return {
        "per_instance": all_results,
        "aggregate": aggregate,
        "by_type": by_type,
        "embedder_ok": embedder_ok,
        "k": k,
        "n_instances": len(instances),
    }

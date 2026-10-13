"""always_budget sweep - find the Tier-1 slot allocation that maximizes
SEMANTIC recall@5 without collapsing TIER1 recall.

By default get_relevant_memories reserves max_memories//2 (=10 of 20) slots for
the Tier-1 preference+instruction preload, which buries query-relevant Tier-2
hits below the recall@5 cutoff. Neither the RRF k constant nor the embedder
moves recall@5, so this reservation is the structural lever.

This sweep varies POTLUCK_ALWAYS_BUDGET (read per-call by get_relevant_memories)
and prints recall@5 overall + by query type, so the tradeoff is visible:
lowering it should lift SEMANTIC/DISTRACTOR while TIER1 stays healthy.

Run:  cd apps/desktop/python && .venv/bin/python -m evals.sweep_always_budget
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from evals.eval_retrieval_quality import (  # noqa: E402
    _EVAL_DB,
    _aggregate,
    _aggregate_by_type,
    _generate_embeddings,
    _run_retrieval,
    _seed_corpus,
    init_memory_db,
)
from evals.fixtures import QUERIES  # noqa: E402
from potluck_local.services.memory_retrieval import MemoryRetriever  # noqa: E402

BUDGET_SWEEP = [0, 2, 3, 5, 8, 10]
TYPES = ["LEXICAL", "SEMANTIC", "DISTRACTOR", "AMBIENT", "TIER1", "NEGATIVE"]


async def main() -> int:
    print("\n=== always_budget sweep (headline: recall@5, by query type) ===")
    print("max_memories=20 (fixed). always_budget = Tier-1 (pref+instruction) slots.\n")
    await init_memory_db(Path(_EVAL_DB))
    await _seed_corpus(_EVAL_DB)
    print("Generating embeddings once...")
    if not await _generate_embeddings(_EVAL_DB):
        print("Embedder unavailable, aborting (semantic arm needed).")
        return 2

    retriever = MemoryRetriever()

    rows = []
    for b in BUDGET_SWEEP:
        os.environ["POTLUCK_ALWAYS_BUDGET"] = str(b)
        results = await _run_retrieval(QUERIES, f"ab{b}", retriever, keyword_only=False)
        overall = _aggregate(results)
        by_type = {t: _aggregate_by_type(results, t) for t in TYPES}
        rows.append((b, overall, by_type))
    os.environ.pop("POTLUCK_ALWAYS_BUDGET", None)

    # --- report: recall@5 by type, one row per budget ---
    hdr = f"{'always_budget':<14}{'OVERALL':>9}" + "".join(f"{t:>11}" for t in TYPES)
    print(hdr)
    print("-" * len(hdr))
    for b, overall, by_type in rows:
        line = f"{b:<14}{overall['recalls'][5]:>9.2f}"
        for t in TYPES:
            agg = by_type[t]
            val = f"{agg['recalls'][5]:.2f}" if agg else "  -"
            line += f"{val:>11}"
        print(line)
    print("-" * len(hdr))
    print("(values are recall@5)\n")

    # --- pick the winner: maximize SEMANTIC+DISTRACTOR recall@5 subject to
    #     TIER1 recall@5 staying at its max (don't sacrifice standing prefs) ---
    tier1_max = max(
        (by_type["TIER1"]["recalls"][5] if by_type["TIER1"] else 0.0)
        for _, _, by_type in rows
    )
    best = None
    for b, overall, by_type in rows:
        t1 = by_type["TIER1"]["recalls"][5] if by_type["TIER1"] else 0.0
        if t1 < tier1_max:
            continue  # this budget hurt standing-preference recall - disqualify
        sem = by_type["SEMANTIC"]["recalls"][5] if by_type["SEMANTIC"] else 0.0
        dist = by_type["DISTRACTOR"]["recalls"][5] if by_type["DISTRACTOR"] else 0.0
        score = sem + dist  # favor the query-relevance types
        if best is None or score > best[1] or (score == best[1] and overall["recalls"][5] > best[2]):
            best = (b, score, overall["recalls"][5])

    if best:
        b = best[0]
        bt = next(bt for bb, _, bt in rows if bb == b)
        ov = next(ov for bb, ov, _ in rows if bb == b)
        print(f"BEST always_budget={b}: keeps TIER1 recall@5 at max ({tier1_max:.2f}) "
              f"while maximizing query-relevant types.")
        print(f"  overall recall@5={ov['recalls'][5]:.2f}  "
              f"SEMANTIC={bt['SEMANTIC']['recalls'][5]:.2f}  "
              f"DISTRACTOR={bt['DISTRACTOR']['recalls'][5]:.2f}  "
              f"TIER1={bt['TIER1']['recalls'][5]:.2f}")
        print(f"\nIf this beats the always_budget=10 baseline, set POTLUCK_ALWAYS_BUDGET={b} "
              "as the new default in memory_retrieval.py, re-run eval_retrieval_quality, "
              "and raise the recall@5 regression floor to match.")
    return 0


if __name__ == "__main__":
    import atexit
    from evals.eval_retrieval_quality import _cleanup
    atexit.register(_cleanup)
    sys.exit(asyncio.run(main()))

"""RRF k-constant sweep — find the k that maximizes recall@5.

Reuses Eval B's corpus, embeddings, retrieval, and scoring. Seeds + embeds
ONCE, then re-runs RRF retrieval across a range of POTLUCK_RRF_K values
(the constant get_relevant_memories now reads per-call from the env), so we
can see how the headline metric moves with k.

Run:  cd apps/desktop/python && .venv/bin/python -m evals.sweep_rrf_k

This is the first eval-driven tuning loop. The point: at a tight budget
(recall@5) the v1 default k=60 produced ZERO lift over keyword, because
1/(60+rank) is nearly flat — strong hits can't dominate. Lower k should let
the semantically-rescued memories climb into the top-5.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Reuse all of Eval B's machinery.
from evals.eval_retrieval_quality import (  # noqa: E402
    _EVAL_DB,
    _aggregate,
    _generate_embeddings,
    _run_retrieval,
    init_memory_db,
)
from evals.fixtures import QUERIES  # noqa: E402
from potluck_local.services.memory_retrieval import MemoryRetriever  # noqa: E402
from evals.eval_retrieval_quality import _seed_corpus  # noqa: E402

K_SWEEP = [1, 2, 5, 10, 20, 40, 60]


async def main() -> int:
    print("\n=== RRF k-sweep (headline: recall@5) ===")
    await init_memory_db(Path(_EVAL_DB))
    await _seed_corpus(_EVAL_DB)
    print("Generating embeddings once...")
    if not await _generate_embeddings(_EVAL_DB):
        print("Embedder unavailable — sweep is meaningless without the semantic arm. Aborting.")
        return 2

    retriever = MemoryRetriever()

    # Keyword-only baseline (k-independent).
    kw = _aggregate(await _run_retrieval(QUERIES, "kw", retriever, keyword_only=True))

    rows = []
    for k in K_SWEEP:
        os.environ["POTLUCK_RRF_K"] = str(k)
        agg = _aggregate(await _run_retrieval(QUERIES, f"rrf_k{k}", retriever, keyword_only=False))
        rows.append((k, agg))
    os.environ.pop("POTLUCK_RRF_K", None)  # restore default

    # --- report ---
    print(f"\n{'config':<14}{'recall@3':>10}{'recall@5':>10}{'recall@10':>11}{'recall@20':>11}{'MRR':>8}")
    print("-" * 64)
    print(f"{'keyword-only':<14}{kw['recalls'][3]:>10.2f}{kw['recalls'][5]:>10.2f}"
          f"{kw['recalls'][10]:>11.2f}{kw['recalls'][20]:>11.2f}{kw['mrr']:>8.3f}")
    print("-" * 64)
    best_k, best_r5 = None, -1.0
    for k, agg in rows:
        r5 = agg["recalls"][5]
        flag = ""
        if r5 > best_r5:
            best_r5, best_k = r5, k
        print(f"{'RRF k='+str(k):<14}{agg['recalls'][3]:>10.2f}{r5:>10.2f}"
              f"{agg['recalls'][10]:>11.2f}{agg['recalls'][20]:>11.2f}{agg['mrr']:>8.3f}{flag}")
    print("-" * 64)

    # mark winner
    print(f"\nBEST recall@5: k={best_k} → recall@5={best_r5:.2f} "
          f"(keyword-only baseline {kw['recalls'][5]:.2f}, lift {best_r5 - kw['recalls'][5]:+.2f})")
    if best_r5 <= kw['recalls'][5]:
        print("NOTE: no k beat keyword-only at recall@5 — ranking, not fusion-k, is the bottleneck.")
        print("      Next lever: a rerank pass over the fused candidates, or a better embedder.")
    else:
        print(f"ACTION: set POTLUCK_RRF_K={best_k} as the new default in memory_retrieval.py,")
        print("        re-run eval_retrieval_quality, and raise the regression floor to match.")
    return 0


if __name__ == "__main__":
    import atexit
    from evals.eval_retrieval_quality import _cleanup
    atexit.register(_cleanup)
    sys.exit(asyncio.run(main()))

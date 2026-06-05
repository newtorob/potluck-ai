"""LongMemEval benchmark runner for Potluck's memory retriever.

Usage (from repo root via make bench):
    apps/desktop/python/.venv/bin/python bench/run_longmemeval.py \
        [--data bench/longmemeval/data/longmemeval_oracle.json] \
        [--k 10] \
        [--max-instances N]      # omit for full 500-instance run
        [--out bench/results/<timestamp>/]

Exits 0 on success. Writes:
    bench/results/<timestamp>/longmemeval_results.json   -- full per-instance data
    bench/results/<timestamp>/longmemeval_summary.txt    -- human-readable summary

The script does NOT auto-update docs/benchmarks.md. Rob reviews the numbers
and updates the table manually before tagging a release.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Sidecar on path (same trick as the adapter)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SIDECAR = _REPO_ROOT / "apps" / "desktop" / "python"
if str(_SIDECAR) not in sys.path:
    sys.path.insert(0, str(_SIDECAR))

# Set a default DB path BEFORE importing potluck_local so the initial singleton
# doesn't point at ~/.potluck/data/memory.db. The adapter will override this
# per-instance anyway, but having a no-op temp path here is a safety net.
import tempfile as _tf
_global_tmpdir = _tf.mkdtemp(prefix="potluck_bench_global_")
os.environ.setdefault("POTLUCK_MEMORY_DB_PATH", str(Path(_global_tmpdir) / "unused.db"))

from bench.adapters.potluck_retriever import run_longmemeval  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run LongMemEval against Potluck retriever")
    p.add_argument(
        "--data",
        default=str(_REPO_ROOT / "bench" / "longmemeval" / "data" / "longmemeval_oracle.json"),
        help="Path to longmemeval_oracle.json",
    )
    p.add_argument("--k", type=int, default=10, help="Retrieval cutoff for recall/nDCG")
    p.add_argument(
        "--max-instances",
        type=int,
        default=None,
        help="Cap at N instances for dev runs. Omit for full 500-instance run.",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Output directory. Defaults to bench/results/<timestamp>/",
    )
    p.add_argument("--verbose", action="store_true", help="Print per-50-instance progress")
    return p.parse_args()


def _make_out_dir(base: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = base / "results" / ts
    out.mkdir(parents=True, exist_ok=True)
    return out


_SPLIT_DESCRIPTIONS = {
    "longmemeval_oracle": "longmemeval_oracle (500 questions, evidence sessions only)",
    "longmemeval_s":      "longmemeval_s (500 questions, ~48 sessions/instance with filler noise)",
    "longmemeval_m":      "longmemeval_m (500 questions, ~500 sessions/instance with heavy filler noise)",
}

_SPLIT_NOTES = {
    "longmemeval_oracle": (
        "The oracle split uses evidence sessions only — no filler noise. "
        "recall_any@10 is structurally bounded near 1.0; the meaningful "
        "ranking signal is recall_all@1."
    ),
    "longmemeval_s": (
        "The S split mixes ~48 sessions per question with the gold "
        "evidence session(s) hidden among them. This is the split Zep "
        "and other systems benchmark on for comparable accuracy numbers."
    ),
    "longmemeval_m": (
        "The M split uses ~500 sessions per question — extreme filler "
        "noise. Tests retrieval at scale."
    ),
}


def _split_label(data_path: Path) -> str:
    """Derive the split label from the data filename, e.g. 'longmemeval_s'."""
    stem = data_path.stem.replace("_cleaned", "")
    return stem if stem.startswith("longmemeval_") else "longmemeval_oracle"


def _format_summary(results: dict, wall_seconds: float, split: str) -> str:
    agg = results["aggregate"]
    by_type = results["by_type"]
    k = results["k"]
    n = results["n_instances"]
    embedder_ok = results["embedder_ok"]

    title_label = split.replace("longmemeval_", "").upper()
    dataset_line = _SPLIT_DESCRIPTIONS.get(split, f"{split} (unrecognized split)")
    note = _SPLIT_NOTES.get(split, "See docs/benchmarks.md for context.")

    lines = [
        "=" * 72,
        f"Potluck Memory Retriever — LongMemEval {title_label} Results",
        "=" * 72,
        f"  Dataset:          {dataset_line}",
        f"  Instances run:    {n}",
        f"  Retrieval cutoff: k={k}",
        f"  Embedder:         {'BAAI/bge-small-en-v1.5 (ok)' if embedder_ok else 'UNAVAILABLE — results are random baseline'}",
        f"  Wall time:        {wall_seconds:.1f}s",
        "",
        "AGGREGATE",
        "-" * 40,
        f"  recall_any@{k}:  {agg['recall_any']:.4f}",
        f"  recall_all@{k}:  {agg['recall_all']:.4f}",
        f"  nDCG@{k}:        {agg['ndcg']:.4f}",
        f"  p50 latency:    {agg['p50_ms']:.1f}ms",
        f"  p95 latency:    {agg['p95_ms']:.1f}ms",
        f"  errors:         {agg['n_errors']}",
        "",
        f"BY QUESTION TYPE (recall_any@{k})",
        "-" * 40,
    ]
    for qt, stats in sorted(by_type.items()):
        lines.append(
            f"  {qt:<35} {stats['recall_any']:.4f}  (n={stats['n']})"
        )
    lines.append("")
    lines.append(
        f"NOTE: These numbers measure retrieval layer only (does the right session\n"
        f"rank at top-k?), not answer generation accuracy. {note}\n"
        f"See docs/benchmarks.md for full context and comparison to Mem0/Letta/Zep."
    )
    lines.append("=" * 72)
    return "\n".join(lines)


async def main() -> int:
    args = parse_args()

    # Load dataset
    data_path = Path(args.data)
    if not data_path.exists():
        print(f"ERROR: dataset not found at {data_path}")
        print("Run: curl -L 'https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json' -o bench/longmemeval/data/longmemeval_oracle.json")
        return 1

    print(f"Loading {data_path}...")
    dataset = json.loads(data_path.read_text())
    print(f"  {len(dataset)} instances loaded")

    # Output dir
    out_dir: Path
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = _make_out_dir(_REPO_ROOT / "bench")

    split = _split_label(data_path)
    print(f"Output dir: {out_dir}")
    print(f"Running LongMemEval {split} (k={args.k}, n={args.max_instances or len(dataset)})...")
    print("This embeds each session per instance — may take a few minutes on first run (model download).")
    print()

    wall_start = time.perf_counter()
    results = await run_longmemeval(
        dataset=dataset,
        k=args.k,
        max_instances=args.max_instances,
        verbose=args.verbose,
    )
    wall_seconds = time.perf_counter() - wall_start
    results["split"] = split

    # Write results
    results_path = out_dir / "longmemeval_results.json"
    results_path.write_text(json.dumps(results, indent=2))
    print(f"Results written: {results_path}")

    # Write summary
    summary = _format_summary(results, wall_seconds, split)
    summary_path = out_dir / "longmemeval_summary.txt"
    summary_path.write_text(summary)
    print(f"Summary written: {summary_path}")
    print()
    print(summary)

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    # Clean up global tmpdir
    import shutil
    shutil.rmtree(_global_tmpdir, ignore_errors=True)
    sys.exit(exit_code)

# Potluck Memory: Benchmark Results

This page states what the memory retriever was measured on, how, and what the numbers do and do not show. The inference benchmark is in [inference-benchmarks.md](inference-benchmarks.md).

---

## Summary

Potluck's embedding retriever (BAAI/bge-small-en-v1.5, cosine similarity) scores **recall_any@10 = 1.00 on the LongMemEval oracle split** at **p50 = 10.6 ms / p95 = 35.0 ms** per retrieval call on an M2 Pro 16GB Mac.

The oracle split holds only the evidence sessions for each question, so its scores are set mostly by the split's structure (see Methodology). The more informative measurements are:

- **LongMemEval-S**, which hides the evidence among filler sessions: recall_any@10 = 0.980, recall_all@10 = 0.938, nDCG@10 = 0.8847.
- **Internal Eval B**, which measures the full retrieval path (keyword and semantic fusion, pinned memories, project isolation): recall@5 = 0.72 for fusion against 0.48 for keyword-only.

---

## Methodology

### What was run

- **Benchmark**: LongMemEval (ICLR 2025), oracle split (`longmemeval_oracle.json`) and S split (`longmemeval_s`)
- **Oracle split**: 500 questions, evidence sessions only (no filler). See "Oracle split: structural note" below.
- **S split**: 500 questions, about 48 sessions per question with the evidence sessions hidden among filler sessions.
- **Retriever**: `semantic_search()` from `apps/desktop/python/potluck_local/services/memory_retrieval.py`, using BAAI/bge-small-en-v1.5 (384-dim, asymmetric with the BGE query prefix). It embeds the question and ranks every stored memory by cosine similarity.
- **Indexing**: Each LongMemEval session (a multi-turn conversation) is flattened to its user-turn text, truncated at 2,000 characters, and embedded as one memory. There is no LLM-based extraction. This matches LongMemEval's own `flat-bm25` and `flat-contriever` baselines.
- **Retrieval mode**: `semantic_search()` directly, not `get_relevant_memories()`. The LongMemEval corpus has no standing preference memories (no pinned or preloaded tier), so the fusion path would add nothing to measure. The fusion path is measured by Eval B below.
- **Hardware (oracle runs)**: Apple Silicon M2 Pro, 16GB unified memory. The embedder uses the Metal backend through sentence-transformers.
- **Date run (oracle)**: 2026-05-31

### Oracle split: structural note

The oracle split provides only the evidence sessions for each question, the sessions that contain the answer. There is no filler noise. Every instance's `haystack_sessions` equals its `answer_session_ids` (checked: 500/500 instances). The maximum is 6 sessions per instance, the median is 2, and 176 of 500 instances have exactly 1 session.

This means:
- **recall_any@10 = 1.00 is guaranteed by the structure** for any retriever that returns all sessions. With k=10 and at most 6 sessions in the corpus, nothing can be missed. The metric asks whether the retriever returned at least one relevant session, and every session is relevant.
- **recall_all@1 = 0.352 is also set by the structure.** It equals 176/500, the share of instances with exactly one evidence session. At k=1 only those instances can have all their evidence in the result, and every one of them does, so the value says nothing about ranking quality.
- **nDCG@10 = 1.00** follows from the same reasoning as recall_any@10.
- **The retrieval test that includes filler** is LongMemEval-S (about 48 sessions per question). The S results below are the ones that measure ranking.

---

## Results

### LongMemEval oracle: Potluck retriever

| Metric | Value | Notes |
|---|---|---|
| recall_any@10 | 1.00 | Guaranteed by the oracle structure (see above) |
| recall_all@10 | 1.00 | Guaranteed by the oracle structure |
| recall_any@1 | 1.00 | Guaranteed by the oracle structure (the corpus always has a relevant session) |
| recall_all@1 | 0.352 | From a separate k=1 run. Equals 176/500, the share of single-session instances |
| nDCG@10 | 1.00 | Guaranteed by the oracle structure |
| p50 latency | 10.6 ms | Per retrieval call, M2 Pro 16GB, model warm |
| p95 latency | 35.0 ms | Per retrieval call, M2 Pro 16GB |
| Wall time (500 instances) | 44.8 s | Includes per-instance DB init, embedding and retrieval |
| Errors | 0 / 500 | |
| Embedder | BAAI/bge-small-en-v1.5 | 384-dim, Metal backend |
| Hardware | M2 Pro 16GB | Unified memory, no discrete GPU |

By question type (recall_any@10):

| Question Type | n | recall_any@10 |
|---|---|---|
| knowledge-update | 78 | 1.00 |
| multi-session | 133 | 1.00 |
| single-session-assistant | 56 | 1.00 |
| single-session-preference | 30 | 1.00 |
| single-session-user | 70 | 1.00 |
| temporal-reasoning | 133 | 1.00 |

### LongMemEval-S: Potluck retriever

500 questions, about 48 sessions per question, k=10, same retriever and indexing as above. Result files are under `bench/results/20260602T174026Z/`. The run records the embedder and the metrics but not the hardware, so this page gives no latency for it.

| Metric | Value |
|---|---|
| recall_any@10 | 0.9800 |
| recall_all@10 | 0.9380 |
| nDCG@10 | 0.8847 |
| Errors | 0 / 500 |

By question type (recall_any@10):

| Question Type | n | recall_any@10 |
|---|---|---|
| knowledge-update | 78 | 1.0000 |
| multi-session | 133 | 1.0000 |
| single-session-assistant | 56 | 0.9643 |
| single-session-preference | 30 | 0.9333 |
| single-session-user | 70 | 0.9857 |
| temporal-reasoning | 133 | 0.9624 |

LongMemEval-M (about 500 sessions per question) is not reported here.

### Internal Eval B: Potluck custom corpus (42 memories, 25 queries)

This eval covers the retrieval contract the product uses: fusion of keyword and semantic rankings, pinned memories, preference preloading, and cross-project isolation. The corpus and queries are in `apps/desktop/python/evals/fixtures.py`.

| Metric | Keyword-only | Fusion (default) | Lift |
|---|---|---|---|
| recall@5 | 0.48 | 0.72 | +0.24 |

`make eval-retrieval` prints recall@3, recall@5, recall@10, recall@20 and MRR for both modes. Only recall@5 is recorded here. Fusion beats keyword-only by 24 points on a budget of 5 slots. Cross-project isolation: 0 leaks detected. The per-query breakdown is not stored in this file because it changes as the corpus grows.

---

## What this measures and what it does not

**What it measures:**
- Whether the BAAI/bge-small-en-v1.5 embedder, working on user-turn concatenations of sessions, finds the right session or sessions for a question, with no filler (oracle) and with filler (S)
- Per-call retrieval latency on Apple Silicon for the oracle runs
- Absence of crashes or out-of-memory errors across a 500-instance suite

**What it does not measure:**
- End-to-end answer accuracy. LongMemEval's QA score needs a generation model and an LLM judge. These runs score retrieval only.
- Retrieval quality in Potluck's product path (chat context injection). That is Eval B, run with `make eval-retrieval`.
- Keyword and semantic fusion on LongMemEval. The LongMemEval runs use `semantic_search()` only.
- Latency on non-Apple-Silicon hardware.
- Memory store scalability. Each LongMemEval instance uses a fresh store of 2 to 6 sessions (oracle) or about 48 (S). Retrieval multiplies the query vector against every stored vector, so latency grows with store size, and this was not measured at large sizes.

---

## How to reproduce

```bash
# One-time setup (if not already done)
make sidecar-setup

# Run the full 500-instance LongMemEval oracle suite
make bench
# Results land in bench/results/<timestamp>/
# Summary printed to stdout; full per-instance JSON in longmemeval_results.json

# Run the internal Eval B (Potluck custom corpus, fusion quality)
make eval-retrieval
```

The `make bench` target downloads `longmemeval_oracle.json` if it is not present (requires network). To fetch it first, for example to run offline:

```bash
curl -L "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json" \
     -o bench/longmemeval/data/longmemeval_oracle.json
make bench
```

To run another split, pass its data file to the runner. The split name comes from the file name:

```bash
PYTHONPATH=. apps/desktop/python/.venv/bin/python bench/run_longmemeval.py \
    --data bench/longmemeval/data/<split file>.json --k 10
```

No OpenAI API key is required. No GPU is required. The embedder (BAAI/bge-small-en-v1.5) downloads on first run from HuggingFace (about 33MB) and is cached by sentence-transformers.

---

## Updating this page

`make bench` writes results to `bench/results/<timestamp>/`. The tables above are edited by hand after reviewing a run.

# LongMemEval (vendored)

MIT License. Original: https://github.com/xiaowu0162/LongMemEval

Version pinned: commit `main` as of 2026-05-31 (paper: ICLR 2025).

## What is vendored

- `data/longmemeval_oracle.json`: the 500-question oracle split (evidence sessions only, no filler). Downloaded from https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned
- `eval_utils.py`: retrieval metrics (recall_any, recall_all, nDCG@k). Verbatim from src/retrieval/eval_utils.py.
- `LICENSE`: MIT, original authors.

## What is NOT vendored

The full retrieval runner (`run_retrieval.py`) depends on PyTorch + GPU-only models (contriever, stella, gte-Qwen2-7B). We do not vendor it. Our adapter (`bench/adapters/potluck_retriever.py`) re-implements the retrieval evaluation loop against the Potluck retriever without those GPU deps.

## How to refresh

```
curl -L "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json" \
     -o bench/longmemeval/data/longmemeval_oracle.json
```

Re-run `make bench` after refreshing data. Pin the commit hash here when refreshing.

## Splits we use

- `longmemeval_oracle.json` by default (`make bench`). The oracle split gives each question only the evidence sessions (no filler noise), so it tests retrieval quality (does the embedder rank the right session in the top k?) without LLM answer generation.

The runner scores retrieval only, so it can also be pointed at `longmemeval_s` and `longmemeval_m` (the full-history splits) with `--data`. Those files are not vendored. One S run is recorded under `bench/results/20260602T174026Z`. Answer-generation accuracy is not measured: it depends on the local model, which varies with the user's hardware.

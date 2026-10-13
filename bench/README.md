# Potluck Benchmark Suite

Benchmarks for the memory retriever, local model inference and cross-machine retrieval. Methods and results are written up in [docs/benchmarks.md](../docs/benchmarks.md) (memory) and [docs/inference-benchmarks.md](../docs/inference-benchmarks.md) (inference). Raw results are committed under `bench/results/`.

All commands run from the repository root. Run `make sidecar-setup` once first; every runner uses the sidecar virtualenv at `apps/desktop/python/.venv`.

## Runners

| Runner | Make target | What it measures |
| --- | --- | --- |
| `bench/run_longmemeval.py` | `make bench` | Retrieval quality and latency on LongMemEval |
| `apps/desktop/python/evals/eval_retrieval_quality.py` | `make eval-retrieval` | Retrieval quality on Potluck's own labeled corpus (Eval B) |
| `bench/run_inference.py` | `make bench-inference` | Local model speed: generation and prompt tokens per second, time to first token, peak memory |
| `bench/run_inference_windows.py` | none | The same inference suite on Windows with the Vulkan build |
| `bench/run_peer_retrieval.py` | none | Memory search across the mesh, against a peer machine |

### LongMemEval retrieval

`make bench` scores the Potluck retriever on the LongMemEval oracle split: 500 questions, evidence sessions only. It reports recall_any@10, recall_all@10 and nDCG@10 at the session level, plus retrieval latency. It downloads the dataset to `bench/longmemeval/data/` if it is missing.

The runner takes the split from the data file name, so another split can be run with `--data`:

```sh
PYTHONPATH=. apps/desktop/python/.venv/bin/python bench/run_longmemeval.py \
    --data bench/longmemeval/data/<split file>.json --k 10
```

Results go to `bench/results/<timestamp>/longmemeval_results.json` and `longmemeval_summary.txt`. The adapter is `bench/adapters/potluck_retriever.py`. The vendored metric code and its license are in `bench/longmemeval/`.

### Eval B

`make eval-retrieval` runs a labeled corpus of 42 memories and 25 queries against Potluck's retrieval path: keyword and semantic fusion, pinned memories, preference preloading and cross-project isolation. It prints recall@3, recall@5, recall@10, recall@20 and MRR for fusion and for keyword-only. The corpus is in `apps/desktop/python/evals/fixtures.py`.

### Inference

`make bench-inference` runs the `bench/prompts/inference_v1.json` suite (10 prompts, 3 runs each) against the GGUF models in `bench/models/` through llama-cpp-python. The runner supports macOS and Linux.

```sh
make bench-inference-get-models                      # Phi-3-mini, Llama-3.1-8B, Qwen2.5-7B (about 12 GB)
make bench-inference                                 # every model in bench/models/
make bench-inference MODEL=bench/models/<name>.gguf  # one model
```

`bench/scripts/get_models.py` downloads the weights and checks each one against `bench/models/SHA256SUMS.txt`. It can also fetch the two Llama-3.1-70B variants by model ID. The downloaded `.gguf` files are not committed.

On Windows, run `bench/run_inference_windows.py` from a virtualenv that has the Vulkan build of llama-cpp-python. It patches the platform checks in `run_inference.py` and otherwise uses the same adapter, prompts and result format.

Results go to `bench/results/<timestamp>/inference_results.json` and `inference_summary.txt`.

### Peer retrieval

`bench/run_peer_retrieval.py` checks that `/peer/memory/search` returns the same results across the mesh as the local retriever does, and measures cross-machine retrieval latency at several store sizes (10, 100 and 500 memories by default). It needs the peer's mesh IP from this machine's mesh roster and an SSH user on the peer. The peer's memory endpoint is loopback-only, so the runner opens an SSH tunnel to populate it with test memories and deletes them afterwards.

```sh
PYTHONPATH=. apps/desktop/python/.venv/bin/python bench/run_peer_retrieval.py \
    --peer-mesh-ip <mesh ip> --peer-ssh-user <user> --peer-name <label>
```

Results go to `bench/results/peer_retrieval_<label>_<timestamp>/results.json`.

## Principles

- Report numbers as measured, including the ones that look bad.
- When a comparison is not like for like (different split, metric or hardware), say so next to the number.
- Record the conditions of every run: hardware, software versions, context size, run count.
- The tables in `docs/` are updated by hand after reviewing a run. No runner rewrites them.

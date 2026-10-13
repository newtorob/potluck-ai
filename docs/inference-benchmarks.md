# Potluck Inference: Benchmark Results

This page reports local-model inference speed, time to first token and memory use on three platforms: Apple M2 Pro (Metal), NVIDIA RTX 2080 Ti (CUDA) and AMD RX 6600 XT (Vulkan). It also covers 70B models, which no single 16 GB machine can hold, and a test of llama.cpp's RPC backend for splitting a model across machines. It is the companion to the memory retrieval benchmark in [benchmarks.md](benchmarks.md). Runs where a model did not fit in GPU memory are included.

Tables from runs before 2026-10-10 are marked "original harness". Read the erratum first.

---

## Erratum: generation rates before 2026-10-10

The harness runs each prompt twice: a streaming pass to time the first token, then a full pass for the token counts. The streaming pass also generates the whole answer. llama-cpp-python returns no timing data with a completion, so the harness fell back to the wall clock, and that clock started before the first pass. Generation speed came out as one answer's tokens divided by the time of two generations.

- **Generation tokens per second is understated by a factor of about 2.1 to 2.2** in every suite result dated before 2026-10-10, on all three platforms. This was checked three ways: against the stored timings of the June Mac and Linux runs; by running the suite on Windows on 2026-10-10 with the old harness and again with the fixed one (16.4, 22.4 and 20.8 became 34.5, 48.8 and 44.9); and by rerunning Linux on 2026-10-10 with the fixed harness on the same llama-cpp-python as June (69.2, 45.9 and 43.0 became 146.2, 96.8 and 89.9).
- **Ratios between platforms mostly stand**, because each platform was understated by about the same factor. Absolute rates do not, and neither does anything built on them.
- **Prompt eval tokens per second** was computed as prompt tokens over time to first token, which also counts the first generated token. It reads low on short prompts.
- **Time to first token and peak RSS are unaffected.**

The fix is in `bench/adapters/llama_cpp_runtime.py`. It times the second pass alone and takes both rates from llama.cpp's own counters, the figures `llama_perf_context_print` shows. A wall-clock rate for the same pass is recorded beside them. Result files written by the fixed harness carry `"timing_version": 2`.

| Model (Q4_K_M) | M2 Pro Metal, June: published, then re-derived | 2080 Ti CUDA: June published, June re-derived, 2026-10-10 measured | 6600 XT Vulkan, 2026-10-10: measured |
|---|---|---|---|
| Phi-3-mini-3.8B | 19.1, about 42 tok/s | 69.2, about 145, **146.2 tok/s** | **34.5 tok/s** |
| Qwen2.5-7B | 12.4, about 27 tok/s | 45.9, about 94, **96.8 tok/s** | **48.8 tok/s** |
| Llama-3.1-8B | 11.6, about 25 tok/s | 43.0, about 88, **89.9 tok/s** | **44.9 tok/s** |

The re-derived figures are estimates from the wall time and time to first token stored with each June run: `output tokens / ((wall time - 2 x time to first token) / 2)`. Where a measured figure exists, the formula lands within 3 percent of it: 34.1, 47.9 and 44.3 against 34.5, 48.8 and 44.9 on Windows, and about 145, 94 and 88 against 146.2, 96.8 and 89.9 on Linux. The June Linux result files are in the repository under `bench/results/20260618T212446Z-linux/` and `bench/results/20260618T214923Z-linux/`.

The raw timings of the June Windows run are not available, so its June column cannot be re-derived. The October rerun replaces it. The Mac column is the estimate; it has not been re-measured with the fixed harness. The Linux and Windows columns were both measured on 2026-10-10, on llama-cpp-python 0.3.25 (CUDA) and 0.3.35 (Vulkan).

The 70B figure of 2.4 tok/s in the Linux section was a quick single-prompt measure outside the suite. How it was timed was not recorded, so it is left as published.

---

## Summary

Three instruction-tuned models in Q4_K_M quantization were run with llama-cpp-python and full GPU offload on each platform.

**Apple M2 Pro 16GB, Metal (llama-cpp-python 0.3.23, original harness).** Medians across 3 runs per (model x prompt) cell and 10 prompts. The full run took 33.8 minutes.

| Model | Parameters | GenTok/s | Prompt Eval Tok/s | Median TTFT | Peak RSS |
|---|---|---|---|---|---|
| Phi-3-mini-4k-instruct-Q4_K_M | 3.8B | **19.1** | 405 | 824 ms | 5,354 MB |
| Qwen2.5-7B-Instruct-Q4_K_M | 7B | 12.4 | 247 | 1,214 ms | 4,637 MB |
| Meta-Llama-3.1-8B-Instruct-Q4_K_M | 8B | 11.6 | 236 | 1,374 ms | 5,508 MB |

**Linux, RTX 2080 Ti, CUDA.** In the original harness the same models decoded about 3.7x faster than on the M2 Pro. Re-measured with the fixed harness on 2026-10-10: 146.2, 96.8 and 89.9 tok/s. See [Linux / CUDA](#linux--cuda-rtx-2080-ti).

**Windows, RX 6600 XT, Vulkan.** In June the 8B model exceeded the card's 8 GB at 8K context, spilled to the CPU and dropped to 2.1 tok/s (original harness). In the 2026-10-10 rerun, on a fresh Windows install with a newer driver and a newer llama-cpp-python, it ran fully on the GPU at 44.9 tok/s. See [Windows / Vulkan](#windows--vulkan-rx-6600-xt).

**Distributed 70B.** Splitting a 70B across machines with llama.cpp's RPC backend works, and the cost is in loading: each worker's share of the weights crosses the network at load time. See [Distributed 70B](#distributed-70b-with-llamacpp-rpc).

Findings from the Mac run (original harness):

- **Generation speed falls as context grows.** Phi-3 drops from 22 tok/s on short prompts to 7-13 tok/s on 1700+ token prompts, consistent with KV cache bandwidth pressure on the unified memory bus.
- **Phi-3-mini is the fastest of the three** at 19.1 tok/s overall, against 11.6 to 12.4 tok/s for the 7-8B models. It loads in 2.4 seconds against 3.0 and 3.8 seconds.
- **The 70B ceiling.** Both Llama-3.1-70B variants (~42 GB at Q4_K_M, ~29 GB at Q3_K_S) exceed the 16 GB unified memory budget, so they were not run on the Mac. This is the case for pooling machines in Phase G.

All suite runs request full GPU offload. CPU-only inference was not measured on the M2 Pro.

---

## Methodology

### Runtime

- **llama-cpp-python**: 0.3.23 for the Mac runs. The Linux and Windows runs list their versions in their run context lines.
- **Metal backend confirmed (Mac)**: `libggml-metal.0.11.1.dylib` in the wheel; all layers assigned to `MTL0 (Apple M2 Pro)` in startup logs
- **n_gpu_layers = -1** (full layer offload)
- **n_threads = 6** on the Mac (performance cores, auto-detected via `sysctl hw.perflevel0.logicalcpu`)
- **n_ctx = 8192** (Phi-3-mini loads with 4096 on the Mac run, its trained context max; the 7B and 8B use 8192)
- Fixed seed = 42, temperature = 0.7, top_p = 0.9

### KV cache reset between runs

llama-cpp-python caches prompt prefix tokens across calls on the same model instance. Without an explicit reset, runs 2 and later show near-zero TTFT (prefix match hit) and inflated prompt-eval rates. The adapter calls `model.reset()` before every inference call, both before the TTFT streaming pass and before the timing pass. Each measurement is therefore a cold prompt evaluation, which is what a user experiences on a fresh question.

Without this reset, run 2 TTFT was ~20ms; with the reset it is 150-7000ms depending on prompt length.

### Two-pass metric capture

Each (model x prompt x run) measurement uses two inference passes:
1. **TTFT pass** (streaming): stream tokens, stop timing on the first non-empty token, then drain the generator. Captures TTFT only.
2. **Full pass** (non-streaming): run the full completion for the token counts. The generation and prompt-eval rates come from llama.cpp's own counters for this pass alone. If the counters are unavailable, the harness falls back to the wall clock of this pass, with the first pass's time to first token standing in for prompt evaluation.

Both passes reset the KV cache before starting. The two passes roughly double the runtime of the suite. Results dated before 2026-10-10 used the earlier timing described in the erratum.

### Models

All models are Q4_K_M GGUF from bartowski's mirror, verified by SHA-256:

| Model | File | Size | SHA-256 |
|---|---|---|---|
| Phi-3-mini-4k-instruct-Q4_K_M | `Phi-3-mini-4k-instruct-Q4_K_M.gguf` | 2.23 GiB | `28a89b4ddb5766355f24e362ae4078b4c35b9ca9568df5fc9e6d9aeee4dee834` |
| Qwen2.5-7B-Instruct-Q4_K_M | `Qwen2.5-7B-Instruct-Q4_K_M.gguf` | ~4.7 GiB | `65b8fcd92af6b4fefa935c625d1ac27ea29dcb6ee14589c55a8f115ceaaa1423` |
| Meta-Llama-3.1-8B-Instruct-Q4_K_M | `Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf` | ~4.9 GiB | `7b064f5842bf9532c91456deda288a1b672397a54fa729aa665952863033557c` |

**Why Q4_K_M?** 13 of the 14 models in Potluck's model catalog use Q4_K_M (the other uses Q5_K_M), so it is the quantization users run. It is a 4-bit k-quant, much smaller than the FP16 file at some cost in quality.

### Prompt suite

10 prompts in `bench/prompts/inference_v1.json`, byte-stable (fixed seed/temperature/top_p):

| ID | Category | Approx input tokens | Max output |
|---|---|---|---|
| `short_qa_1` | short_qa | ~50 | 200 |
| `short_qa_2` | short_qa | ~50 | 200 |
| `code_gen_1` | code_generation | ~120 | 500 |
| `code_gen_2` | code_generation | ~150 | 500 |
| `code_gen_3` | code_generation | ~140 | 500 |
| `summarization_1` | summarization | ~540 | 300 |
| `summarization_2` | summarization | ~470 | 300 |
| `summarization_3` | summarization | ~450 | 300 |
| `long_context_1` | long_context | ~1,690 | 500 |
| `long_context_2` | long_context | ~1,880 | 500 |

3 runs per (model x prompt) cell. All reported metrics are **medians across 3 runs**.

### Metrics

- **GenTok/s**: output tokens per second of generation. Fixed harness: `n_eval / t_eval` from llama.cpp's counters for the full pass; a wall-clock rate for the same pass is recorded beside it. Headline number.
- **PETok/s**: prompt eval tokens per second. Fixed harness: `n_p_eval / t_p_eval` from the same counters. Measures context ingestion throughput.
- **TTFT**: wall-clock from `create_chat_completion()` to the first non-empty token in the streaming response.
- **Peak RSS**: `psutil.Process().memory_info().rss` sampled every 100ms during inference. Approximate (includes model weights where they are mapped into the process, KV cache, Python interpreter and compute buffers).

---

## Mac / Metal (M2 Pro)

All figures in this section are from the 2026-06-01 run with the original harness.

### Hardware run context

| Parameter | Value |
|---|---|
| Machine | Apple MacBook Pro (Mac14,9) |
| Chip | Apple M2 Pro |
| Total RAM | 16 GB unified memory |
| macOS | 15.7.3 (Darwin 24.6.0) |
| llama-cpp-python | 0.3.23 |
| Metal backend | Active (`libggml-metal.0.11.1.dylib`) |
| n_gpu_layers | -1 (all layers to MTL0) |
| n_threads | 6 (performance cores, auto-detected) |
| n_ctx | 8192 (4096 for Phi-3-mini) |
| Date run | 2026-06-01 |
| Results file | `bench/results/20260601T164914Z/inference_results.json` |
| Total bench time | 33.8 min (3 models x 10 prompts x 3 runs x 2 passes) |

### Per-model summary (median across all 10 prompts, 3 runs each; original harness)

| Model | GenTok/s | PETok/s | Median TTFT | Peak RSS | Load time |
|---|---|---|---|---|---|
| Phi-3-mini-4k-instruct-Q4_K_M | **19.1** | **405** | 824 ms | 5,354 MB | 2.4 s |
| Qwen2.5-7B-Instruct-Q4_K_M | 12.4 | 247 | 1,214 ms | 4,637 MB | 3.0 s |
| Meta-Llama-3.1-8B-Instruct-Q4_K_M | 11.6 | 236 | 1,374 ms | 5,508 MB | 3.8 s |

### Per-category breakdown: Phi-3-mini-3.8B-Q4_K_M

| Category | Gen Tok/s | PE Tok/s | Median TTFT | Peak RSS | Median output |
|---|---|---|---|---|---|
| short_qa (~50 tok) | 22.1 | 284 | 150 ms | 5,345 MB | 143 tok |
| code_generation (~130 tok) | 20.9 | 391 | 399 ms | 5,319 MB | 459 tok |
| summarization (~490 tok) | 16.9 | 409 | 1,256 ms | 5,355 MB | 123 tok |
| long_context (~1,785 tok) | 10.3 | 408 | 4,376 ms | 5,363 MB | 228 tok |

### Per-category breakdown: Qwen2.5-7B-Q4_K_M

| Category | Gen Tok/s | PE Tok/s | Median TTFT | Peak RSS | Median output |
|---|---|---|---|---|---|
| short_qa (~50 tok) | 13.5 | 174 | 259 ms | 4,721 MB | 200 tok |
| code_generation (~130 tok) | 13.3 | 237 | 614 ms | 4,608 MB | 464 tok |
| summarization (~490 tok) | 11.3 | 251 | 1,814 ms | 4,638 MB | 127 tok |
| long_context (~1,785 tok) | 9.7 | 258 | 6,072 ms | 4,656 MB | 295 tok |

### Per-category breakdown: Llama-3.1-8B-Q4_K_M

| Category | Gen Tok/s | PE Tok/s | Median TTFT | Peak RSS | Median output |
|---|---|---|---|---|---|
| short_qa (~50 tok) | 13.1 | 171 | 391 ms | 5,478 MB | 200 tok |
| code_generation (~130 tok) | 12.3 | 232 | 699 ms | 5,490 MB | 500 tok |
| summarization (~490 tok) | 10.8 | 235 | 2,024 ms | 5,526 MB | 155 tok |
| long_context (~1,785 tok) | 8.4 | 239 | 6,546 ms | 5,546 MB | 217 tok |

### Notable patterns

**Gen tok/s falls with context length.** Phi-3 drops from 22 tok/s (short_qa) to 10 tok/s (long_context). This is consistent with memory bandwidth pressure: each decode step reads the KV cache, which grows with the number of prompt tokens. The long_context prompts at ~1,800 tokens fill 22% of the 8192-token KV cache.

**Prompt eval tok/s is lower on short prompts and roughly flat above about 130 tokens.** For Phi-3 it is 391, 409 and 408 tok/s on the code, summarization and long_context prompts. The short_qa figures read low because of the prompt-eval calculation described in the erratum. Prompt eval uses batched matrix multiply, so it scales well with prompt length.

**Phi-3's gen tok/s advantage shrinks at long context.** On short_qa Phi-3 is about 1.6x faster than Qwen2.5 (22.1 against 13.5 tok/s). On long_context it is only 1.1x faster (10.3 against 9.7 tok/s). At long context the decode cost is increasingly the KV cache bandwidth rather than the weights, so the smaller model's advantage narrows.

**Peak RSS covers the whole process, not just the model weights.** Phi-3's RSS is 5,354 MB although its file is 2.23 GB. The rest is the Python interpreter, the compute buffers (a ~320 MB Metal compute buffer is logged at load) and the KV cache, which counts toward RSS on unified memory. The figure is approximate, and Phi-3's is higher than Qwen2.5's (4,637 MB) although its file is smaller, so compare RSS across models with care.

**Qwen2.5-7B against Llama-3.1-8B.** Qwen2.5 was slightly faster (12.4 against 11.6 tok/s) and used less memory (4,637 against 5,508 MB RSS). The difference is 7 percent on one machine; run-to-run spread is not reported separately.

### 70B models: local ceiling

The Mac runs include no 70B result because the models do not fit in 16 GB of unified memory.

**Llama-3.1-70B-Q4_K_M (~42 GB file)** needs about 42 GB for weights plus several GB for the KV cache. Apple Silicon shares all memory between the CPU, the GPU and the OS, so there is no separate VRAM pool, and 16 GB is far short.

**Llama-3.1-70B-Q3_K_S (~29 GB file)** narrows the gap but does not close it. 29 GB is still 13 GB beyond the machine's total RAM.

**What fits on 16 GB**: a 7B Q4_K_M model uses ~4.6 GB peak RSS at 8192 context, leaving ~11 GB for the OS, other apps and headroom. An 8B Q4 uses ~5.5 GB.

**Why this matters for the mesh**: a 70B model that no single 16 GB machine can hold can in principle be split across several machines with llama.cpp's RPC backend. The distributed section below reports what happened when that was tried: loading is bound by the network.

---

## Linux / CUDA (RTX 2080 Ti)

### Original run (2026-06-18, original harness)

This run used the same `inference_v1` suite (3 models, Q4_K_M, 10 prompts, 3 runs each, two-pass capture, n_ctx 8192, n_gpu_layers=-1) on a wired Linux node with a CUDA build of llama-cpp-python.

The card is an **RTX 2080 Ti (11 GB VRAM)**, a 2018 Turing part. All three models fit entirely in VRAM, so unlike Apple's unified memory there is no CPU/GPU bandwidth sharing.

Cross-vendor results (median across 10 prompts, 3 runs; original harness):

| Model (Q4_K_M) | M2 Pro Metal | 2080 Ti CUDA | Decode speedup |
|---|---|---|---|
| Phi-3-mini-3.8B | 19.1 tok/s | **69.2 tok/s** | 3.6x |
| Qwen2.5-7B | 12.4 tok/s | **45.9 tok/s** | 3.7x |
| Llama-3.1-8B | 11.6 tok/s | **43.0 tok/s** | 3.7x |

Prompt eval and time-to-first-token (original harness):

| Model | Metal PE / TTFT | CUDA PE / TTFT |
|---|---|---|
| Phi-3-mini | 405 tok/s / 824 ms | 3,537 tok/s / 88 ms |
| Qwen2.5-7B | 247 tok/s / 1,214 ms | 2,302 tok/s / 126 ms |
| Llama-3.1-8B | 236 tok/s / 1,374 ms | 2,249 tok/s / 142 ms |

Findings:

- **Decode is about 3.7x faster, consistently.** All three models land at 3.6 to 3.7x. Decode is memory-bandwidth-bound, and the 2080 Ti's ~616 GB/s is roughly 3x the M2 Pro's unified-memory bandwidth (~200 GB/s); the rest of the gap is likely compute headroom.
- **Prompt eval and TTFT improve roughly 9 to 10x.** Prompt eval is compute-bound and batched (all prompt tokens processed in parallel), so the GPU's raw compute advantage shows up more here than in bandwidth-bound decode. TTFT drops from over a second to under 150 ms across the board, which is the latency a user feels before the model starts responding.
- **RSS is not comparable across vendors.** The CUDA peak RSS (580 to 910 MB) is far below the Mac's (4.6 to 5.5 GB) because on NVIDIA the model weights live in VRAM, outside the process RSS, whereas Apple's unified memory counts the weights toward RSS. Compare VRAM to VRAM, not RSS to RSS, across vendors.
- **Phi-3 n_ctx caveat.** The CUDA run used n_ctx 8192 for all three models, including Phi-3, whose trained context is 4096 (the Mac run capped Phi-3 at 4096). Decode throughput is largely insensitive to n_ctx, so the Phi-3 comparison still holds, but the 7B and 8B are the clean apples-to-apples cells (both at 8192).

Run context: Ubuntu 22.04.5, x86_64, 62.6 GB RAM, RTX 2080 Ti (driver 580.159.03), llama-cpp-python 0.3.25 (CUDA 13), n_gpu_layers -1, n_ctx 8192, 3 runs/cell, 2026-06-18. Results under `bench/results/20260618T212446Z-linux/` and `bench/results/20260618T214923Z-linux/`. In the first of those runs Llama-3.1-8B failed to create its context (out of memory); the 43.0 tok/s figure is from the second, which ran that model alone.

### Rerun with the fixed harness (2026-10-10)

The same `inference_v1` suite on the same machine and the same llama-cpp-python build as June, with the fixed harness. The Potluck engine's model was unloaded for the run so the card was free.

| Model | Generation (llama.cpp counters) | Generation (wall clock) | Prompt eval | TTFT | Peak RSS |
|---|---|---|---|---|---|
| Phi-3-mini-3.8B Q4_K_M | **146.2 tok/s** | 143.2 tok/s | 3,586 tok/s | 88 ms | 0.6 GB |
| Qwen2.5-7B Q4_K_M | **96.8 tok/s** | 94.4 tok/s | 2,332 tok/s | 126 ms | 0.9 GB |
| Llama-3.1-8B Q4_K_M | **89.9 tok/s** | 88.0 tok/s | 2,239 tok/s | 142 ms | 0.9 GB |

- **The figures that the timing error did not touch match June.** The machine runs the same llama-cpp-python build, with a point update to the NVIDIA driver (580.159 to 580.178). Time to first token is 88, 126 and 142 ms then and now, prompt eval is within 2 percent, and generation is 2.1 times the published figure on all three models.
- **Against the RX 6600 XT measured the same day**, the 2080 Ti is about 2.0 times as fast on the 7B (96.8 against 48.8 tok/s) and on the 8B (89.9 against 44.9), and 4.2 times on Phi-3 (146.2 against 34.5). In June the 7B ratio was 2.9 times. The AMD figure has risen since then and the NVIDIA figure has not moved.

Run context: Ubuntu 22.04.5, x86_64, 62.6 GB RAM, RTX 2080 Ti (driver 580.178.04), llama-cpp-python 0.3.25 (CUDA 13), n_gpu_layers -1, n_ctx 8192, 3 runs/cell, 2026-10-10. Results under `bench/results/20261010-linux/`.

### 70B on a single consumer GPU (partial offload)

Llama-3.1-70B-Q3_K_S (29 GB) does not fit in 11 GB of VRAM, but the Linux node has 62 GB of system RAM, so it runs with partial offload: 20 of 80 layers on the 2080 Ti, the rest on CPU. Measured single-prompt decode was **2.4 tok/s** (n_ctx 2048; a quick single-prompt measure, not the full 10-prompt suite). One consumer GPU can run a 70B by spilling to system RAM, but at 2.4 tok/s it is barely usable. This is the single-node baseline for pooling.

---

## Distributed 70B with llama.cpp RPC

### Load cost (June 2026)

Machines were pooled to run the 70B with llama.cpp's RPC backend. The finding is about the network, not the GPUs.

The infrastructure works. With the official b9704 prebuilt binaries, the Linux `rpc-server` drives the 2080 Ti through **Vulkan** (the NVIDIA driver ships a Vulkan ICD, so no CUDA toolkit build is needed), the macOS prebuilt provides a Metal `rpc-server` plus a `llama-cli` with `--rpc`, and the cross-machine RPC link connects.

The limit is bandwidth. llama.cpp RPC uploads each worker's share of the model tensors at load time, roughly 10 GB to a worker for the 70B. Measured raw throughput from a WiFi MacBook to the wired Linux node was **17 MB/s (~141 Mbit/s)**, so the upload alone takes about 10 minutes before the first token. In this setup only the Linux node was wired; the Mac and Windows machines were on WiFi, so any pool had to cross a WiFi link. At gigabit speed (about 110 MB/s) the same 10 GB would take roughly 1 to 2 minutes; that is arithmetic, not a measurement.

What this means for routing: sharded inference puts gigabytes on the inter-node link at load and activations on it every token, so it suits a wired, low-latency group on one LAN, and a single WiFi node slows the whole pool. Whole-model-per-peer routing needs only the prompt in and the tokens out, which is light and tolerates WiFi. Sharded pooling in Potluck is an opt-in for your own machines; see [managed-split-models.md](managed-split-models.md).

### RPC decode cost (August 2026)

A second run used current llama.cpp prebuilts (b10375, `ggml-rpc-server v5.0.0`) to capture decode throughput, which the June run never reached because it stalled in the load phase. RPC still connects across machines, and a Mac orchestrator (Metal + RPC) drives a remote worker.

The RPC overhead on decode is small. The same GPU (Windows RX 6600 XT) was measured natively and through RPC, on an 8B Q4:

| Path | pp128 (tok/s) | tg64 / decode (tok/s) |
|---|---|---|
| 6600 XT native (no RPC) | 113.3 | 4.21 |
| 6600 XT via RPC, over WiFi/Tailscale | 97.4 | 3.60 |

That is about a 14% penalty on decode and on prompt eval from routing through RPC over WiFi. These decode rates are far below the 44.9 tok/s the same card reached on an 8B model in the October rerun; the offload state and driver of this run were not recorded, so the 14% is a relative figure for that configuration. Once loaded, the per-token cost is latency-bound (a few small round trips per token), so a wired low-latency link should shrink it. The load remains bandwidth-bound: the 4.58 GiB tensor-share upload took about 7 minutes at about 12 MB/s (Mac to Windows over Tailscale; Mac to Linux measured 37 MB/s). Direct-LAN paths and the `rpc-server`'s local weight cache (`local cache: n/a`, off in this run) would both cut the load time; neither was measured.

Two confounds mean the target case, wired multi-GPU, is still unmeasured:
1. The Linux RTX 2080 Ti was running on the Intel iGPU for the whole session. `nvidia-smi` reported `Failed to initialize NVML: Driver/library version mismatch` (kernel module 580.159.03, userspace 580.173.02), so every Linux number from that session was CPU plus iGPU.
2. The Mac orchestrator had about 94 MB free of 16 GB and could not take any local layers in a split (`res = -3` on any nonzero local share). Only a 100%-remote split ran; no genuine 2-worker or 3-way split was captured.

The decode overhead is modest, but the actual wired GPU-to-GPU number has not been measured.

---

## Windows / Vulkan (RX 6600 XT)

### Original run (2026-06-20, original harness)

This run used the same `inference_v1` suite (3 models, Q4_K_M, 10 prompts, 3 runs each, two-pass capture, n_ctx 8192, n_gpu_layers=-1) on a Windows 10 node with the Vulkan build of llama-cpp-python driving an AMD Radeon RX 6600 XT (8 GB VRAM). ROCm/HIP is not an option on this card: gfx1032 is dropped from the current HIP SDK, so Vulkan is the only GPU path. The result files of this run are not in the repository.

Cross-vendor results (median across 10 prompts, 3 runs; original harness):

| Model (Q4_K_M) | M2 Pro Metal | 2080 Ti CUDA | 6600 XT Vulkan |
|---|---|---|---|
| Phi-3-mini-3.8B | 19.1 tok/s | 69.2 tok/s | 16.2 tok/s |
| Qwen2.5-7B | 12.4 tok/s | 45.9 tok/s | 15.9 tok/s |
| Llama-3.1-8B | 11.6 tok/s | 43.0 tok/s | **2.1 tok/s** |

Findings:

- **The 8 GB VRAM ceiling was the headline.** The 6600 XT served Phi-3 (3.8B) and Qwen2.5-7B fully on the GPU at ~16 tok/s. The 8B model at n_ctx 8192 did not fit in 8 GB once weights, KV cache and Vulkan compute buffers were counted, so it spilled to CPU partial offload and fell to 2.1 tok/s (peak RSS jumped to 7.6 GB, TTFT to 2.6 s). The Mac holds the 8B in its 16 GB unified memory, and the 2080 Ti holds it in 11 GB of VRAM. VRAM capacity, not vendor, decided which models the card could serve.
- **AMD was close to Apple on the models that fit.** On Qwen2.5-7B the 6600 XT was slightly ahead of the M2 Pro (15.9 against 12.4 tok/s), and on Phi-3 it trailed (16.2 against 19.1).
- **NVIDIA was about 2.9x the AMD card on the 7B**, the model both ran cleanly (45.9 against 15.9 tok/s). The 2080 Ti's extra 3 GB of VRAM is what let it also run the 8B.
- **RSS tracks model size on Vulkan** (Phi-3 2.6 GB, Qwen 5.0 GB), unlike the CUDA path where weights live in VRAM outside process RSS. The 8B's jump to 7.6 GB RSS is the signature of weights spilling into system RAM under partial offload.

Run context: Windows 10 (10.0.26200), AMD64, RX 6600 XT (8 GB, Vulkan), llama-cpp-python 0.3.24, n_gpu_layers -1, n_ctx 8192, 3 runs/cell, 2026-06-20.

### Rerun with the fixed harness (2026-10-10)

The same `inference_v1` suite (10 prompts, 3 runs each, n_ctx 8192, n_gpu_layers=-1) on the same card, with the fixed harness. The machine has a fresh Windows 11 install since June, with AMD's August 2026 display driver and llama-cpp-python 0.3.35.

| Model | Generation (llama.cpp counters) | Generation (wall clock) | Prompt eval | TTFT | Peak RSS |
|---|---|---|---|---|---|
| Phi-3-mini-3.8B Q4_K_M | **34.5 tok/s** | 34.2 tok/s | 643 tok/s | 516 ms | 2.7 GB |
| Qwen2.5-7B Q4_K_M | **48.8 tok/s** | 48.0 tok/s | 594 tok/s | 492 ms | 4.9 GB |
| Llama-3.1-8B Q4_K_M | **44.9 tok/s** | 44.2 tok/s | 565 tok/s | 578 ms | 5.1 GB |
| Qwen3-8B Q4 (Potluck catalog `qwen3-8b-q4`) | **43.6 tok/s** | 43.0 tok/s | 540 tok/s | 546 ms | 5.2 GB |

Findings:

- **The 8B now fits the 8 GB card.** Llama-3.1-8B at an 8192-token context ran fully on the GPU, with about 6.3 GB of the card in use, at 44.9 tok/s. In June it spilled to the CPU. This is a real change and separate from the timing error: with the old harness the October run reads 20.8 tok/s where June read 2.1.
- **The 7B is faster than in June too.** With the old harness, 15.9 tok/s in June and 22.4 in October.
- **What changed was not isolated.** Between the two runs the machine got a fresh Windows install, a new AMD driver and a newer llama-cpp-python (0.3.24 then, 0.3.35 now). The June driver version was not recorded.
- **Phi-3 is slower than the 7B on this card** (34.5 against 48.8 tok/s), which is not the case on the other two platforms. With the old harness its figure did not move between June and October (16.2 and 16.4). Phi-3 was run at n_ctx 8192, twice its trained context. Not investigated.
- **A stale driver gave wrong text at full speed.** On the driver Windows installs by itself on this card (27.20.22016.2, dated July 2021), the Vulkan backend repeated tokens in every reply while the CPU gave correct text. Updating the driver fixed it. A throughput figure alone would not have shown the fault, and this harness does not check the text it gets back. The engine's own answer check is described in [windows-gpu-inference.md](windows-gpu-inference.md).
- **Generation barely depends on prompt length here.** The 7B holds 46.8 tok/s on the long-context prompts against 49.5 on short Q&A. The 8B drops from 46.5 to 39.5.

Run context: Windows 11 (10.0.26300; the harness prints it as Windows 10), AMD64, Ryzen 7 5800X, 63.9 GB RAM, RX 6600 XT (8 GB, Vulkan, driver 32.0.21045.5002), llama-cpp-python 0.3.35, n_gpu_layers -1, n_ctx 8192, 3 runs/cell, 2026-10-10. Results under `bench/results/20261010-windows/` and `bench/results/20261010-windows-qwen3-8b/`. The run made the same day with the old timing, kept for comparison, is under `bench/results/20261010-windows-old-timing/`.

---

## What this measures and what it does not

**What it measures:**
- Generation throughput and prompt-eval throughput for three model sizes (3.8B, 7B, 8B) under full GPU offload on Metal, CUDA and Vulkan
- TTFT as a function of prompt length, the user-visible latency before the model starts responding
- Peak process memory at n_ctx=8192 with a cold KV cache per run
- The local ceiling: what model size runs usably on 16 GB unified memory, 11 GB of VRAM and 8 GB of VRAM

**What it does not measure:**
- Generation quality or accuracy. There are no evals against a benchmark dataset; that needs a model judge.
- CPU-only performance. All suite runs request GPU offload. A CPU and Vulkan comparison on one Windows machine is in [windows-gpu-inference.md](windows-gpu-inference.md).
- Flash attention. It was not enabled (the default in llama-cpp-python 0.3.x), and its effect was not measured.
- Batched inference. All runs are single-request. Multi-user batch throughput is a different metric.
- Structured output and constrained decoding latency.

---

## How to Reproduce

```bash
# One-time: install sidecar venv
make sidecar-setup

# Download the three small models (~12 GB total)
make bench-inference-get-models

# Run the full benchmark (3 models x 10 prompts x 3 runs)
make bench-inference

# Run against a specific model:
make bench-inference MODEL=bench/models/Phi-3-mini-4k-instruct-Q4_K_M.gguf

# Results land in bench/results/<timestamp>/
# inference_results.json: full per-run JSON with system info header
# inference_summary.txt: human-readable table
```

`make bench-inference` supports macOS and Linux. On Windows, run `bench/run_inference_windows.py` from a virtualenv with the Vulkan build of llama-cpp-python; see [bench/README.md](../bench/README.md).

SHA-256 verification runs automatically during download. On first download, the hash is pinned to `bench/models/SHA256SUMS.txt`. Subsequent runs verify against the pinned hash and refuse to proceed on mismatch.

For reproducibility, the following must match:
- llama-cpp-python version (check `pip show llama-cpp-python`)
- Model file SHA-256 (checked automatically)
- n_ctx, n_gpu_layers, n_threads, seed, temperature, top_p, all recorded in `inference_results.json`
- macOS version on the Mac (Metal shader compilation can change between OS releases)
- GPU driver version on Linux and Windows

---

## Model Selection Rationale

**Why these three?**
- Phi-3-mini (3.8B), Qwen2.5-7B and Llama-3.1-8B are instruction-tuned open-weight models at three sizes, which show how speed and memory scale from 3.8B to 8B.
- Llama-3.1-8B is a widely deployed reference point, so its numbers can be compared against published llama.cpp community results.
- The Windows rerun also measured Qwen3-8B, a model in Potluck's catalog.

**Why Q4_K_M and not Q5_K_M or Q8_0?** Q4_K_M is what 13 of the 14 catalog models use, so benchmarking it keeps the numbers relevant. Q5 and Q8 need more memory and are not compared here. Comparing quantizations for output quality belongs in a quality benchmark, not a throughput benchmark.

---

## Updating this page

`make bench-inference` writes results to `bench/results/<timestamp>/`. The tables above are edited by hand after reviewing a run. A model update that changes a file's SHA-256 is documented explicitly.

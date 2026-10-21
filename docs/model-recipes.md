# Experimental local model recipes

A recipe fixes the model bytes, native engine commit, acceleration, context size and memory allowances together. It is an opt-in starting point for local testing. Passing preflight means the declared checks pass now; it does not establish model quality, reliable tool use, speed or successful loading.

The catalog is [model-recipes.json](../apps/desktop/python/potluck_local/data/model-recipes.json). Every entry is `experimental`. Recipes do not inherit qualification from a similarly named catalog model or a different runtime. The native backend keeps them ineligible for lending to other people.

## Use a recipe

```sh
potluck models recipes
potluck models preflight qwen2.5-0.5b-instruct-q4-k-m-cpu-4k
potluck models install-recipe qwen2.5-0.5b-instruct-q4-k-m-cpu-4k
potluck models load-recipe qwen2.5-0.5b-instruct-q4-k-m-cpu-4k
```

Listing and preflight do not download or load anything. Preflight reports disk, currently available RAM, platform, native engine and installed-artifact checks. A missing artifact can be installable while not loadable; the preflight command exits unsuccessfully until loading checks pass. Installation starts a background download of the exact revision and verifies its SHA-256. Loading is a separate explicit command, rechecks the artifact and available resources, and applies the recipe for this session. A successful load clears the previous startup model choice, so restarting cannot restore that model with different settings; other runtime preferences stay unchanged. An existing conflicting artifact is not overwritten.

These recipes target the native backend on Apple Silicon macOS and, for the CPU candidate only, Linux x86-64. The platform list is a preflight constraint, not a tested-platform claim. Windows and Intel Mac native recipes are not included. There is no automatic fallback to another model, backend or routing scope.

## Included candidates and evidence

| Recipe | Runtime | Estimated available RAM required | Evidence and limits |
| --- | --- | --- | --- |
| `qwen2.5-0.5b-instruct-q4-k-m-cpu-4k` | CPU, 4,096 tokens, 128 MiB prompt cache | 1,749,691,232 bytes (1.63 GiB) | Exact model and engine passed the macOS CPU protocol smoke below. Linux and coding quality remain unqualified. |
| `qwen3-8b-q4-k-m-metal-8k` | Apple Metal, 8,192 tokens, 256 MiB prompt cache | 8,651,662,144 bytes (8.06 GiB) | Official GGUF pin and Qwen3 architecture registration verified. This native recipe has no load, quality or speed result. |
| `muse-glimmer-30b-17gb-metal-4k` | Apple Metal, 4,096 tokens, 256 MiB prompt cache | 19,390,706,816 bytes (18.06 GiB) | Official text GGUF pin and Muse architecture registration verified. This native recipe has no load, quality or speed result. No image projector or speculative drafter. |

The small-model evidence is [native-local-macos-cpu-20261017.json](../bench/results/native-local-macos-cpu-20261017.json): macOS arm64, CPU, two threads, 4,096-token context and 128 MiB cache. It checks exact identity, tool-call/result exchange, cancellation and subsequent reuse, unloading, and the unqualified-lending gate. It is a protocol smoke, not a coding benchmark; its timings are not a performance claim. Recipes do not pin the thread count, so even this evidence is not a timing comparison for every recipe invocation.

The separate [recipe API smoke](../bench/results/model-recipe-macos-cpu-20261017.json)
uses the CPU recipe with existing weights in temporary storage. Its eleven checks
cover read-only inspection, exact artifact/context/engine, session-only loading,
preserved preferences, gateway inference and child-process cleanup. Reproduce it
with `bench/scripts/model_recipe_smoke.py --model <existing-gguf> --bundle <pinned-bundle> --output <result.json>`
using the sidecar Python environment and `PYTHONPATH=apps/desktop/python`.
It downloads nothing and does not use the app's running model or port.

## Immutable provenance

Metadata was checked against the Hugging Face model API at each exact revision on 2026-10-17 UTC. The file size and SHA-256 below are the publisher's LFS metadata, not hashes obtained by downloading new weights during recipe preparation. Installation independently checks the downloaded bytes. All three files are complete single-file GGUFs, not split shards.

| Artifact | Repository revision | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| [Qwen2.5 0.5B Instruct Q4_K_M](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/blob/9217f5db79a29953eb74d5343926648285ec7e67/qwen2.5-0.5b-instruct-q4_k_m.gguf) | `9217f5db79a29953eb74d5343926648285ec7e67` | 491400032 | `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db` |
| [Qwen3 8B Q4_K_M](https://huggingface.co/Qwen/Qwen3-8B-GGUF/blob/7c41481f57cb95916b40956ab2f0b139b296d974/Qwen3-8B-Q4_K_M.gguf) | `7c41481f57cb95916b40956ab2f0b139b296d974` | 5027783488 | `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785` |
| [Muse Glimmer 30B KQuant 17GB Q4_K_M](https://huggingface.co/meta-models/Muse-Glimmer-30B-GGUF/blob/70bf1b61ac09f91b24d39038091b41c582bc5d7a/Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf) | `70bf1b61ac09f91b24d39038091b41c582bc5d7a` | 16756683904 | `4cc57c0f51040a226e5a72cc47b7613f7772950e460a665f7083de89f183f60e` |

To audit a pin without downloading weights, request `https://huggingface.co/api/models/{repository}/revision/{revision}?blobs=true`, check that `sha` matches the revision, and locate the exact `siblings[].rfilename`. Compare both `size` and `lfs.size` with `size_bytes`, and `lfs.sha256` with the recipe hash. Downloads use `/resolve/{revision}/{filename}`; a moving branch such as `main` is never the recipe source.

Every recipe uses `llama-server` (llama.cpp b11756, ggml RPC 8.0.0) at [`d80619397c2ba5cf1366c29f7d43699ca3a159b9`](https://github.com/ggml-org/llama.cpp/tree/d80619397c2ba5cf1366c29f7d43699ca3a159b9), matching [Potluck's native bundle pin](../apps/desktop/python/potluck_local/data/split-engine.json). Its [architecture registry](https://github.com/ggml-org/llama.cpp/blob/d80619397c2ba5cf1366c29f7d43699ca3a159b9/src/llama-arch.cpp) includes `qwen2`, `qwen3` and `muse-glimmer`. Architecture registration is only a prerequisite: it does not prove that a given GGUF, template or accelerator works. The [pinned Muse publisher card](https://huggingface.co/meta-models/Muse-Glimmer-30B-GGUF/blob/70bf1b61ac09f91b24d39038091b41c582bc5d7a/README.md) describes the text file and its separate image and speculative-decoding companions; those companions are outside this recipe.

The `bench/results/*-20261017.json` files record measurements taken on the previous pin, `5a32f7b66ef6cfb3e60deea26e3454cc6ad3438c`. They are kept unchanged as history. Potluck's pin moved to the commit above afterwards, so those numbers do not describe the shipped engine, and they are being re-run on the new pin.

## Resource estimates

Preflight uses this allowance for system memory:

```text
model file bytes
+ context tokens × KV bytes per token
+ prompt cache MiB × 1,048,576
+ overhead bytes
```

Weights are counted once for CPU and Apple unified memory. The comparison is against live available RAM, not the machine's installed capacity. Other programs, the running model and allocations during startup can change availability. The displayed estimate is not a guarantee against allocation failures.

The KV allowance assumes full-context F16 keys and values: `2 × layers × KV heads × head dimension × 2 bytes`. Architecture dimensions come from the upstream immutable configs:

| Model | Layers | KV heads | Head dimension | KV bytes/token | Config |
| --- | ---: | ---: | ---: | ---: | --- |
| Qwen2.5 0.5B | 24 | 2 | 64 (`896 / 14`) | 12288 | [Qwen config](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct/blob/7ae557604adf67be50417f59c2c2f167def9a775/config.json) |
| Qwen3 8B | 36 | 8 | 128 | 147456 | [Qwen config](https://huggingface.co/Qwen/Qwen3-8B/blob/b968826d9c46dd6066d109eabc6255188de91218/config.json) |
| Muse Glimmer 30B text | 52 | 2 | 128 | 53248 | [Meta config](https://huggingface.co/meta-models/Muse-Glimmer-30B/blob/a4e59da52a7bc87ae7251dd5545c0dd437c44b68/config.json) |

Muse mixes local sliding-window and global attention. Its allowance deliberately gives every text layer a full-context KV cache instead of claiming a reduction from the sliding window. This is not an allowance for an image encoder or drafter. Runtime allocation layouts, graph/work buffers and accelerator allocations still need measurement. The 1 GiB overhead for the tiny CPU model and 2 GiB for each larger model are editorial reserves, not measured peak-memory limits. A recipe must be revised if qualification finds they underestimate the actual requirement. The upstream `17GB` quantization label is not a total-memory requirement.

## Qualification still to do

Qualification must exercise these exact artifacts and engine builds on the proposed hardware: load/unload, tool calls and results, cancellation/recovery, context boundaries, peak memory, and real repository edit/test tasks. Record hardware, settings, artifact hash, engine pin and results before making compatibility, quality or performance claims. Existing catalog checks and upstream benchmark scores do not substitute for this work.

Qwen3.8-27B remains a separate candidate. A [pinned Unsloth GGUF repository](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/tree/4ca720788d1e01f1bff70c033e0d0028fd02e502) was inspected, but this batch does not establish its engine behavior or hybrid-attention/state memory allowance. It is not included as an install/load recipe yet. Speculative decoding, vision and additional coding-agent integrations are also separate work.

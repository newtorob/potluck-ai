# Windows GPU inference

How Potluck picks a llama.cpp backend on Windows, what it is allowed to claim
about the result, and how the installer is built.

## Why there are two backend sets

The default `llama-cpp-python` wheel for Windows is a CPU-only llama.cpp build. Its `llama_cpp/lib/` contains `ggml-base.dll`, `ggml-cpu.dll`, `ggml.dll`, `llama.dll` and `mtmd.dll`. There is no `ggml-cuda.dll` and no `ggml-vulkan.dll`, so `llama_supports_gpu_offload()` returns false and `n_gpu_layers=-1` has nothing to offload to. Installing the wheel from `requirements.txt` alone gives an engine that runs every model on the CPU.

A GPU backend therefore has to be vendored beside the CPU one, selected at run time, and checked at build time. Two details of the app also make a CPU-only engine hard to notice from the outside, so the engine reports what actually ran:

- `GET /readiness/gpu-test` names a device by pattern-matching the installed graphics card, so a machine with an NVIDIA card reports `cuda` whatever the backend can do.
- `gpu_layers = -1` reads as "offload everything", when it only means "ask for everything".

## What ships now

Two complete backend sets, side by side in the bundle:

| Directory | Contents | Source |
|---|---|---|
| `llama_cpp/lib` | CPU only | the wheel from `requirements.txt` |
| `llama_cpp/lib-vulkan` | Vulkan | `scripts/vendor_gpu_backend.py`, pinned |

Both come from llama-cpp-python 0.3.35 built against llama.cpp `b10454`
(`4df29be4f4c3673f428170fda944a5b19f743bb8`). One wrapper version, one engine
version, two device backends, so a CPU/GPU comparison measures devices rather
than versions.

### Why Vulkan and not CUDA

One Vulkan build covers NVIDIA, AMD and Intel. It needs no vendor runtime
beyond the graphics driver the user already has, and it adds 38 MB to the
installer.

The CUDA wheel (`v0.3.35-cu124`) is 482,736,710 bytes because it bundles the
CUDA runtime. That would add about 476 MB to the installer for one vendor, and
it has not been verified on NVIDIA hardware under Windows, so it is not shipped.

`cu124` is the CUDA variant to test. CUDA 12.x still targets Pascal (`sm_61`,
for example the GTX 1080). CUDA 13 dropped Maxwell, Pascal and Volta, so a
`cu130` or later wheel would not run on those cards.

### How the backend is chosen

`potluck_local/services/llama_backend.py`, called from `main()` before anything
imports `llama_cpp`. The ordering matters: llama-cpp-python reads
`LLAMA_CPP_LIB_PATH` at import time and resolves its native library once.

1. No extra backend sets installed (macOS, Linux) means no decision to make.
   The wheel's own library is used, untouched.
2. `POTLUCK_LLAMA_BACKEND=cpu` forces the CPU set. `POTLUCK_LLAMA_BACKEND=vulkan`
   forces the Vulkan set without probing.
3. Otherwise the sidecar re-invokes itself with `--probe-inference-backend`,
   which loads the Vulkan set in a child process and asks ggml for its device
   list. The GPU set is selected only when a real GPU device comes back.
4. Anything else, including a probe that crashes or never answers, selects CPU.

The probe runs in a child process because `ggml.dll` in the Vulkan set links
`ggml-vulkan.dll`, which links `vulkan-1.dll`. On a machine with no Vulkan
driver (a VM on the Microsoft Basic Display Adapter, a stripped image) the
import fails outright, and a driver that faults during enumeration would take
the sidecar with it. The sidecar has to start either way.

The result is cached at `~/.potluck/data/inference-backend.json`, keyed on the
contents of the backend directories, so a rebuilt bundle re-probes. The cache
expires after seven days, which is the coarse mechanism by which a driver
install eventually gets noticed.

### How many layers get offloaded

`gpu_layers = -1` now means "as many as fit", not "ask for all and hope".

A model whose weights do not fit in VRAM streams them across PCIe on every
token, which is slower than staying on the CPU.

Weights are not the whole cost. The context auto-tuner sizes `n_ctx` from
system RAM, and on a 64 GB machine it picks 131072 for a 4B model. At that
context the KV cache is 18 GB, against 2.3 GB of weights, on a card with 8 GB.
The AMD driver accepts the allocation and backs the overflow with host memory,
so it looks fine until a conversation gets long enough to touch it.

So `plan_gpu_layers` budgets weights plus KV cache against reported free VRAM
minus a 1 GiB reserve, and gives way in this order:

1. Everything on the card at the requested context, when it fits.
2. The context reduced until weights plus KV fit, down to a floor of 8192.
3. At that floor, as many layers as fit.
4. The CPU.

KV size comes from the GGUF header (`block_count`, `attention.head_count_kv`,
`attention.key_length`, `attention.value_length`), using llama.cpp's own
arithmetic. The unit test pins it against a figure llama.cpp printed: Qwen3 4B
at 131072 tokens logs `Vulkan0 KV buffer size = 18432.00 MiB`. When a header
does not carry enough to size it, the plan falls back to weights only and the
reason says so.

The graph compute buffer is llama.cpp's own allocation and is not part of this
budget, which is why the reserve exists rather than being zero. On the 4B model
at the fitted 24576-token context it came to 3154 MiB on the device and 62 MiB
on the host, against 1024 MiB of reserve. It is reported in
`/health/inference` so the gap is visible rather than assumed; sizing it
properly needs measurements across more models and cards than one.

An explicit positive `gpu_layers` is a user pin: neither the layer count nor
the context is touched. macOS Metal and the Linux build are untouched too, the
plan only applies to a backend this module selected after a probe.

One limitation. "Free VRAM" here is whatever `ggml_backend_dev_memory`
reports. Measured on an RX 6600 XT on Windows on 2026-09-13, that figure was
identical with a game holding a large share of the card and with the machine
idle, so it is not a live system-wide budget. The reserve absorbs KV cache and
compute buffers; it does not protect against another application already
holding several GB. On a busy card, pin `gpu_layers`.

## What the app is allowed to claim

Three things that used to be conflated are now separate: what this build can
do, what was asked for, and what actually happened.

`GET /health` includes an additive `inference` block:

```json
{
  "model_loaded": true,
  "active_model_id": "qwen3-4b-instruct-2507-q4",
  "n_ctx_loaded": 4096,
  "inference": {
    "schema_version": 1,
    "model_id": "qwen3-4b-instruct-2507-q4",
    "execution_verified": true,
    "device_kind": "gpu",
    "device_name": "AMD Radeon RX 6600 XT",
    "backend": "vulkan"
  }
}
```

`execution_verified` is true only when llama.cpp's own load output accounted
for this exact model's weights. The evidence is the `load_tensors: <device>
model buffer size = N MiB` lines and the `offloaded N/M layers to GPU` line,
captured through the ggml log callback during the load. A detected graphics
card, a backend that supports offload, and a requested layer count can all be
true while the model runs entirely on the CPU, so none of them set it.

`device_kind` is `gpu` when every layer was offloaded, `cpu` when none were,
`mixed` in between, and `unknown` when the engine reported nothing. The
`backend` label is derived from the device name that holds the weights
(`Vulkan0` becomes `vulkan`, `CUDA0` becomes `cuda`), so it describes what ran
rather than what was installed. A suffix that says how the buffer is held is
dropped first: Metal names its weights `MTL0_Mapped`, which becomes the device
`MTL0` and the label `mtl`, and the device is looked up by that name, so a Mac
reports "Apple M2 Pro". `CUDA_Host` and `Vulkan_Host` buffers are pinned system
memory and do not count as device memory.

`GET /health/inference` carries the whole picture for the diagnostics UI: the
selected backend and why, the ggml device list with memory, the requested and
used layer counts, the offload plan and its reasoning, the per-device weight,
KV and compute buffers, and the last measured benchmark for the loaded model.
Anything not measurable right now is null rather than inferred.

KV and compute buffers are reported separately from weights and do not affect
`device_kind`, which is about where the model ran. They matter because a long
context can put more on the card than the weights do, which is how a model that
"fits" still spills.

Neither response carries model paths, prompts or credentials.

`GET /readiness/gpu-test` still guesses the device from installed hardware.
Clients should read the `inference` block of `/health` instead. When a
model is loaded it also carries the answer check result under `acceptance`,
next to its one-token probe, which only shows that a token came out.

## Checking that the answers are right

A GPU backend can generate at full speed and still return wrong text. With an
old AMD driver the Vulkan backend repeated adjacent tokens in every reply,
while the CPU on the same machine gave correct text. A test that passes on any
non-empty text does not catch this. A newer driver fixed it. The engine looks
at the answers before it trusts a setup.

**What it asks.** After a model loads, and before any request is served, the
engine asks it a few questions with known answers at temperature 0. Three must
pass: copy a number, answer 2 + 2, and write two short sentences without words
doubled (a reply with at least four adjacent identical words, and at least 15%
of its word pairs, fails). Three more are recorded and do not decide the
result: the capital of France, whether an answer ends by itself, and finding a
code planted in a long prompt. The questions are easy on purpose, so the
smallest model in the catalog passes on a healthy machine. On a small model the
quick level takes about a second on a GPU and a second or two on a CPU. On a
big model on a CPU it takes as long as generating about 50 tokens.

**The fallback.** When a required check fails on a load with layers on a GPU,
the engine loads the same model on the CPU, with no layers on the GPU and no GPU
help with prompt batches, and asks again.

- Right on the CPU: the GPU is the problem. The model stays on the CPU and
  `/health/inference` reports `acceptance.status` `passed`, `verdict`
  `gpu_wrong_output`, a `fallback` from `gpu` to `cpu`, and the sentence "The GPU
  gave wrong answers for this model, so it is running on the CPU, which is
  slower. Updating the graphics driver usually fixes this."
- Wrong on the CPU too: the model is the problem. The engine puts the planned
  GPU load back, because it is faster, and reports `failed` with verdict
  `model_failed` and the sentence "This model did not pass its answer check on
  this machine. You can still use it yourself. It is not offered to other
  people."
- A failure on a load that was already CPU only is `model_failed`.

If the CPU copy cannot be loaded, the GPU load is put back and the result is
`model_failed`. If that cannot be loaded either, nothing is loaded and the load
call fails.

**Proofs.** Each result is kept in `data/acceptance-proofs.json` in the engine
data directory, tied to everything that could change the answer: the model
file's SHA-256, the backend, the device name, the layer count handed to
llama.cpp, the driver, the llama-cpp-python version, the backend library
signature, the version of the checks, and the level. The driver is read from the
registry's display class key on Windows (`DriverVersion` and `DriverDate`,
matched to the device by name), from `/proc/driver/nvidia/version` or the kernel
driver on Linux, and from the OS version and build on macOS. A driver change
also makes the engine probe the backends again.

- A passing proof is reused for 90 days (30 when the driver could not be read).
  A load then reports `from_proof: true` and runs nothing.
- A proof that the GPU was wrong sends the next load straight to the CPU, so
  nobody waits for a GPU known to be bad. Any change to the key, a new driver
  for example, tries the GPU again and checks it again.
- A proof that the model failed is reused for 7 days, so a model that fails
  does not load three times at every start.
- `POST /models/{slug}/test` with `{"force": true}` ignores the store and runs
  now, and goes back to the GPU first if the model is on a fallback.
  `{"level": "full"}` fills about 85% of the loaded context (at most 32768
  tokens) with a long prompt.
- A proof also keeps what the load cost this machine, measured while the model
  loaded and answered the checks: how far free memory fell, the engine's
  resident growth, the GPU memory where it can be read (the engine's own share on
  Windows, the drop in the device's free figure elsewhere, null when neither is
  available), and the context size and layer split it was measured at. A reused
  proof keeps its measurement. `GET /models/setup-options` judges fit from that
  figure plus a margin (10%, at least 0.5 GiB) for models that have one, and says
  so in `fitBasis`. The last benchmark of a model is kept the same way, in
  `data/benchmark-results.json`, so it is still there after a restart.

**The setting.** `runtime.verify_answers`, on by default. Off means no checks and
no fallback; the status is `skipped` and the model is not offered to other
people.

**Other people.** A model that has not passed is not lent. Circle requests to
this machine and pool jobs it is sent are refused with the category
`model_not_verified` and the text "The serving machine has not verified this
model." The machine also stops offering the model to the coordinator, and its
circle capabilities say `available_for_inference: false`, so the refusals are
the second line. The owner's own machines are served either way. The contributor
status reports `pause_reason` `model_not_verified`, and `/peer/capabilities`
carries `answers_verified`.

Three kinds of load are not checked: a managed split, a pipeline or whole-model
load, and a load with `rpc_servers`. They report `not_run` and `answers_verified`
null, and they are lent as they were before the check existed. A single machine
load with the check off, failed or still running is not lent.

## Measured

RX 6600 XT (8 GB) and Ryzen 7 5800X, Windows 11 26200, AMD driver
32.0.21045.5002. llama-cpp-python 0.3.35 in both arms. 314-token prompt,
64 tokens generated, `n_ctx` 2048, 8 threads, temperature 0. Cold is a first
load, warm is a second process against the same file; the two were within
noise, so one figure is given.

| Model | CPU prompt | Vulkan prompt | CPU generation | Vulkan generation | Total request |
|---|---:|---:|---:|---:|---|
| Qwen3 1.7B Q4_K_M | 215 tok/s | 2031 tok/s | 21.8 tok/s | 80.9 tok/s | 4.42 s to 0.94 s |
| Qwen3 4B Instruct Q4_K_M | 98 tok/s | 929 tok/s | 11.7 tok/s | 68.1 tok/s | 8.77 s to 1.28 s |
| Llama 3.1 8B Q5_K_M | 24 tok/s | 430 tok/s | 5.5 tok/s | 27.4 tok/s | 24.19 s to 3.06 s |

All three Vulkan runs offloaded every layer (29/29, 37/37, 33/33).

### The packaged executable, through the gateway

The numbers above come from a bench harness. These come from the executable
electron-builder produced, started directly out of `win-unpacked` with no
developer virtualenv on `PATH`, against a scratch `POTLUCK_DATA_DIR` so the
installed app's settings and models were untouched. Qwen3 4B Instruct Q4_K_M,
auto-tuned `n_ctx` of 131072, a prompt of 127 tokens, through
`POST /v1/chat/completions` with `X-Potluck-Scope: local`.

| | Load | First request | Second request | Effective output |
|---|---:|---:|---:|---|
| Vulkan (selected by probe) | 4.28 s | 2.32 s | 1.74 s | 45 to 53 tok/s |
| CPU (`POTLUCK_LLAMA_BACKEND=cpu`) | 6.22 s | 8.13 s | 9.80 s | 11 to 13 tok/s |

Both replies were coherent. Effective output is completion tokens over total
request wall clock for a non-streaming request, so it includes prompt
processing and is not a generation rate.

The Vulkan run reported `offloaded 37/37`, 2375.91 MiB of weights on `Vulkan0`
and 304.28 MiB on `CPU_Mapped` (the token embedding table), and
`execution_verified: true`, `device_kind: "gpu"`,
`device_name: "AMD Radeon RX 6600 XT"`. Forcing the CPU backend on the same
binary produced `execution_verified: true`, `device_kind: "cpu"`,
`supports_gpu_offload: false`.

Reporting KV and compute buffers showed the KV cache problem as numbers:
`Vulkan0 KV buffer size = 18432.00 MiB` on a card with 8573 MB, plus an
8977 MiB host compute buffer, and a `ggml_vulkan: Failed to allocate pinned
memory` in the log. The requests were fast anyway because a 127-token prompt
touches almost none of a 131072-token KV cache. The offload plan accounts for
the KV cache so that it does not depend on that.

### Check for other GPU load before benchmarking

An earlier measurement showed Vulkan as *slower* than CPU on the same machine:
2.93 generation tok/s against 6.22 for Llama 3.1 8B Q5. That number reproduced
exactly, twice, including with this pinned wheel.

It was contention. Another application was holding about 51% of the GPU's 3D
engine and the desktop window manager another 25%, so the GPU was roughly 80%
busy before any benchmark started. CPU inference on a 16-thread 5800X was
unaffected, so the comparison was structurally unfair to the GPU. With the other
application closed the same run gives 27.4 tok/s.

Before trusting any Windows GPU benchmark, check what else is using the card:

```powershell
(Get-Counter "\GPU Engine(*)\Utilization Percentage").CounterSamples |
  Where-Object { $_.CookedValue -gt 1 } |
  ForEach-Object {
    $procId = ($_.InstanceName -split "_")[1]
    "{0,6:N1}%  {1}" -f $_.CookedValue, (Get-Process -Id $procId -EA SilentlyContinue).ProcessName
  }
```

This also matters in normal use. A game or any other GPU application on the
same machine competes with Potluck for VRAM and compute.

## Not tested

- **NVIDIA on Windows.** Vulkan is well supported on Pascal and the backend
  selection is vendor-neutral, but no number in this document was measured on
  NVIDIA hardware under Windows and none should be quoted as if it was.
- **Intel integrated graphics.** Same reason.
- **The answer check against a real faulty driver.** The faulty AMD driver was
  not available to test against. The fallback was exercised on real models with
  a GPU that was made to return doubled words, and the CPU isolation
  (`op_offload` off) was not confirmed on Vulkan or CUDA.
- **A machine with no Vulkan driver at all.** The CPU fallback path is covered
  by unit tests against a probe that reports a load failure, not by a real
  driverless machine.

## Building the installer

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-windows-installer.ps1
```

From a clean checkout: the script refuses a dirty tree unless `-AllowDirty` is
passed, because `build-info.json` stamps the dirty flag into the release. Pass
`-SkipDeps` to reuse an existing venv and `node_modules`.

The Makefile's `installer-bundle` target drives macOS and Linux through pnpm's
`prepack` chain, which is POSIX shell (`rm -rf`, `mkdir -p`, `bash`). pnpm on
Windows runs scripts through `cmd.exe`, so that chain does not work there. The
PowerShell script runs the same steps explicitly, in order.

The script fails the build if `llama_cpp/lib-*` is missing from the bundle or
if the CPU fallback is missing, so an installer without a GPU backend is not
produced silently. `build.py` also runs `vendor_gpu_backend.py --check` on the
PyInstaller output, so a direct run is checked too.

Requirements: Python 3.11+, Node 20+, pnpm, Go, Git. No C toolchain: the Python
sidecar is packaged from prebuilt wheels and the mesh sidecar needs no cgo.

## Bumping llama-cpp-python

`requirements.txt` pins Windows to an exact version, and
`scripts/vendor_gpu_backend.py` pins the matching backend wheel by URL, byte
size and SHA-256. Move both together. The vendor script refuses to install when
the installed wrapper version does not match its pin, because pairing a wrapper
with a native library from a different llama.cpp commit gives silent memory
corruption rather than a clear import error.

To re-pin: pick the new `llama-cpp-python` version, confirm the
`v<version>-vulkan` tag exists, download
`https://github.com/abetlen/llama-cpp-python/releases/download/v<version>-vulkan/llama_cpp_python-<version>-py3-none-win_amd64.whl`,
record its size and SHA-256, and update `WRAPPER_VERSION`, `LLAMA_CPP_COMMIT`
and the `ARTIFACTS` table.

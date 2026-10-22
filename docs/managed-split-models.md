# Run one model across your own machines

This workflow runs on Mac and Linux. All participating computers need a
split-capable runtime built with the same pinned native engine. For the design
and security model, see [managed split models](architecture/managed-split-models.md).

A **head** keeps the complete GGUF file and serves requests. One to three
**workers** hold assigned model weights in memory and execute part of the same
inference. Workers do not need to download a separate model file. All computers
must belong to your Potluck household and be reachable through its mesh.
Splitting increases available capacity; it does not guarantee faster responses.

## Enable each worker

Start its runtime and verify its household connection:

```sh
potluck up
potluck household list
potluck split status
```

Unload any local model with `potluck models unload <model-id>`, then opt in:

```sh
potluck split worker on
```

The opt-in is saved on that computer. While reserved, the worker cannot load
an ordinary local model or join another split. To stop sharing:

```sh
potluck split worker off
```

This interrupts any split currently using that worker.

## Start from the project on the head

Install the intended model on the head. Pin its exact bytes in the project,
or use `potluck models sync` if the project already has a `potluck.lock`.
Unload the head's current model before starting a split.

```sh
potluck up
potluck models pin <installed-model-id>
potluck gateway enable
potluck split plan <installed-model-id> --peer <worker-name>
potluck split start <installed-model-id> --peer <worker-name>
potluck split status
```

Use names from `potluck household list`, or a worker's Potluck mesh IP if its
name is ambiguous. Repeat `--peer` to select additional workers. `--context`
accepts 512 to 8192 tokens and defaults to 2048. More context needs more memory.

`plan` checks the installed pin and each worker without reserving compute.
For Metal and CPU workers, it shows available system memory, the 2 GiB
headroom target, and the remaining budget for weights, context and working
memory. That snapshot is checked again when the worker is reserved. CUDA
workers use native free VRAM during loading; system RAM is not a VRAM estimate.

`start` loads in the background. Wait for `ready`; `loading` is not a successful
inference. `potluck split status` shows model weights allocated to the head's
GPU, its CPU, and the selected workers. **Workers** is an aggregate that may
include CPU or GPU backends; it does not mean all those weights are on a GPU.
Only a ready split labels the placement verified. `--json` includes the raw
allocation and transport counts for each worker. A worker that receives no
model weights causes loading to fail rather than being silently ignored.

If the ready split still has CPU weights, free memory on GPU workers or select
additional compatible GPU workers, then restart and check placement again.
A larger memory budget may move more layers to GPUs, but the amount depends on
layer sizes and runtime buffers; measure response speed separately.

Loading budgets Metal and CPU workers against available system RAM reported
when their lease is acquired, targeting 2 GiB of headroom. CUDA devices target
at least 1 GiB of free VRAM. The native engine can keep overflow layers on the
head's CPU; the selected context and exact model bytes stay unchanged. These
are startup estimates, not a reservation against memory used by other apps.
Check the reported placement before drawing conclusions about GPU fit or speed.

Once ready, use the gateway, coding agent, or plain inference from the head:

```sh
potluck run "Explain this function" --scope local
potluck --scope local
```

Here `local` selects the head's loaded model, which you explicitly configured
to use the selected workers. Requests retain the project's exact model hash.
The split adapter does not route through a cloud model, a trusted circle or the
open pool. Existing household gateway routing can also reach this loaded head.

The coding agent waits for the response's completion marker before executing a
tool. It suppresses output after the first action while allowing inference to
finish normally, so an ordinary tool turn does not cancel the split. If the
stream drops, the pending action is not executed. For coding tasks, provision
enough context for the system prompt, file contents, and tool history; the
repository-edit evaluation suite uses `--context 8192`.

```sh
potluck split stop
```

Cancelling a request interrupts that request and discards its remaining output.
The split stays loaded for the next request when the engine confirms it is idle;
the selected model and workers remain unchanged. Cancellation waits up to three
seconds for that confirmation before forcing the engine to stop. A very early
cancellation may also stop the split if the engine has not yet confirmed that it
accepted the request. In either case, explicitly start the split again. Partial
output is never resumed or replayed.

`stop` releases all workers. It leaves their saved opt-in unchanged. Restarting
the runtime does not automatically recreate a split. Worker loss fails the
current request and ends the split. Restore the worker and explicitly start again.

## Build and validate

The versioned CLI repository-edit suite and its sandboxed runner are documented
in [the evaluation README](../apps/cli/eval/repo-edit-v1/README.md). Run it against
the pinned baseline and split model to check actual edits and test feedback,
in addition to plain-inference smoke tests.

The exact engine commit and protocol are in
`apps/desktop/python/potluck_local/data/split-engine.json`. With a local llama.cpp
Git checkout containing that commit, build on the target machine:

```sh
python3 scripts/build-split-engine.py --source /path/to/llama.cpp --backend metal
# Linux: use --backend cpu, or --backend cuda with the installed CUDA toolchain.
# Windows or Linux with an AMD or Intel GPU: --backend vulkan with the Vulkan SDK.
cd apps/desktop/python
.venv/bin/python build.py
```

The engine builder reads the pinned commit using `git archive`; it does not
change the source checkout. It stages both executables, their license and a
manifest under `native/split`. Use `--output` for a separate build directory;
the builder refuses to overwrite an existing bundle. For CUDA, select the
target architectures with `--cuda-architectures` and a compatible host compiler
with `--cuda-host-compiler` when required by that toolkit.

A Vulkan engine budgets each GPU by its memory type. A dedicated card is
budgeted like CUDA, from its free VRAM. An integrated GPU (Intel Arc or Iris,
AMD APU) shares system memory and is budgeted like Metal. The engine reads
which kind it has from ggml-vulkan's device line (`uma: 1` for an integrated
GPU) and refuses to plan a split if it cannot tell.

The CLI release packager requires that engine inside the frozen runtime and
records its identity. macOS release signing must cover both nested executables;
the `sign-sidecars.sh` script in `apps/desktop/electron/scripts` finds and signs
all Mach-O files. A passing source test does not replace testing the packaged
build on real hardware.

For isolated development on a different port, configure both
`POTLUCK_SIDECAR_URL` and `POTLUCK_GATEWAY_URL`. An isolated worker port also
requires `POTLUCK_SPLIT_PEER_PORT` on the head. These overrides are not required
for normal installed use.

## Troubleshooting

- Missing engine: install the same split-capable runtime on every computer.
- Missing worker: check the live mesh, household membership and its opt-in.
- Busy computer: unload its ordinary model or stop its existing split.
- Pin mismatch: run `potluck models lock` to see the pin and `potluck models sync` on the head to install matching bytes.
- Load failure: check available GPU/system memory, reduce context, and check
  `potluck split status` on each computer. The first load transfers weights over
  your network and can take several minutes.
- Head GPU memory exhausted: another runtime or application may still own VRAM
  even when this Potluck runtime has no model loaded. Unload it or free memory
  before retrying. Potluck does not stop other applications automatically.
- Linux roster unavailable: run `potluck doctor`. Legacy service-account mesh
  installs may need a narrow Unix-socket ACL for the signed-in local user; do
  not make the socket world-readable/writable or bypass membership checks.

Native RPC stays on loopback. Potluck carries it over authenticated, leased
connections on the household mesh. This feature does not require opening a raw
RPC port on your LAN or router. Scoring verification for open-pool contributions
is not supported by the split adapter.

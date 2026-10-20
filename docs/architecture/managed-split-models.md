# Managed household split models

One pinned GGUF is loaded by a head on the user's own mesh and executed with
one to three explicitly opted-in workers. Initial support is Mac/Linux. This
does not enable circles, open-pool contribution or arbitrary remote endpoints.
For setup steps, see [Run one model across your own machines](../managed-split-models.md).

The CLI runs `split worker on|off` on workers and `split plan|start <model>
--peer <owned-name>` on the head. A project `potluck.lock` must match installed
head bytes. Plans are read-only; start creates a background load; status reports
loading, ready or failed; stop releases the group. Existing local/household
gateway requests use the loaded split model through the normal runtime slot.
The runtime never downloads, swaps the pin or broadens the routing scope.

The native engine is llama.cpp commit
`5a32f7b66ef6cfb3e60deea26e3454cc6ad3438c`, built with RPC and Metal/CUDA/CPU.
The build script archives this exact commit without changing a developer's
checkout. Bundles carry the same engine protocol and commit; mixed builds fail
closed. No patched Python binding is needed. CLI/runtime packaging includes
both native executables, license and engine manifest. macOS release signing
must cover these nested executables. Source builds use an explicit engine dir;
packaged installs require no environment variables or developer toolchain.

Native RPC is experimental upstream and must not be exposed to open networks.
Every native worker binds 127.0.0.1. A WebSocket relay on Potluck's existing mesh
sidecar requires live owned-machine membership, a source-bound random lease,
explicit worker opt-in and an unexpired reservation. HTTP middleware does not
protect WebSockets, so the WebSocket handler repeats these checks itself.
No worker credential, prompt, tensor bytes or tokens enter logs. Only filtered
model-allocation counters are retained in memory. Turning worker sharing off
closes its current transport and native process.

Native head processes also bind loopback, with an ephemeral API key. A separate
watchdog holds each native process group: sidecar pipe EOF, termination, or
45 seconds without a heartbeat kills the group. Worker leases last 30 seconds
and renew every five seconds. Loss of a lease stops the head and reports failure.
Partial output is never replayed as a continuation. Cancellation stops this
first-version split; users explicitly start again. Runtime restart does not
automatically reassemble the group or reactivate a previous local model.

Worker reservations exclude ordinary model loads. The head holds the existing
model-command gate while loading, verifies the pinned file again before making
the adapter available, and reports actual RPC allocation and transport counters.
Insufficient memory, unavailable workers or silently local-only placement is a
load failure. Placement capacity and latency must be measured separately.

## Tests

`apps/desktop/python/tests/test_managed_split.py` covers worker opt-in, lease
binding and expiry, pin validation before any network call, local-control and
WebSocket authentication, failing closed on a missing or mixed engine, stream
completion, cancellation and cleanup, and the supervisor killing the native
process on pipe EOF.

## Equivalence tests

`bench/scripts/split_equivalence.py` has two levels, after DeepInfer's
`yoco_equiv.py`.

Potluck does not compute layer ranges. It fixes the device order (head first,
then workers in the order selected) and a memory margin per device
(`split_memory.fit_arguments`), and the pinned engine's `--fit` picks the tensor
split. The engine then applies a fixed rule: layer `il` goes to the first device
whose cumulative split fraction exceeds `il / (n_layer + 1)`, the output head is
slot `n_layer`, and the token embedding stays on the CPU. Consequence: the output
head lands on the last device in the order whenever that device holds at least one
slot's worth of the split, which for a head plus workers means a worker. A split
that runs slowly should be checked for this first.
`services/split_layout.py` restates the rule so a plan can be read.

**L1, plan metadata (seconds, no weights, binaries or GPUs).** Runs in the normal
sidecar suite and as `python bench/scripts/split_equivalence.py l1 [--model
file.gguf --budget-mib 6000,4000]`. It checks, over a matrix of synthetic device
inventories, backends, memory budgets and model shapes: every layer and the head
assigned exactly once, ranges contiguous and in device order, the output head on
the documented device, zero-share devices empty, each device inside its budget
(or a refusal when nothing fits), the same input giving the same plan, and the
head's launch arguments (loopback only, layer split, one RPC endpoint per
device). Tests: `test_split_layout.py`, and the additions to
`test_split_memory.py`.

**L2, teacher-forced output.** Runs `llama-perplexity --kl-divergence-base` for a
reference config over a fixed text, then `--kl-divergence` for each candidate, and
reports mean and max KL divergence, top-1 agreement and the PPL ratio. A
config is `{bin_dir, model, args, rpc_servers}`, so the same command compares any
reference with any candidate. It also logs which device got each layer and checks
that against the L1 rule. rpc-servers are local, in ports 18100 to 18199, and
stopped by PID.

```
python bench/scripts/split_equivalence.py build --source ~/llama.cpp
python bench/scripts/split_equivalence.py l2 --suite local-split --model <model.gguf>
python bench/scripts/split_equivalence.py l2 --reference ref.json --candidate cand.json \
    --max-mean-kld 1e-4 --max-max-kld 1e-3 --min-top1 99.9 --max-abs-ln-ppl-ratio 0.005
```

Mac results (M2 Pro, Metal, pin `5a32f7b`, 16 chunks of 512 tokens over 29,921
bytes of `docs/architecture`; `bench/results/split-equivalence-macos-metal-20261020.json`).
Candidates: two-way even, three-way even, two-way 70/30, all layers on the RPC
worker, and the arguments Potluck's planner produces for this Mac. Every
candidate matched the whole-model reference exactly as far as the tool prints:

| Model | Mean KLD | Max KLD | Top-1 agreement | PPL difference |
| --- | --- | --- | --- | --- |
| qwen-2.5-0.5b-instruct-q4 | 0.000000 | 0.000053 | 100.000% | 0.003078 |
| qwen-2.5-1.5b-instruct-q4 | -0.000000 | 0.000042 | 99.964% | 0.006738 |

A second whole-model run (the noise floor) gives the same figures: they come from
the reference file storing log-probabilities as 16-bit values, not from the
split. The observed layer ranges matched the L1 rule in every case with a fixed
tensor split; the output head sat on the last device each time (for example
layers 0-12 on MTL0, 13-23 and the head on RPC0). As a negative control, 1.5b as
reference against 0.5b as candidate gave mean KLD 0.494, max 5.318, top-1 63.03%,
and fails every threshold.

Thresholds for "split matches whole" on one machine and one backend: mean KLD
at most 1e-4, max KLD at most 1e-3, top-1 at least 99.9%, and
|mean ln(PPL(candidate)/PPL(reference))| at most 0.005. The PPL check is a ratio
because an absolute difference grows with PPL and with fewer chunks: a
whole-model repeat at 8 chunks differed by 0.051 at PPL 53.9 (ratio 0.00094),
which an absolute limit of 0.05 failed. The measured floor is a ratio of at most
0.00094 and the negative control is 0.298, so the limits sit above the floor and
far below the control. The results file records the first run's verdicts under
the earlier absolute limit. Different
backends (CUDA against Metal) or a compressed build will not be exact; set looser
thresholds for those from their own floor. Not measured here: cross-vendor splits
and any speed figure.

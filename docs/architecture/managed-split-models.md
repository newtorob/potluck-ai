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

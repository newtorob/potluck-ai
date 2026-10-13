# Mid-job worker loss and recovery

Status: strict gateway stream completion, bounded direct My Machines retry and an explicit household answer restart are implemented. Coordinator restart and cancellation, split-group recovery and token-prefix replay are planned and not built.

## Current behavior

| Path | What happens when a worker disappears |
| --- | --- |
| Coordinator `/jobs/stream`, before output | Tries up to three eligible peers, excluding failed peers. Each first-token budget depends on prompt size and measured throughput, with a 5-second floor and 90-second cap. There is no shared deadline across these attempts. |
| Coordinator `/jobs/stream`, after output | WebSocket disconnect drains active jobs, sends a terminal error, and settles the attempt as failed without successful-job credit. The desktop preserves partial output. No automatic continuation or restart. |
| Silent coordinator-connected worker | A 60-second heartbeat TTL excludes stale peers from new jobs; the enabled reaper checks every 30 seconds and drains existing jobs. The stream also has a 120-second deadline after dispatch. These are backstops, not immediate progress detection. |
| Direct My Machines | Desktop and gateway try up to three machines serving the same model slug before output. First-output allowance is at most 90 seconds (derived from prompt size and measured throughput when available); idle allowance is 30 seconds; the dispatch has a shared 240-second budget. Errors after output preserve partial text without silently retrying. The desktop offers an explicit restart on another household computer. |
| Python pipeline split model | Edge failures/timeouts raise transport errors. Continuous batching fails affected active sequences and resets the socket for future work; it does not recover those sequences. |
| llama.cpp RPC split model | Loads a fixed set of RPC endpoints. No application-level replacement/replay supervisor was found. Actual native worker-loss behavior requires fault injection. |

The Python gateway rejects EOF without DONE on direct streams and requires both successful metadata and DONE on coordinator streams. Error envelopes remain authoritative; finish reason and available usage are retained. OpenAI and Ollama endpoints report interruptions instead of successful completion. The synchronous coordinator endpoint still does not share the coordinator streaming failover loop.

The household restart snapshot is session-only and preserves the exact messages, model slug, routing mode and history permission. A restart appends a distinct assistant attempt without repeating the user message or discarding partial output. It excludes machines already attempted, reads current household membership, prevents double-click dispatch, and stays in My Machines even if the composer scope changed. If no replacement is available, the snapshot remains retryable. Completed and missing snapshots cannot be restarted. History-off requests stay ephemeral even if history is subsequently enabled.

Current same-model matching is by the existing catalog slug and peer endpoint model check. This is not an exact weights/tokenizer/runtime fingerprint; that stronger identity and engine state contract is required before prefix replay. Gateway response headers say `household` rather than naming the first selected machine, which may be replaced before output.

## Recommended contract

The direct My Machines pre-output retry and the household restart are implemented. The rest of this contract is planned.

**A request survives a serving computer disappearing when compatible replacement capacity exists within its authorized scope.** Preserve partial output and completed workflow steps, expose recovery state, and stop cleanly when capacity or the recovery budget runs out. Do not promise a fixed pause or uninterrupted generation.

1. **Keep ownership outside the worker.** The requesting device holds an immutable request and policy snapshot, a logical request ID, and the accepted output. Each execution gets a separate opaque attempt ID. The coordinator retains its existing blinded peer contract; stable requester identity and cross-attempt identifiers must not leak to contributors. This covers serving-worker loss. Requester or coordinator crash recovery needs separate persistence and reconciliation; do not silently persist content when history is disabled.

2. **Detect loss and stalled work.** Socket close is the fast path. Add explicit first-token and decode-progress deadlines to direct routes and an initial-heartbeat deadline to the registry. Derive work budgets from request size, model load/prefill phase and observed throughput. Heartbeats alone do not prove progress. A total request deadline and attempt limit bound all loading, dispatch and retry work.

3. **Retry before output.** Extend the existing coordinator behavior to direct routes and other supported callers. One layer owns retries; nested frontend/gateway/coordinator loops must not multiply attempts. Exclude failed machines and use cooldown/backoff for repeated failures. Recheck authorization on every dispatch while preserving the original scope ceiling, circle selection, history policy, model and parameters. Never silently broaden scope or substitute another model.

4. **Restart visibly after output.** Preserve and label the interrupted answer. V1 offers **Restart on another computer** using the original request snapshot. A replacement answer is a distinct attempt, never appended to the partial answer. Automatic visible restart can follow for pure generation under the same policy and budget. API clients receive a typed interruption; an ordinary text stream cannot retract earlier output.

5. **Retire old attempts.** Add capability-negotiated per-attempt cancellation and acknowledgement. Accept tokens/results only from the current attempt; late messages from an old worker cannot overwrite the replacement. Cancellation is best effort, so worker-side execution deadlines also bound abandoned computation. Make settlement idempotent across the logical request as well as within an attempt: failed attempts receive no successful-job credit, and replacement success cannot double-charge. Preserve mixed-version compatibility.

6. **Handle insufficient capacity honestly.** If no compatible worker/group fits, keep the request and partial answer and offer retry, an explicitly chosen alternate model, or cancel. Waiting must be bounded and cancellable. A machine going offline is a liveness event, not evidence that it fabricated an answer.

## Split models

Planned. None of this is implemented.

Losing one stage loses model layers and that stage's attention cache for the current request. Retrying a socket or sending the next token cannot recreate that state.

**V1:** invalidate the failed execution group, reserve enough compatible capacity, load the necessary layers and restart the current inference on a complete group. Preserve conversation history and already completed agent steps. Reuse healthy machines where possible, but do not treat their old per-request caches as a valid resumed group. Supervise native RPC execution in a separate process so a stuck/crashed native call cannot take the request owner down with it. Without enough remaining/replacement memory, the same model cannot proceed.

**V2:** retain exact input and accepted output token IDs plus model/tokenizer/template/configuration identity, sequence position, stop state and sampling state at the requester. Rebuild caches by replaying that exact prefix on a compatible group, then expose only new output. This requires an engine recovery API; the current text-delta stream is insufficient. Re-tokenizing displayed text or inserting it into a new assistant message is not a token-exact checkpoint. Cross-hardware generation may diverge after the preserved prefix. Measure replay latency before adding KV-cache replication or warm redundant stages.

## Agent actions

Planned. None of this is implemented.

Retry the interrupted inference or uncommitted computation, not the entire agent workflow. Store completed steps, artifacts and action receipts at an authorized owner. Stable action IDs and external-service idempotency/reconciliation prevent retries from repeating writes, payments or publishing. When the effect of an action is unknown, reconcile before reissuing it. A request ID alone cannot guarantee exactly-once external execution; see [AWS's idempotent API guidance](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/).

## Status

1. **Implemented:** explicit successful gateway stream termination; direct-route timeouts and bounded pre-output retries; the household replacement-answer UI.
2. **Planned:** request and attempt ownership, cancel negotiation, stale-result rejection and settlement deduplication, followed by automatic mid-output restart. Circle and pool explicit restart depend on this coordinator attempt protocol.
3. **Planned:** split-group reservation, teardown and restart with native process supervision.
4. **Planned:** token-prefix replay, after the restart path handles worker loss reliably.

## Evidence

- [Coordinator failover and terminal outcomes](../../services/coordinator/coordinator/routers/jobs.py): `_dispatch_stream_with_failover`, `dispatch_job_stream`, `_finalize_job_in_db`.
- [Disconnect draining](../../services/coordinator/coordinator/services/peer_registry.py): `drain_peer_jobs`; [heartbeat reaper](../../services/coordinator/coordinator/services/peer_reaper.py).
- [Desktop direct dispatch](../../apps/desktop/frontend/src/lib/householdInference.ts): `streamHouseholdInference`; [partial output](../../apps/desktop/frontend/src/stores/chatStore.ts).
- [Gateway direct stream](../../apps/desktop/python/potluck_local/services/gateway_routing.py): `stream_peer_openai`; [pool stream](../../apps/desktop/python/potluck_local/services/pool_routing.py).
- [Pipeline edge errors](../../apps/desktop/python/potluck_local/services/pipeline_parallel.py), [batch failure handling](../../apps/desktop/python/potluck_local/services/continuous_batch.py), [RPC configuration](../../apps/desktop/python/potluck_local/services/runtime.py).

## Tests

Tests include Python transport fault injection, OpenAI and Ollama endpoint tests, frontend streaming, store and component regressions, and a Playwright test (`apps/desktop/frontend/e2e/recovery.spec.ts`) that drives an interruption and the restart button through the real frontend transport, with all backends intercepted, and checks that two different simulated household computers receive identical request payloads. These tests do not replace physical worker-loss tests for split inference.

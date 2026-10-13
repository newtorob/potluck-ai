# Household batch jobs

First workflow: summarize explicitly selected UTF-8 files without tools. Each
file is an independent unit. The local runtime owns a durable queue, so the CLI
can exit after submission. Scope is explicitly `local` or `mine`; the latter
uses only the existing household mesh. A project model ID and SHA-256 are required
and immutable. The scheduler never loads a model or changes contribution settings.

Inputs are snapshotted privately on the submitting machine, along with outputs
and bounded attempt metadata, in a separate SQLite store. Peer inference remains
stateless. No prompt, path, output, or task record goes to the coordinator.
These local snapshots are private plaintext files, not encrypted storage.
The CLI accepts explicit regular text files only, with no directory recursion or
symlink following. Limits bound file sizes, job count, concurrency and retries.

One batch runs at a time per runtime, with one in-flight piece per eligible
machine and bounded total concurrency. Fresh capability probes must match the
exact model hash. The scheduler owns retries; peer dispatch cannot secretly
select a second machine. A worker that fails is excluded for the rest of that run.
Partial output is discarded and only a terminal successful result is committed.
Workers must advertise `batch_protocol_version=1` and acknowledge it in response
headers. The batch stream preserves the native finish reason; missing finish,
token-limit truncation, errors and premature EOF cannot become completed results.
Existing chat requests retain their legacy compatibility. The system prompt is
snapshotted with the job so resume does not pick up a changed template.
Retried inference is at-least-once; a completed result is committed once.

Completed results are never replayed. Cancel preserves them, stops new admissions
and drains active requests. Resume requires the same project pin and scope.
After runtime restart, unfinished jobs are paused and their interrupted leases
recovered. Explicit resume uses saved inputs, even if source files have changed.
If no eligible machines remain, pause with an actionable reason. At most three
attempts per piece across all resumes; exhaustion remains a failure.

A partially generated answer is never migrated to another machine, and the
workflow runs no general agent jobs. It does not execute file contents,
model-requested tools, or shell commands.
The submitting runtime must stay running and reachable for active scheduling.
Cross-account circles, split-model orchestration, prefix reuse and live desktop
visualizations are out of scope for this workflow.

## Tests

`apps/desktop/python/tests/test_batch_jobs.py` and `apps/cli/test/batch.test.ts`
cover parallel assignment, whole-piece retry after truncated streams, the
immutable pin and scope, no duplicate completed work after a restart, cancel and
resume ordering, private storage, safe input selection, and the real peer
dispatch path.

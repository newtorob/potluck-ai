# Shared context for local Potluck agents

The developer harness turns a reviewed project brief and a list of assignments
into saved jobs for the existing Potluck CLI agent. It is opt-in and does not
change gateway settings, load a model or change ordinary agent behavior.

The primary curator maintains the brain: project direction, current work,
priorities, constraints and references to the task/vault evidence behind them.
The harness freezes that revision for a batch. It does not ingest the whole
vault, fetch MCP memories automatically or let agent output rewrite the brain.
The curator reviews findings and explicitly updates the next brain revision.

## Prepare a brief and assignments

Use private local files for project context. A brain expires within seven days;
use a shorter expiry for fast-moving work. Dates below are examples and must be
replaced with current reviewed dates.

```json
{
  "version": "potluck-agent-brain-v1",
  "project": "sample",
  "revision": "reviewed-1",
  "updatedAt": "2026-11-06T12:00:00Z",
  "expiresAt": "2026-11-07T12:00:00Z",
  "brief": "Build reliable, source-cited answers from reviewed company knowledge.",
  "priorities": ["Preserve policy prerequisites and exceptions."],
  "constraints": ["All findings need primary review; no publication."],
  "provenance": ["Current reviewed project task and vault note."]
}
```

The brain is capped at 5 KiB. Inputs are bounded regular UTF-8 files. Each job
gets its own workspace containing only its selected copies. Source paths are
relative to `root`; `name` is the file name visible to that agent.

```json
{
  "version": "potluck-agent-plan-v1",
  "id": "receipt-review-1",
  "brain": "/absolute/private/brain.json",
  "root": "/absolute/private/source-pool",
  "model": "your-loaded-model-id",
  "contextWindowTokens": 8192,
  "limits": {
    "concurrency": 1,
    "maxSteps": 3,
    "outputTokens": 700,
    "timeoutMs": 180000,
    "totalOutputTokens": 2100
  },
  "jobs": [{
    "id": "receipt-stage",
    "objective": "Identify any omitted stage in the receipt instructions.",
    "successCriteria": ["Cite exact evidence and state uncertainty."],
    "files": [{"path": "receipt-policy.md", "name": "evidence.md"}]
  }]
}
```

Queue up to 64 jobs. Concurrency defaults to one when omitted and is capped at
four. This is a queue capacity, not a measured throughput claim or a guarantee
that one GPU benefits from concurrent inference. Use one hub for every batch
sharing a runtime. Separate hubs do not coordinate with one another or with
other runtime clients. The operator must own the runtime before dispatch.

The total output-token ceiling must cover jobs × steps × per-turn output.
There are no automatic retries or recursive subagents. Per-job deadlines cancel
the client request; runtime worker drain may take longer. A separate owned
runtime watchdog remains appropriate for native evaluations.

## Run and inspect

From `apps/cli`, build once with `npm run build`, then:

```sh
npm run agents -- prepare /absolute/private/plan.json /absolute/private/agent-hub
npm run agents -- status /absolute/private/agent-hub receipt-review-1
```

Set `POTLUCK_AGENT_URL` to an explicitly owned `http://127.0.0.1:PORT/v1`
endpoint (or IPv6 loopback), with a non-8321 port, and supply its key in
`POTLUCK_AGENT_KEY`. The harness does not read or change the normal gateway
configuration. Then:

```sh
npm run agents -- run /absolute/private/agent-hub receipt-review-1
```

Only local scope is used. The configured model and context allowance are
recorded, but this harness does not attest model weights or query loaded
context capacity. The operator/supervisor must verify those before dispatch;
receipts explicitly record `modelIdentityVerifiedByHarness: false`.

The underlying agent can read/search its copied workspace. Edits, shell
commands and verification commands are disabled, and confirmation always
denies mutations. Context and source text cannot grant tool permissions.
This application boundary is not an OS sandbox against other local processes.

The hub stores `batches/<id>/manifest.json`, a manifest digest, copied files,
per-job `state.json` and immutable `receipt.json`. Receipts contain bounded
local traces, tool results, timing and input/configuration identities, never
the runtime key. Treat these files as private project data. CLI status prints
state rather than raw model/source content.

`needs_review` means the agent returned normally. It does not mean the result
is correct, accepted or applied. The primary reviews the cited evidence and
decides what to incorporate. The receipt captures a transcript, not a trusted
schema for automatic downstream actions. Failed/cancelled jobs remain terminal.

## Freshness and recovery

Dispatch checks brain expiry and hash, original sources and copied-file hashes.
Changed inputs are refused. Refresh context explicitly and prepare a new named
batch; existing jobs never silently switch revisions. Status remains available
after expiry. Source drift during a job makes its result failed.

Re-running a batch processes only queued jobs. A killed process leaves running
jobs unresolved. Once its recorded PID has exited on the same host, run:

```sh
npm run agents -- recover /absolute/private/agent-hub
```

Recovery marks unresolved running jobs `interrupted`, without retrying them,
and preserves pending work and any receipt. Resume the batch to run queued jobs.
To retry failed/interrupted work, review the evidence and prepare a new batch ID.

One kernel-owned loopback coordination socket serializes runs and recovery in
each hub; it serves no content and releases on process exit. Its deterministic
high port can collide with another local service, in which case dispatch
refuses rather than running without coordination. Complete lock and receipt
files are published atomically; files and POSIX directory entries are synced.
Windows directory syncing is best effort and Windows execution is untested.
An invalid owner record or a still-present PID requires inspection, not forced
unlocking. Context hashes detect accidental drift, not a malicious local owner
rewriting both a manifest and its hash.

This first version supports reviews, test design and evidence-backed planning.
Code-writing workers, automatic task retrieval, multi-host dispatch and reviewed
memory promotion are later integrations. They are not enabled by this harness.

## Supplied-evidence roles (plan v2)

Use `potluck-agent-plan-v2` for a small assignment whose evidence can fit in one
request. Every job must add `role` and `output`; set `limits.maxSteps` to one.
Available versioned roles are `context-handoff-v1`, `evidence-review-v1` and
`test-design-v1`. Example job fields, in addition to the v1 fields:

```json
{
  "role": "evidence-review-v1",
  "output": {
    "version": "potluck-agent-result-v1",
    "maxWords": 150,
    "maxFindings": 3
  }
}
```

V2 supplies the curated brain as `@brain` and every assigned file's complete
contents in the pinned user message. Preparation rejects oversized context
before inference. These jobs have no tools: even read/search actions are stopped
before execution. They still use the actual Potluck CLI agent loop and its
stream-completion checks. V1 retains its original read/search behavior.

The one response must be a complete JSON object with exactly `status`
(`complete` or `insufficient`), `summary`, `findings` and `limitations`. Each
finding has exactly `detail` and `evidence`; each evidence entry has `file` and
`quote`. Quotes must be nonempty contiguous substrings of that exact supplied
source. Complete handoff/test-design jobs need a finding; a complete evidence
review may have none. Insufficient results must state a limitation.

The declared maximum covers all prose, source labels and quoted words, counted
by whitespace; maxWords is 32–350 and maxFindings is 1–6. Maximum four
limitations and three quotations per finding. Duplicate keys, extra keys,
markdown fences, prefacing prose, unbound quotes and over-limit outputs fail.
There is no extraction, repair, additional model turn or automatic retry.

A successful format/quotation check records `passed_format_and_quotes_only`
and leaves the job `needs_review`. It does not verify that a quotation entails
a claim, an omission list is complete, or a clean verdict is correct. The
primary must still review scope and factual accuracy. Original traces and
failed output remain in the receipt. Changing a source or brief requires a new
batch, just as in v1.

The first native role trial is unqualified: all five outputs contained an empty
think wrapper before JSON and were rejected without repair. Use these roles as
experimental contracts until explicit native structured-output support is
measured. The original failed responses and primary findings are preserved in
[the role trial report](../bench/results/20261106-potluck-agent-role-contracts/REPORT.md).

## Explicit native JSON schema (plan v3)

### Exact-unit omission review

The opt-in `omission-review-v1` role uses output version
`potluck-omission-result-v1` with the same `maxWords` and `maxFindings` bounds.
It accepts compact JSONL evidence packets whose answer record names selected
units and exact retained bindings. This version supports `source_rule` answers
only; other verdicts are rejected during preparation. Source headings, grouping
and the curated project context remain visible as untrusted reference data.

The raw result contains `status`, `omissions` and `limitations`. Each omission
requires `file`, `unit_id`, `quote` and `reason`. The quoted text must occur in
that exact unit, and its state must be `supplied_only`. Selected units, retained
bindings, unknown references, duplicate references and contradictory packet
states fail validation. The role does not assume every supplied-only unit is
relevant to the question.

Valid results are normalized into the usual review receipt with a deterministic
summary and unit IDs preserved in the evidence. The receipt records
`passed_unit_binding_checks_only`; it still needs primary review. These checks
do not prove relevance, completeness, or the truth of a reason or limitation.
Rejecting an incorrect omission is a failed review, not a clean result. No
failed claims are silently removed or retried. Existing roles are unchanged.

### Per-candidate omission assessment

Use `omission-review-v2` with `potluck-omission-result-v2` to require a decision
for every supplied-only unit. The raw output contains `status`, `assessments`
and `limitations`; each assessment names `file`, `unit_id`, `decision`
(`omission` or `not_needed`), an exact unit `quote`, and a `reason`.
The complete candidate set must appear exactly once. Missing, duplicate or
retained-unit assessments fail. Preparation rejects more than 16 candidates
rather than truncating the source or silently dropping review work. It also
rejects word budgets below the minimum needed for the required assessment
fields; this lower-bound check does not guarantee a useful answer will fit.

`maxFindings` limits omission findings; `maxWords` covers all assessment text,
including dismissed candidates. The normalized receipt preserves every
assessment and derives findings from `omission` decisions only. Its
`passed_candidate_coverage_checks_only` marker verifies that the requested
units were assessed and bound to source text. It does not verify the decisions
or reasons. A complete but incorrect set of dismissals can still fail primary
review. V1 and the other role contracts remain unchanged.

### Evidence-bound dismissal review

Use `omission-review-v3` with `potluck-omission-result-v3` to make dismissal
reasons inspectable. Every assessment includes `covered_by` and one decision:
`omission`, `already_covered`, `not_applicable`, or `prose`. An
`already_covered` assessment needs 1–3 unique unit IDs from the same packet,
all selected or retained by an exact binding. Other decisions require an empty
reference list. A supplied-only candidate cannot serve as retained evidence.

The role asks the worker to consider governing conditions and exceptions for
selected policy groups, including when the question compares permitted uses.
A narrow permission does not by itself cover a broader governing rule. Reasons
are limited to six words, with short exact candidate quotes encouraged. The
existing total word and generation budgets remain enforced; references count
toward the word limit. No output is truncated or repaired into a pass.

The receipt records `passed_coverage_reference_checks_only`. This verifies
reference membership, not entailment: a valid retained ID can still be the
wrong justification. Primary review must assess every omission and dismissal,
including whether the cited retained content actually covers the candidate.
Older role versions and their prior results remain unchanged.

### Question-scoped applicability

The opt-in `omission-review-v4` / `potluck-omission-result-v4` pair keeps the
v3 output fields and validation. It asks the worker to distinguish governing
rules for the question's named subjects from illustrative examples or different
situations that do not change the answer. Implicit eligibility, timing, budget
and exception conditions still matter. This prompt guidance is not a mechanical
relevance check; primary review must still reject irrelevant omission flags.
Older contracts and receipts remain replayable.

### Governing rules in comparisons

`omission-review-v5` / `potluck-omission-result-v5` retains the same typed
assessment checks. Its prompt clarifies that comparisons need the governing
base policies on both sides, rather than only explicit differences. Separate
conditional procedures require an applicable trigger; source proximity alone
does not make them missing requirements. These are review instructions, not
proof of applicability. Earlier contracts remain unchanged.

### Source-only relevance and deterministic coverage

The opt-in `scope-review-v1` / `potluck-scope-result-v1` pair separates source
relevance from draft coverage. It accepts one validated `source_rule` packet,
at most sixteen total units and 1,024 UTF-8 bytes per unit; larger packets fail
before inference rather than being clipped. Model input contains only the
question, source groups, headings and unit IDs/text. Selected/retained states,
filenames, assignments, success criteria and project brain are not sent in this
role. Identical question/source data therefore produces identical messages and
schema regardless of what a draft retained.

The model classifies every unit exactly once as `governing`,
`different_situation`, `illustration`, `prose` or `unresolved`, with a reason of
at most four words. Unresolved classifications require an insufficient result
and a limitation. The generated JSON text values, including status, unit IDs,
classifications, reasons and limitations, consume the declared word budget.

Local code derives potential omissions as governing units marked supplied-only
in the validated packet. It copies their complete trimmed source text into
quoted evidence and preserves all classifications as `scopeAssessments`.
Copied evidence and fixed local summary text do not consume the generated-word
budget; this is an explicit new contract, not a relaxation of earlier versions.
The input byte bounds limit copied evidence, and exceeding the declared finding
limit rejects the output instead of dropping findings. This version checks exact
unit retention, not semantic coverage by a different retained unit; duplicated or
paraphrased rules can therefore need primary review.

`passed_scope_and_coverage_checks_only` confirms exhaustive classification
shape and deterministic unit coverage, never correctness of relevance. Every
classification and resulting omission still needs primary semantic review.
Older role versions and ordinary gateway defaults remain unchanged.

### Implicit mandatory qualifications

`scope-review-v2` / `potluck-scope-result-v2` retains the source-only packet,
strict schema and deterministic exact-unit coverage of v1. Its separate prompt
makes explicit that qualifications governing who can use a named policy, how
much, when and under what mandatory conditions remain relevant even if the
question does not ask about each qualification. A separate optional procedure
requires a trigger in the question or a mandatory relationship to the named
policy. These instructions still require semantic review; they are not a proof
that the worker classified the source correctly. V1 remains replayable.

### General policies remain normative

`scope-review-v3` / `potluck-scope-result-v3` adds a separate prompt clarification:
a governing base policy and its applicable narrower exception both remain
rules. Non-normative prose must not become a catch-all label for a general
policy. V2 mandatory-qualification guidance, source-only exposure and strict
schema/local normalization remain in place. Earlier contracts replay unchanged;
correctness still depends on primary review of native classifications.

### Protocol

Use `potluck-agent-plan-v3` with the same role jobs and one-step limit to request
the `llama-json-schema-v1` protocol. This adds the native
`response_format: {type: "json_object", schema: ...}` request to the actual
CLI stream. V1/v2 and ordinary CLI requests omit it completely.

This is an explicit native dialect, not automatic endpoint capability discovery.
The operator must verify that the owned endpoint supports it before dispatch.
The harness records `protocolCapabilityVerifiedByHarness: false`; the owned
trial supervisor separately performs a structured readiness probe. A server
refusal fails the job without a prompt-only fallback or automatic retry. A
server can ignore a requested format, so returned text is still strictly
validated even when the request declares a schema.

The schema bounds keys, types, list sizes and source-name choices. Source quote
membership, word limits, complete streams, freshness and primary semantic review
remain required. Empty think wrappers, fences and other extra text still fail;
the harness does not strip them. A valid schema cannot prove that an answer is
correct or complete.

The owned native evaluation uses a bounded experiment adapter around Potluck's
existing guarded `runtime.chat` completion path. Direct chat streaming is a
different native path and is not established by that evaluation. The adapter
retains the full native reply and only emits unchanged content as CLI SSE after
the native reply reports `stop`; it never strips thinking text or promotes a
length-limited reply. Its sampler settings are supplied by the explicit trial
driver. It is not an automatically started or shipped general runtime service.
Client deadlines request cancellation; native drain can take longer, and the
owned outer watchdog remains responsible for final cleanup.


## November 6 measured plan-v3 result

The same five consumed role cases returned 5/5 strict-format results and 3/5 primary semantic passes with explicit schema plus the supervised guarded-completion adapter. Complete travel, missing travel exception and unknown appeal deadline pass. Product-direction handoff and receipt test-design coverage fail. No native streaming, fresh accuracy or wider concurrency claim follows. Generic behavior and ordinary gateway defaults remain unchanged. See `bench/results/20261106-potluck-agent-native-schema/REPORT.md` for frozen inputs, native traces, remaining limits and cleanup evidence.


## Measured assignment scope refinement

A later consumed five-case repeat changed only the handoff/test-design assignments. Four of five pass primary review: receipt test-design now covers the original requirements as Company Brain answer/evidence-retention assertions, and three evidence controls remain correct. The handoff restores product direction but omits the required primary-review reminder, so it still fails. Plan-v3/runtime code and old results are unchanged. See `bench/results/20261106-potluck-agent-assignment-scope/REPORT.md`. Next apply bounded reviewed assistance to procedural-stage retention and real selected-versus-supplied review packets; this does not qualify an autonomous worker fleet.


## Typed Company Brain review bridge

A bounded experiment can compile an independently frozen source envelope with
Company Brain's existing coverage-v3 request/schema, invoke the actual Potluck
CLI agent library, and decode its unchanged response through that same Python
contract. A local source binding then compares decoded selected evidence with
the reviewed draft's retained unit IDs. The generic relevance roles remain
available for replay; this experiment does not select a new default.

Direct supplied-context callers can set `toolsDisabled: true` in `runTask`.
This requires read-only mode and no verification command, and stops every
parsed action before callbacks or tool execution. Omitting the option preserves
ordinary agent behavior. Structured output and a prompt saying no tools do not
substitute for this boundary.

The experiment retains the original domain assessment, exact request and raw
response as separate evidence. A finished CLI turn still awaits Python domain
validation and primary semantic review. It does not demonstrate resumable-queue
integration, fresh accuracy, native chat streaming or broader agent autonomy.


The typed bridge measured2/2 on the original consumed complete/missing controls, with no-tools, source-binding and exact domain replay checks. The separate unchanged Company Brain workflow then passed5/5 first-use synthetic supplied-evidence criteria. These do not qualify resumable bridge integration or make the shared assessor an independent semantic reviewer. See `bench/results/20261106-potluck-typed-coverage-review/REPORT.md` and `bench/results/20261106-investigation-fresh-qualification/REPORT.md`.

## Resumable typed coverage jobs

Opt-in `potluck-agent-plan-v4` jobs use `domain: "company-brain-coverage-v1"`
and exactly two copied files named `source-envelope.json` and `draft.jsonl`.
Keep the usual objective, success criteria and curated brain metadata. The domain
contract alone forms model messages; brain/assignment text and draft-retained
states do not alter the coverage request. V1–V3 behavior remains unchanged.

Set both `POTLUCK_AGENT_DOMAIN_PYTHON` (absolute Python executable) and
`POTLUCK_AGENT_DOMAIN_ROOT` (absolute directory containing `potluck_local`) for
`prepare` and `run`. The CLI invokes only the fixed Python bridge module, without
a shell, network access or model-selected commands. It strictly parses original
envelope JSON, bounds subprocess input/output/time and fingerprints the actual
Python implementation. It does not use cached bytecode. Preparation freezes that
identity, the complete request/schema and validated source binding in the manifest.
Any drift blocks dispatch or leaves existing output pending; no silent rebinding.

V4 requires concurrency1, one step,700output tokens and at most180seconds per
model call. The supplied sources retain the16unit/1024UTF8byte-per-unit limits,
140generated-word and3derived-finding caps. Model actions are disabled by the
runner. Native schema capability/model identity and owned runtime lifecycle still
need the external supervised runtime; the CLI never starts or changes a gateway.

A completed model turn first publishes immutable `raw-receipt.json` and enters
`needs_domain_validation`. Deterministic Python decoding then creates a separate
final `receipt.json`: either `needs_review` with
`passed_binding_and_coverage_checks_only`, or `failed` for a rejected result.
Unavailable validation remains pending. Running the same batch resumes that local
validation without another model call. Completed/failed jobs are never inferred
again. Recovery after owner-process exit can adopt published raw output; an
interrupted model call without raw evidence stays interrupted. A final receipt
published before a state-write crash is replay-validated, without inference.

Every final result still requires primary semantic review. Shared source selection
is not an independent correctness judgment, and a binding check is not acceptance.
All evidence stays in the private local hub. Do not infer greater concurrency,
streaming support or deployment readiness from this opt-in integration.

### Test design with bound source references

`test-design-v2` is an explicit opt-in role with output version
`potluck-test-design-result-v2`; existing roles and defaults are unchanged. It
presents deterministic IDs for whole paragraphs from the supplied sources,
split only at blank lines. IDs bind the source name, complete source content,
and paragraph offsets. The worker returns proposal details and paragraph IDs;
the harness verifies those IDs and copies the exact source text into the
receipt. It rejects foreign IDs and duplicate IDs within one finding, source
drift, and over-limit results without changing or trimming evidence. Separate
findings may cite the same paragraph; every copied instance counts toward the
serialized-result limit.

`maxWords` bounds generated summary, proposal details and limitations; exact
server-copied evidence is separately bounded by a 32,768-byte full serialized
result limit. The input catalog is limited to 128 paragraphs. A complete result
requires a proposal, and an insufficient result requires a limitation. A
format-valid result remains unverified, requires primary factual review, and
does not mean any test was run. Existing batch source hashes and freshness
checks apply before and after execution; resume does not retry a finished job.

`test-design-v3`, output version `potluck-test-design-result-v3`, uses the same
reference schema and limits. It additionally instructs the worker to spell out
requested statuses, conditions, limits and observable boundaries in each test
proposal, including relevant prerequisites and exceptions. It requests a concrete
setup, action and observable outcome, preserves the named object or record, and
asks for behavioral evidence rather than project context. The instruction is
not a completeness guarantee; primary review still assesses the proposal itself.

### Atomic test matrix

`test-matrix-v1`, output version `potluck-test-matrix-result-v1`, is an opt-in
role for one to three explicitly authored `successCriteria`. The harness assigns
those criteria IDs C1 through C3; their count may not exceed `maxFindings`. Each
returned ID has setup, action, expected outcome, and source paragraph IDs. For a
complete result all fields and at least one source reference are required. An
insufficient result retains every ID, marks unresolved fields explicitly and may
leave their evidence empty, but requires a limitation. Limitations are capped at
two and should describe actual evidentiary gaps rather than boilerplate. The role uses exact supplied non-brain source
paragraphs as evidence; project context cannot serve as behavioral evidence.

Generated setup, action, expected and limitation text shares `maxWords`; raw and
materialized output retain separate 32,768-byte limits. The receipt includes
`matrixChecks` and deterministic per-check findings for existing displays.
Format and reference validity do not establish that the test implements its
criterion, that the cited source supports it, or that any test ran. Primary
factual review is required. Existing role contracts and defaults are unchanged.

`test-matrix-v2` uses output version `potluck-test-matrix-result-v2`. Each
check also returns its exact supplied criterion before its setup, action,
expected outcome and source references. A singleton string enum expresses the
exact criterion in the runtime-supported schema; validation independently
requires byte-for-byte equality. The stored `criterion` records the requested
check, not generated test behavior or proof of coverage. The generated-word
budget excludes this echoed criterion but includes setup, action, expected and
limitations; raw and materialized byte limits still include the echo. The echo
also consumes model output tokens. Primary review must assess the actual test
behavior, which may still omit or contradict the requested state or action.
Existing `test-matrix-v1` and other roles remain unchanged.

`test-matrix-v3`, output version `potluck-test-matrix-result-v3`, additionally
requires `testRequirements` on each job: an ordered record for each C1–C3
criterion with `checkId`, `initialState`, nonempty `actions`, and nonempty
`expected` arrays. IDs must match criterion order; entries and arrays are
bounded and unknown fields are rejected. These records are explicit authored
assignment data, not source policy, generated test behavior or proof of
entailment. The worker receives them alongside curated context and independently
bound non-brain source paragraphs. Unsupported requested behavior must be
reported as insufficient, including a conflict between a criterion and its
authored requirements; no unspecified API or UI is assumed. Echoed criteria,
generated fields, source references, output budgets and primary review retain
v2 semantics. This opt-in input contract does not alter prior roles or defaults.

### Opt-in test matrix V4

`test-matrix-v4` uses `potluck-test-matrix-result-v4` with the V3 typed requirements and evidence binding. Authored scenarios provide abstract test operations and fixture preconditions; cited source policy supplies behavioral invariants. A complete result is a proposal requiring review, not evidence that a test executed or that its expectation is entailed. Source limits apply to the supplied operations unless explicitly scoped otherwise; satisfying a necessary condition alone does not establish success. Returned setup, action and expected fields must retain supplied preconditions and intermediate checks as well as outcomes; they may reuse source-supported requirement wording. Input requirements and the criterion echo alone are not test coverage. The overall generated-word limit still applies. V1–V3 remain unchanged.

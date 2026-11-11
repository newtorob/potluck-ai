Current reviewed-pilot milestone: [public evidence summary](../../../bench/results/company-brain-milestones-20261111/README.md) and [pilot workflow](../../../docs/company-brain-pilot.md). The dated implementation/evaluation lineage below remains historical; current V32 display compaction changes no model selection or stored evidence.

# Local Company Brain evidence and review

This local-owner developer feature connects scoped Markdown source collections,
reviewed knowledge and cited lookup to a saved investigation in the desktop app.
The investigation explicitly uses an already loaded local model. Source/review
commands do not invoke a model. No tool execution, training, remote inference or
automatic knowledge approval is part of this workflow. Shared company deployment
still requires authenticated server-side authorization and operational work.

For the opt-in desktop folder → reviewed knowledge → answer → handoff flow, see [Company Brain local pilot](../../../docs/company-brain-pilot.md).

## Sources and authority

Register and scan selected folders with `potluck_local.services.source_collections`
as described in the engine README. Use a disposable database for experiments:

```sh
export POTLUCK_MEMORY_DB_PATH=/absolute/path/to/experiment/memory.db
python -m potluck_local.services.source_collections --workspace example \
  register engineering /absolute/path/to/approved-docs
python -m potluck_local.services.source_collections --workspace example scan COLLECTION_ID
```

Raw `source_chunks` are evidence, not approved instructions. `reviewed_claims`
records immutable versions of a claim under a workspace-specific `claim_key`:

| State | Meaning |
| --- | --- |
| proposed | Source-backed text awaiting an explicit owner review. |
| approved | A reviewer and reason were recorded; eligible for current lookup only while all sources remain allowed, ready, and recent. |
| superseded | Replaced atomically by a proposal with the same key; retained as history with a replacement link and review metadata. |
| deleted | Content, key, and free-text review metadata are scrubbed; identifiers, timestamps, source bindings, and a fixed deletion cause remain. |

At most one version per workspace/key can be approved. Approving a second proposal
requires explicit supersession. Removing the replacement never reactivates its
predecessor. Human review records responsibility; it does not automatically prove
that a source supports the claim.

Claims bind between 1 and 16 distinct source references, each containing
`collection_id`, `document_id`, `revision_id`, and `chunk_id`. Every reference must
match the current indexed source in the caller's explicit allowlist. A claim
requiring multiple collections is unavailable if any one is outside the scope.

The local review CLI is an owner action; it is deliberately not an MCP write tool:

```sh
python -m potluck_local.services.reviewed_knowledge --workspace example \
  --collection COLLECTION_ID propose deployment-status 'Attribution is deployed.' \
  --citation '{"collection_id":"COLLECTION_ID","document_id":"DOCUMENT_ID","revision_id":"REVISION_ID","chunk_id":"CHUNK_ID"}'

python -m potluck_local.services.reviewed_knowledge --workspace example \
  --collection COLLECTION_ID approve CLAIM_ID --reviewer owner --reason 'Checked the cited source'

python -m potluck_local.services.reviewed_knowledge --workspace example \
  --collection COLLECTION_ID supersede OLD_CLAIM_ID NEW_PROPOSAL_ID \
  --reviewer owner --reason 'Reviewed the correction'

python -m potluck_local.services.reviewed_knowledge --workspace example \
  --collection COLLECTION_ID list --status superseded

python -m potluck_local.services.reviewed_knowledge --workspace example \
  --collection COLLECTION_ID delete CLAIM_ID
```

Repeat `--collection` for multi-source claims. IDs come from evidence results; the
uppercase values above are placeholders. Proposed content is limited to 16,000
characters, keys/reviewer names to 256, and review reasons to 4,000. The existing
credential redactor applies to these persisted text fields. It is not general PII
filtering. `list` is a review-management view of stored states, not a freshness
guarantee; use evidence lookup before presenting a claim as current.

## Evidence lookup and citations

The local Python API is:

```python
from potluck_local.services.evidence_lookup import (
    EvidenceScope, lookup_evidence, resolve_citation,
)

scope = EvidenceScope(workspace="example", collection_ids=("COLLECTION_ID",))
result = await lookup_evidence(scope, "Was download attribution deployed?")
# Each result has a citation_id. Resolve it again before using a saved citation.
record = await resolve_citation(scope, "source:CHUNK_ID:REVISION_ID")
```

Results separate `source_chunks` from `reviewed_claims` and include scope, source
identifiers, relative paths, anchors, revision IDs, indexed timestamps, review
metadata, uncertainty, and missing-evidence/partial-result notices. A reviewed
claim carries its source records. Unresolvable, superseded, removed, out-of-scope,
or stale citations return `None`; resolution never falls back to a different
revision or a personal memory. Citation IDs are references, not access tokens.

Search ranks Unicode case-insensitive whole-word overlap, ignoring common question
words. It does not generate an answer or verify semantic support. Unsupported
queries produce empty result lists with missing-evidence information. Related
words alone cannot establish that a question is answered, and conflicts in raw
source documents still require review.

Limits: 100 collections per scope, 1,000 query characters, 20 distinct meaningful
terms, 1–100 returned records, and at most 200 candidates per evidence kind before
ranking. Candidate truncation is explicit and can omit a more relevant result.
SQL still scans matching source text; this is not a large-corpus search benchmark
or an enterprise-scale performance claim. Citation resolution uses direct IDs.

## Opt-in MCP access

The owner sets a fixed scope when starting the MCP process:

```sh
POTLUCK_EVIDENCE_SCOPE='{"workspace":"example","collection_ids":["COLLECTION_ID"]}' \
  python -m potluck_local.mcp --stdio
```

This adds `evidence_search(query, max_results=10)` and
`evidence_resolve(citation_id)`. There is no workspace or collection argument in
either tool. Scope is captured at server construction, so changing the environment
requires restarting that process. Missing configuration leaves these tools absent;
malformed configuration prevents startup. Both memory privacy switches must be
enabled, and unavailable settings fail closed. Evidence arguments and response text
are excluded from MCP observability records; errors omit source text and paths.

This uses the existing local MCP trust boundary: a private stdio child process, or
the existing bearer-token gate for optional loopback TCP. The configured owner
grants the agent read access to the entire selected scope. It does not prevent that
agent from forwarding retrieved text to its configured model provider. Choose the
agent/model accordingly for private sources. Workspace labels and reviewer names
are not authenticated organizational identities. Other existing personal-memory
tools retain their separate behavior; this allowlist only governs the new evidence
tools. Multi-user deployment still needs authenticated per-user access enforcement.

## Freshness and removal

Evidence lookup requires a successful scan with a timestamp no more than 24 hours
old. Failed, pending, never-scanned, unknown-age, future-dated, and older snapshots
are excluded. This is freshness of the indexed snapshot: changes on disk become
visible only after an explicit scan. There is no watcher or scheduled scan.

- A changed or removed document invalidates and scrubs all claims bound to it,
  including proposed and superseded payloads, in the same scan transaction.
- Changed selection rules immediately scrub affected claims and derived sources.
- Removing a collection scrubs its claim payloads and removes its source records.
  Original files are untouched. Tombstones whose collection was removed are no
  longer listable because their original authorization bindings cannot resolve.
- A failed scan rolls back all changes, retains the previous snapshot, and blocks
  current retrieval until a successful rescan. Manual deletion remains possible
  while a scan is failed. Tombstones are not automatically revived.

Scrubbing removes content from active logical rows. It is not a promise of forensic
erasure from SQLite WAL files, filesystem backups, or previously returned agent
responses. No evidence embedding or retrieval cache is created by this feature.
Personal/global memories and the legacy vault importer remain separate; the
personal-memory wipe action does not remove registered source collections.

## Verification and remaining work

The source, review, lookup, MCP, and cross-service invariant tests use disposable
databases and synthetic documents. They exercise scope isolation, atomic
supersession, scan rollback, deletion, freshness, citation invalidation, privacy,
and concurrent approvals. No real vault is implicitly ingested by these tests.

Model usefulness is a separate gate from these software tests. Frozen synthetic
cases and the comparison contract are in [evals/company_brain](evals/company_brain/README.md).
The comparison consumes explicit independent labels; it does not infer semantic
support from valid citation IDs. Failed or missing execution status and absent
assessment provenance cannot pass, even with favorable labels. Assessor metadata
distinguishes agent from human review; it is an assertion of independence, not an
authenticated identity or evidence of customer acceptance. Missing review timing
remains unavailable, not zero. No quality improvement is claimed.

The separate `potluck_local.services.company_brain_eval_runner` command runs the
frozen synthetic cases through the production investigation function using an
explicitly named, hash-checked local model on CPU. It uses fresh isolated stores,
retains every attempt, exact retrieved evidence, model request, reply or error,
and measured timing, and leaves assessment labels unset. The paired conditions
are raw scoped evidence and reviewed evidence where supported by the fixture;
other cases are controls. Stale-source rejection is reported separately from
generated-answer quality. See the evaluation README for the command and limits.

The separate grounding diagnostic accepts an explicit frozen fixture directory and
an investigation template version. Its default is `investigation-v4`, which is
also the production default. `investigation-v5` and `investigation-v7` are
diagnostic-only and cannot be selected through the investigation API or UI.
The opt-in answer preview continues to use `investigation-v6`. The v7 candidate
uses exact issue spans for question planning and explicit guidance for derived
answers. It was not promoted after the answer-completeness comparison: arithmetic
omissions remained, and harmless punctuation changes could invalidate a plan.
Exact-span planning cannot guarantee coverage of every requested part, and
calculations remain model-generated and require independent checking. See
`bench/results/20261029-answer-completeness-v2/` for the frozen runs and assessments.
Running a diagnostic does not change
or download a model. The v3 comparison fixtures at
`apps/desktop/python/evals/company_brain/model_comparison_v3/` and results at
`bench/results/20261029-model-prompt-comparison-v3/` have been consumed; they are
not a fresh holdout. The v1 and v2 raw artifacts remain unchanged.

The question-answer preview represents each planned question with cited support,
an unknown or conflicting evidence. Further improvements require new frozen
evaluation and actual owner review. Neither the diagnostic nor agent assessment
establishes owner acceptance.

## Saved investigations in the desktop app

Open **Workspace → Investigations**. Enter the workspace used to register the
selected sources, load collections, and choose the exact collection scope. Source
registration/scanning remains a local CLI operation. Load a model in **Models**,
reload collections to refresh model status, and explicitly select the loaded
model. Enter an issue and a short evidence query, then create an investigation.

New briefs separate facts, prior fixes, unverified hypotheses, recommended checks,
and model questions. Deterministic retrieval notices are stored separately from
model questions and cannot be edited in a correction. Historical drafts retain
their original combined missing-evidence list, labeled with unknown provenance.

Every fact and prior fix must cite retrieved records and include a short exact
source excerpt for each citation. Excerpts must appear in the source chunk actually
shown to the model, within the retained context boundary. A reviewed-claim citation
must quote one of its underlying source chunks, not just repeat the approved claim.
Corrections use the same checks. The app and Markdown handoffs show the excerpts
beside claims; citations open source text and revision metadata. An exact match
makes support inspectable: it does not prove entailment, completeness, or resistance
to injected instructions. The owner must still inspect the claim against its source.
Recommendations remain text only; no commands, tools or fixes are executed.

Saved drafts retain the original answer, selected source scope, all retrieved
evidence bindings, model slug and SHA-256, prompt/retrieval versions, generation
settings and elapsed time. **Load saved** uses the original exact collection set.
Current limits are 100 saved records per scope and 10 lookup results total,
with at most 6,000 characters per evidence record and 24,000 characters total
model context. Truncation is disclosed. These character bounds are not a tokenizer
budget; a model can reject context it cannot fit.

Generation uses the selected local runtime directly, temperature 0 and at most
1,600 output tokens. Request-local citation aliases reduce output overhead and
are checked and mapped to full source IDs before persistence. Only a valid JSON
brief is accepted. New generation explicitly requests a restricted JSON schema;
unsupported runtime backends, schema keywords, or schema compilation fail closed.
The verified stock llama-cpp formatter receives an explicitly compiled grammar,
preventing its generic-JSON fallback. The managed native adapter forwards the schema
through normal and protected-token requests. Other handlers are unavailable for this
workflow. Structural validation and source checks still run after generation: a
syntactically constrained response can contain unsupported statements. Generation has a
240-second soft timeout: the existing runtime drains a non-cancellable worker
before returning. Invalid output or timeout saves no partial investigation and
does not trigger an automatic retry, model download or broader routing.

An experimental **Answer each question** preview uses `answer_each_question: true` on creation.
The optional `questions` list supplies one to eight explicit questions; otherwise a
separate bounded local model call plans questions from the original issue alone.
The original issue, question list, origin and model provenance are retained. The
planner can omit requested parts, so its output is not a completeness guarantee.
Each question receives one answer labeled supported, unknown or conflicting.
Unknown applies to the presented evidence; conflicting answers should preserve
competing values. The server assigns snippet IDs and copies excerpts from the
underlying source text actually shown to the model. Corrections retain the original
questions and can select only those snippets. Source and scope checks still apply.
These structural checks do not prove that an answer is entailed, that conflicting
values are represented fairly, or that the plan covered the issue. Inspect the
sources and answers. This preview does not replace the default brief workflow.
Planning and answering share a 240-second soft timeout; an inference worker may
need to drain after timeout, especially during prompt evaluation. An optional
explicit generation profile can be supplied for supported models;
unsupported model or backend combinations fail rather than silently falling back.
The preview's **Answer profile** control offers Standard or Qwen3 non-thinking.
The latter sends `generation_profile: "qwen3-non-thinking"`, verifies the active
stock GGUF formatter, and explicitly renders its non-thinking mode. It uses
temperature 0.7, top-p 0.8, top-k 20 and min-p 0; this does not change other chat
requests. Template verification is deliberately strict, so a different template
or backend can be rejected even when the model name contains Qwen3.

The explicit experimental profile `qwen3.8-27b-native-non-thinking` is limited to
its pinned model SHA-256, native engine commit, loaded template and renderer probes.
The renderer and a small structured-output diagnostic have been inspected with
the installed model. This profile renders and tokenizes through the native prompt
guard before constrained completion; a missing guard fails the request. The
diagnostic does not qualify all schemas or task quality. This profile does not
change the default or establish a model quality score.

The internal `investigation-v14` experiment represents factual answers as short
claims with citations per claim and model-reported unmet aspects. Claims and
coverage remain unverified. It is a service and export experiment, not a public
API or desktop preview option. If a claim reaches its output limit or the model
reports an unmet aspect, the answer is marked incomplete and an integrity warning
is retained in the draft and corrected export. This is distinct from the model's
assessment of whether the presented evidence supports a claim. The default
workflow and earlier investigation versions are unchanged.

The internal `investigation-v15` experiment plans answer aspects against numbered
presented evidence units, then generates a compact answer per aspect. Its bounded
paragraph-v3 units preserve source text and prefer sentence boundaries in prose;
long sentences can still split and source context can still be omitted. The full
issue is retained separately from its evidence query. If a supplied query exceeds
retrieval limits, the experiment records the selected and omitted distinct terms
and reports the bounded query. Unit selection and exact stored excerpts provide
provenance, not proof of relevance, coverage or entailment. Capacity warnings and
unverified model assessments remain visible. Invalid evidence plans fail without
saving a partial draft. A supplied operation plan uses one evidence-planning call
and one answer call; automatic operation planning may require additional calls.
This is not a public API or UI option.

The v15 experiment allocates at most six factual slots across the request and
limits their combined answer text and separators to 2,400 characters, with at most
1,000 characters per slot. More than six factual operations fail explicitly.
The allocation and actual per-slot limits are recorded. Reaching an allocation is
an informational signal; a model-reported need for more slots or a text-limit hit
marks the draft incomplete. Compact JSON is requested but not guaranteed. The
existing output-token limit and shared deadline remain in effect.

The retained real-document comparison saved 3/4 drafts with v13 and 2/4 with v14;
neither met all frozen fact requirements in any case. v14 is not promoted.
See `bench/results/20261030-claim-quality-v14` for failures, independent reviews
and checks; a valid claim citation is not proof of complete coverage.

## Review, outcomes and reuse

1. Explicitly accept, correct or reject a draft, with a reviewer and reason.
   A correction preserves the original draft and can change text, citations and
   excerpts. Facts and prior fixes need exact excerpts even when correcting a legacy
   draft. Historical combined missing evidence remains in the original draft; the
   reviewer supplies new unverified questions separately. Retrieval notices remain
   immutable. The review is one-shot; it is not a collaborative editor.
2. Record the actual outcome separately as resolved, unresolved or inconclusive,
   with notes. Accepting a brief does not resolve the underlying issue. An outcome
   is also one-shot; create a new investigation for a later observation.
3. Export a Markdown handoff, or after review and outcome explicitly download an
   evaluation-case proposal. The proposal contains references and review/outcome
   categories rather than source text or an automatically accepted test answer.
4. An accepted/corrected investigation with an outcome can propose knowledge using
   citations from the reviewed brief. The claim remains **proposed**; approval
   still uses the separate owner review CLI. This does not train a model.

The HTTP endpoints are under `/company-brain` and use the sidecar's existing
loopback/token middleware. They are unavailable through peer/remote routes.
Workspace strings and reviewer names are organizational labels, not authenticated
company identities. Both memory privacy switches are required. Exports already
downloaded cannot be revoked or scrubbed by later source changes.

New, changed or removed source material in any selected collection invalidates
the investigation, including earlier empty-result claims. Unchanged rescans keep
it available. Invalidated payloads are scrubbed, and knowledge derived from those
investigations is scrubbed recursively through dependent investigations. Review,
outcome and knowledge writes revalidate the evidence inside their transaction.
Old versions never reactivate automatically. A failed or stale scan hides an
artifact until evidence is current again; a content-invalidated artifact does not
revive after rescan. The logical-erasure limits above also apply here.

Before a pilot, run the frozen cases on pinned baseline/candidate configurations,
independently label citation support and unsupported answers, and measure review
effort/time to accepted output. Source lifecycle UI, authenticated organizational
access, larger-corpus performance and repeatable backup/recovery remain additional
work. This implementation belongs to the free/open core.

### Internal cited-calculation diagnostic

The internal `investigation-v8` diagnostic adds an operation plan and deterministic
cited calculations. It is available through the Python service only; the default
and answer-preview versions are unchanged. The planner sees the issue and question
IDs, and may miss requested parts. An explicit operation plan can also be supplied
at the Python boundary. Each planned request has a required result. The model selects
source quantities; the server extracts their numbers and units from complete
source spans contained in presented snippets and performs the fixed arithmetic
using Decimal. Full source context prevents a presentation cutoff from changing a
number or unit. Unrecognized catalog units, missing inputs, incompatible units and
zero denominators do not produce a computed value. Model-reported conflicts or
missing evidence remain uncomputed; detecting those conditions still requires
semantic review. At most 100 extracted quantities are accepted; larger
catalogs fail the diagnostic rather than silently dropping evidence. Planning and
answering share the 240-second soft deadline, with limits of 800 and 2,000 output
tokens respectively. String lengths are enforced after generation (500 characters
per request and 1,000 per answer or explanation), avoiding unsupported deeply
nested grammar expansions in the installed backend. Original inputs, arithmetic,
rounding and provenance remain
available separately from any reviewer correction and are included in the handoff.
A valid calculation does not establish correct operand selection, source authority,
comparability, question coverage or owner acceptance; inspect the evidence.


Supported operations are signed difference, percentage-point difference, percent
change and part/total percentage. A generic difference between two percentage
inputs also yields percentage points; it never implies relative percent change.
The unit catalog recognizes milliseconds,
seconds, minutes, hours, percent signs, a bounded count-unit vocabulary and plain
numbers. It deliberately omits ambiguous formats, currency, composite rates and
unsupported units. Time calculations normalize to seconds. Results use at most
six fractional digits with `ROUND_HALF_UP`; exact selected inputs, normalization
factors, operand roles and the evaluated expression are retained. A source span
split across snippets is omitted instead of reconstructed from a partial number.

An owner-supplied plan avoids model planning, but still relies on model operand
selection and the same source validation:

```python
item = await create_investigation(
    scope, "How did latency change?", "latency", loaded_model_slug,
    template_version="investigation-v8",
    operation_plan=[{"question_id": "Q1", "request": "Latency change",
                     "operation": "difference"}],
)
```

This internal Python option is not exposed by the public creation route or the
desktop preview switch. Corrections lose calculation metadata, even when the
reviewer retains the same text; the original trace remains labelled separately
in the handoff. Source removal, revision changes and scope restrictions continue
to invalidate or hide the whole investigation, including its calculation traces.

For shared-unit forms such as `from 100 to 200 ms` or `from 100 to 200 sessions`,
the full expression must be in one presented snippet. Each input keeps the
source unit text and its snippet-relative offsets in the calculation trace.

The experimental reviewed-operation mode suggests an evidence-blind plan in two
bounded local model calls, then lets the owner edit and explicitly confirm each
request and operation before generating a v9 draft. Confirmation acknowledges the
plan, not the factual answers. The plan endpoint does not save a draft. Generated
results keep each operation separately, include known cited operands and source
extraction notices, and distinguish unavailable, unsupported, zero-denominator and
conflicting evidence. The owner must inspect the plan, source excerpts, notices and
answers; valid arithmetic does not establish authority, selection or completeness.
Source invalidation and correction rules apply, and exports keep the original
machine calculation separate from the reviewer's correction.

Use **Reviewed calculations (experimental)** in Investigations. Write one to twelve
distinct requests or choose **Suggest a plan**, edit the request text and calculation
type, then select **Confirm plan and draft**. Suggesting replaces the current plan;
edits made while suggestions are pending take precedence. Recoverable failures retain
the entered plan. This local opt-in does not replace the default brief or answer preview.

`POST /company-brain/investigations/plan` accepts `workspace`, `collection_ids`,
`issue`, `model_slug` and an optional `generation_profile`. It returns editable
`operations` rows with `request` and `operation`, model identity and review notices.
Creation accepts the same rows as `reviewed_operations` with `plan_confirmed: true`;
these cannot be combined with `answer_each_question` or `questions`. The five operation
types are `answer`, `difference`, `percentage_point_difference`, `percent_change` and
`percentage`. Confirmation is explicit on each creation; suggestions alone save nothing.

V9 uses the versioned quantity extractor and calculator v2; v8 retains v1 behavior.
V2 supports bounded spelled integers through ninety-nine and additional plural count
tokens with exact lexical identity. It does not establish nounhood or comparability,
infer general singular/plural equivalents, or support arbitrary compound noun phrases.
Spelled decimals, fractions, uncertainty bounds and ambiguous ranges are excluded;
complete shared-unit forms such as `from five to ten widgets` are supported. Notices
identify number-like text the extractor did not handle, not proof that a source value
is absent. A lone operand has no verified before/after role and cannot establish a
zero denominator. The model still selects evidence, operand order and conflict status.

Owners can review the inputs and limitation evidence for a saved v9 draft. The
`calculation-preview` endpoint accepts one to twelve calculation review rows, each
containing `request_id`, `status` (`calculate`, `missing`, `unsupported`, or
`conflicting`), ordered `operand_ids`, `snippet_ids`, and a reason. Input IDs must
belong to the saved source-bound quantity catalog; evidence is drawn from the saved
snippet catalog, including snippets omitted from the model's original answer. The
server adds the selected inputs' excerpts. Two compatible, distinct inputs are
required for calculation. Arithmetic and zero-denominator checks are deterministic;
source choice, authority, and unavailability are reviewer assessments.

Preview does not save or invoke a model. To save, submit the same rows with a
`correct` review and a corrected brief containing the exact preview answers. The
server rebuilds both and rejects changes to reviewed answers. The original brief and
machine traces remain intact; reviewer choices, reasons, results, and exact excerpts
are recorded separately and included in export. The usual scope, privacy, and source
freshness checks apply.

New v9 drafts pin `quantity_parser_version` to `v3`, which adds bounded two-word
count units such as `support tickets`. Unsupported scales, ambiguous expressions,
and longer phrases remain unextracted. This is lexical extraction, not a check of
subject, authority, or comparability. Existing v9 drafts without this field replay
their original v2 catalog; v1 and v2 extraction are unchanged. Model-generated
unavailability reasons are labeled assessments rather than verified absence.

The diagnostic runner supports `--operation-mode fixture_supplied` for v9 with a
checksummed `confirmed_operations.json`. Those runs retain `fixture_supplied` origin,
never owner confirmation. Automatic v9 runs retain `planned_unreviewed` origin. Compare
them separately; synthetic plans do not measure owner effort or factual acceptance.

The `investigation-v10` candidate is available only through the Python service for
internal evaluation; it does not change the default brief, answer preview, or v9
reviewed-calculation UI. Calculation preview is unavailable for v10 drafts. V10
asks the model for named operand roles and separate known-input, competing, and
limitation excerpt IDs. It requires cited limitation evidence for a `documented`
basis; `not_established` means the presented evidence did not establish the input,
not that the document globally lacks it. The service validates the structure and
citations, not whether the model's assessment or rationale is semantically correct.
The existing deterministic v3 calculator performs the arithmetic, and traces retain
roles and evidence groups for audit.

The v9 correction form labels subtraction inputs as the amount to subtract and
the amount to subtract from, percentage change as baseline/current, and a
percentage as part/total. It displays the selected expression before preview;
the server remains the authority for unit conversion and calculated results.

### Internal investigation-v11 experiment

Service-only v11 combines deterministic v2 retrieval with bounded `paragraph-v1` source snippets. It preserves v10 calculation semantics and adds ordered factual answer parts: each part references an exact request substring, an assessment, and source snippet IDs. The service checks substring order, bounds, IDs and excerpt provenance and renders the aggregate from the parts. It records unmatched request text and explicitly marks semantic request coverage unverified; neither citation validation nor span validation establishes entailment or completeness. Source instructions are untrusted data. Corrections edit aggregate answer text and citations while the original machine-generated parts remain in the immutable draft. V11 is opt-in and does not promote or change existing defaults. Generation and planning share the existing time limit; output and excerpt counts are bounded. This contract needs fresh evaluation before any quality or readiness claim.

The owner-selected calculation preview remains available only for v9; it refuses v11 drafts. Ordinary v11 corrections reconstruct `paragraph-v1` snippets and preserve the original machine parts in the saved draft.

### Internal investigation-v12 experiment

Service-only v12 retains v10 calculation semantics and v11's deterministic retrieval, with `paragraph-v2` source snippets. A separate bounded, source-free model call proposes exact ordered request spans for all factual operations. The service validates spans and records unmatched text, assigns fixed part IDs, and requires a final answer for each ID. The automatically generated part plan is `planned_unreviewed`, including when the operation plan was supplied by a fixture or confirmed by an owner. Span and citation checks do not establish semantic coverage or entailment. Factual-only final prompts omit calculation quantity catalogs and extraction notices while preserving source snippets and evidence omission notices; both catalogs and notices remain in provenance. Mixed and calculation plans retain the calculation context. Original parts remain in the draft when aggregate answers are corrected; correction reconstructs the stored snippet version. Planning and generation share the 240-second deadline and bounded outputs. This internal candidate does not change the default or expose a public switch; calculation preview remains v9 only. It requires fresh evaluation before any quality claim.

### Internal bounded answers and compound durations (v13)

The service-only `investigation-v13` experiment retains the full original request
while planning up to four exact ordered request spans. If a well-shaped proposal
paraphrases, overlaps or invents text, the server falls back to the entire original
request as one part. It records the proposal and fallback separately as unreviewed
diagnostics; answer headings always quote request text. This does not establish
semantic coverage. Malformed JSON, duplicate keys or invalid plan shape still fail.

Server-assigned part IDs are required in the final answer. Generation schemas,
prompt-visible per-part limits and validation share a 4,000-character aggregate
budget per factual operation, including separators, with at most 1,000 characters
per part. A single-part fallback therefore has a 1,000-character limit. Older versions retain their
1,000-character aggregate limit. Output is never silently truncated or repaired.
Each factual part may cite up to eight snippets; the aggregate and its correction
may cite up to 32 distinct snippets. Calculations retain their eight-snippet limit.
Factual-only requests use a short factual instruction; mixed requests retain the
explicit calculation contract. Source excerpts remain server-rendered and bound
to scoped retrieved sources. Corrections allow the same bounded aggregate while
retaining original machine parts and calculation traces separately and unchanged.

V13 uses the versioned `v4` quantity parser for explicit calendar, business and
working day quantities. These units remain distinct and cannot be converted or
mixed; supported differences and percentage changes are computed deterministically.
Lexical exclusion of nearby negation, approximate values and ranges is conservative,
not proof that a source asserts a measurement. Unsupported expressions retain
extraction notices. V1–v3 quantity extraction and v11/v12 answer replay stay intact.
A request for both a difference and a percentage change requires two reviewed
operations, plus a factual operation when measurement scope is requested. Fixture
plans are recorded as supplied plans, never owner confirmation or verified coverage.

V13 is not exposed by the public template selector and does not change defaults.
Qualification results are recorded under `bench/results/20261030-answer-contract-repair`;
unit-test success alone does not establish answer quality or pilot readiness.

The internal `investigation-v16` experiment uses one structured answer call when
an operation plan is supplied. It requests concise, individually cited claims,
with a global maximum of six factual claims and 2,400 text characters. The model
is asked for shorter claims; a claim within eight characters of its hard limit
is conservatively marked incomplete, regardless of punctuation. This warning
can have false positives and cannot establish complete output or source support.
Automatic operation planning can require additional calls. The deadline and output
token limit are unchanged. This is a service/export experiment, not a public API
or UI option; earlier versions and the default are unchanged.

The internal `investigation-v17` experiment generates an initial v16-style draft,
then asks the model to review it against the same presented evidence and original
request. With a supplied operation plan this uses two calls within the existing
shared deadline; automatic operation planning may require additional calls. Both
the initial draft and final machine-reviewed claims are retained separately from
any human correction. Neither the model's review nor citation matching proves
coverage or entailment. A failed review saves no partial draft. This is a service
and export experiment, not a public API or UI option.

The internal `investigation-v18` experiment is an extractive source answer,
not a synthesized explanation. With a supplied operation plan, the model selects
ordered evidence-unit IDs in one call and the server displays the exact selected
passages with per-passage citations. At most eight passages and 4,000 total answer
characters are allowed; exceeding the limit fails without a partial draft. The
model's status, passage relevance and coverage are unverified. Original passages
and warnings remain separate from a human correction in export. The deadline,
output-token limit and deterministic arithmetic are unchanged. This is a service
and export experiment, not a public API or desktop option.

Internal investigation-v19 experiments with model-proposed request-focus groups and exact selected source passages. Group labels and per-focus statuses are unverified coverage intent recorded in provenance and export; the aggregate status alone does not identify an unresolved focus or prove completeness. This internal experiment does not change the default investigation template.

Internal investigation-v20 tests a larger extractive display budget: up to 16 displayed passages and 8,000 factual characters across operations, with at most eight source IDs per focus. It retains the v19 focus-to-source selection and unverified coverage semantics. This is a diagnostic candidate, not a default or evidence that earlier failed runs passed.

Internal investigation-v21 experiments with explicitly labeled server-added structural context for positively bounded ordered-list blocks, preserving the paragraph-v3 snippet catalog. A block at an uncertain chunk boundary or with ambiguous nesting is not expanded and produces a visible limitation when selected. Structural grouping does not establish relevance, prerequisites, entailment, or complete request coverage; display budgets and correction replay remain enforced. This is an unqualified internal experiment, not a default.
V21 also displays the validated calculator's explicit input roles, normalized values and units, and evaluated expression for computed results. This is a deterministic presentation change; it does not verify model-selected operands or change arithmetic.

Internal `bounded-evidence-v1` experiment: see
[`docs/investigation-bounded-evidence.md`](../../../docs/investigation-bounded-evidence.md)
for whole-request native token budgeting, exact rate arithmetic, question-specific
unknowns and verified list boundaries. Public defaults remain unchanged.

### Bounded missing-evidence explanations (internal opt-in)

`unknown_mode="cited-unknown-v4"` requires `bounded-evidence-v1` and its pinned
native investigation profile. Each factual focus includes `unknown_reason`: an
empty string when supported or conflicting, or a nonempty, single-line reason
of at most 300 characters when unknown. The model identifies what the presented
evidence cannot establish and may cite related passages to explain a distinction.
The explanation is labeled an **unverified model assessment**. Exact source
excerpts remain separate; this is not proof that an answer is absent elsewhere.

Reasons, focus indices, selected snippet IDs and exact excerpts are saved in
`answer_coverage.unknown_explanations`. Review reconstructs the original answer
and rejects inconsistent traces. Owner corrections remain separate from that
original assessment in exports. This mode does not change public defaults, add
generation retries, or increase context, output or deadline limits. Added prompt
and schema fields are included in exact native input-budget measurement; displayed
explanations count toward the existing 8,000-character factual answer limit.
Structural validation checks formatting and bindings, not semantic correctness.

The earlier `cited-unknown-v1`, `cited-unknown-v2` and `cited-unknown-v3` contracts remain replayable
with their original instructions and field order. V2 asks for an assessment
before related passage selection. V3 additionally asks for an explicit distinction
when a nearby value, date, version or identifier could be mistaken for the missing
answer. This instruction remains a model behavior requirement, not a semantic
guarantee from structural validation.

V4 targets short explanations below the hard cap and requires terminal sentence
punctuation, rejecting the observed mid-word cutoff instead of silently editing
it. Punctuation is a structural guard, not proof of grammatical or semantic
completeness. The original model output remains in diagnostic receipts.


### Optional direct conclusions

The internal `presentation_mode="cited-conclusions-v7"` requires the bounded
evidence path and `cited-unknown-v4`. Each supported focus gets a short direct
answer, explicitly labeled as an unverified model assessment, followed by its
source IDs and unchanged exact quotations. Focus labels distinguish answers to
different policies or parts of a request. Unknown and conflicting focuses cannot
contain a supported conclusion; unknowns retain their cited missing-evidence
explanations. Arithmetic stays on the deterministic calculation path.

Conclusions use complete single-line text, aim for 200 characters, and have a
300-character hard cap. Controls, forged citation markers, missing required
conclusions and nonempty conclusions on unknown/conflicting focuses are rejected.
Added prompt/schema fields are measured within the same input/output/context and
time limits; all framing and prose counts toward the existing factual display
limit. Structural checks do not establish that a conclusion is entailed by its
quotes. Preserve qualifications and review semantic correctness separately.

`answer_coverage.cited_conclusions` saves each original focus, assessment,
conclusion, source IDs and exact excerpts. Review reconstructs this trace and
rejects inconsistent original records. Exports retain original conclusions and
evidence separately from owner corrections. Omitting this mode preserves the
prior extractive behavior and old saved records. Public defaults are unchanged.

The earlier `cited-conclusions-v1`, `cited-conclusions-v2` and
`cited-conclusions-v3`, `cited-conclusions-v4`, `cited-conclusions-v5` and `cited-conclusions-v6`
instructions, schemas and saved
records remain replayable. V2 additionally asks for every assertion in conclusions and unknown
explanations to be supported by its exact selected passages, avoids passage-ID
prose, retains essential timing/scope qualifiers and discourages repeated focuses.
V3 asks for a single short missing-evidence sentence targeting 160 characters,
leaving room below the unchanged 300-character cap. It retains validation that
rejects the observed clipped output rather than silently trimming or repairing it.
Terminal punctuation is a structural check, not proof of a complete sentence.
These instructions guide model behavior; they do not prove semantic entailment.

V4 gives focus labels a separate 60-character schema and validation limit, with
instructions to target short standalone headings below 40 characters. Related
conditions may share a conclusion; different policies retain separate labels.
This aims to avoid clipped headings and redundant comparisons without dropping
requested aspects. Overlong labels reject rather than being silently trimmed.
Length validation does not establish grammatical completeness or non-redundancy;
those remain explicit human evaluation criteria. Old modes retain their schema
limits and original trace reconstruction.

V5 retains the short-label contract. It asks each conclusion to stand alone with
material eligibility, scope and timing conditions and any documented exception
to a requirement. A list-item citation cannot establish an unstated budget
period; the model must select the needed context or omit an unnecessary detail.
Comparisons use a combined focus or separate item focuses, without an additional
restatement. These are generation instructions, not semantic validators. Original
first-exposure failures and subsequent consumed regressions stay separate.

V6 uses one consolidated factual/unknown/conclusion instruction instead of
appending overlapping extractive and free-prose instructions. Mixed plans retain
the existing numeric-operation instructions and formula guidance. Its schema
orders citation selection before explanation/conclusion generation. The response
fields, source binding, budgets and post-generation validators are unchanged.
This is an instruction/order experiment, not proof that every exception is kept.
Earlier versions retain their original instructions, property order and replay.

V7 retains the consolidated contract and source-first field order. A fictional
worked example demonstrates preserving an exception and residual approval
discretion in the conclusion itself. It is explicitly not evidence and does
not add source facts. Incidental details have lower priority than material
conditions when shortening a rule. This remains a generation experiment;
semantic correctness requires independent qualification.

Qualification status: the concise-label panel's four first-exposure CivicActions
cases passed 2/4 with v4. Consumed repair runs improved headings and comparison
repetition, but v6 still omitted a material receipt exception (10/11 overall),
and the v7 example did not repair it and regressed a prior distinction (9/11).
Neither mode is promoted. See `bench/results/20261104-investigation-concise-labels/`
for original drafts, frozen criteria, all revisions and the next implementation
decision. Unit tests and source-integrity audits do not establish entailment.

### Optional source-bound answer components

`presentation_mode="cited-components-v1"` explicitly captures conditions,
exceptions and distinctions before generating a primary answer. Each component
has bounded prose and one or two references to the focus's selected source IDs.
Supported answers permit conditions and exceptions; unknowns permit distinctions
between nearby facts and the requested information. Conflicting focuses carry no
prose components. The server combines every captured component with its primary
answer or missing-fact statement, without a second model call or summarization.

Each primary/component field is at most 160 characters, with at most two entries
per kind. The assembled conclusion or reason, including labels, remains limited
to 300 characters. Invalid references, status/field mismatches, malformed arrays,
forged citation markers and overflow reject the whole result. Text is never
trimmed to make an answer pass. Input token measurement includes the larger
schema; the 8,192 context, 2,000 output reserve, shared deadline and existing total
factual display limit remain in force.

The cited-conclusion trace retains primary texts, components, each component's
source IDs, the composed conclusion and exact source excerpts. Rendered component
source lines make those bindings observable during original-result reconstruction.
Correction and Markdown export preserve these original fields separately from
owner changes. Older contracts and public defaults remain unchanged.

This guarantees inclusion of captured components, not complete extraction or
semantic entailment. Component selection, classification and prose remain model
assessments requiring evaluation. Consumed regressions and fresh qualification
must be reported separately; there is no default/public promotion in this batch.

November 5 consumed evaluation: all 11 attempts saved, but only 8/11 factual and
5/10 applicable presentation checks passed. The receipt exception is captured,
but management discretion is still missing; the CLI distinction is empty and
travel introduces an unsupported exclusivity claim. A clipped heading and
fragmentary route conclusions also fail presentation. All 11 preservation,
source/budget, correction/export and cleanup audits pass. This mode is not
qualified; the earlier v6 remains the stronger measured comparison at 10/11.
See `bench/results/20261105-investigation-answer-components/` for original drafts,
frozen scoring and all failures. Next is a separate bounded verification pass for
unsupported claims and omitted qualifications, with its own latency and accuracy
measurement; a second model call must not be treated as ground truth.


### Separate local draft assessment

`POST /company-brain/investigations/{iid}/draft-verification` accepts the usual
`workspace` and `collection_ids`, plus an optional strict integer `seed` (default
7). This experimental endpoint applies only to an unreviewed
`cited-components-v1` draft using `bounded-evidence-v1` and `cited-unknown-v4`.
It is opt-in, has no automatic retry, and does not change the default creation
flow or add a desktop control.

The checker receives the original primary prose, captured components and every
whole cited passage for each focus. It first checks missing material source
qualifications, then claim support and wording completeness. It does not inspect
unselected evidence or owner corrections. An omission must quote an exact
contiguous span of the supplied passage. Fixed source/claim/focus inventories,
strict JSON and bounded explanations reject malformed or incomplete output.

A valid assessment is `needs_review` if any check reports an omission,
unsupported/uncertain claim or unclear prose, or a claim has no cited source.
Otherwise it is `no_issue_found`. Both are model assessments, not proof or owner
approval. A second pass using the same model may share the first pass's errors.
Capacity, timeout, truncated/malformed output, runtime failure or a changed model
records `not_checked`; cancellation propagates without saving an assessment.

This is one additional generation with its own 240-second total deadline and
2,000-token output reserve. The serving engine measures the full prompt/schema;
no passage is dropped or clipped to fit. Model hash and loaded context are pinned
again under inference admission. Potluck's application prompt-cache path is
disabled for this request; native engine prefix reuse is not asserted absent.

Up to three reports are retained in `provenance.draft_verifications`, including
exact input, check inventory, derived result, settings, input budget and elapsed
time. Consistency hashes bind these fields for replay, not as a signature against
a database administrator. The original brief, evidence, status, review and
outcome stay unchanged. Before saving, a write transaction rechecks source
bindings and the original brief/evidence/provenance and draft status. A source
rescan, concurrent owner review or competing saved check cannot be overwritten.

Owner review and Markdown export validate and retain the original reports.
Exports label them unverified and explicitly exclude owner corrections from the
assessment's scope. Reports cannot approve an investigation or repair its text.
Checker sensitivity, false positives and extra latency must be measured
separately from the unchanged original answer accuracy.

November 5 checker evaluation: all 11 original drafts remained unchanged and all
11 reports survived correction/export. Of 10 applicable drafts, eight returned
valid assessments and two were rejected for invalid omission quotes, despite
normal native completion. Against frozen criteria, there were zero usable,
precise detections of the five known problems, three false review flags among
five clean drafts, and three known problems incorrectly cleared. One problem was
flagged for the wrong reason; another produced an invalid response. Extra check
latency is recorded in the benchmark receipts. Original factual 8/11 and
presentation 5/10 scores remain unchanged. Do not use this experiment as a
reliability gate. See `bench/results/20261105-investigation-draft-verification/`.


### Explicit draft-checking contract v2

The same optional endpoint accepts `version="draft-verification-v2"`. Omitting
this field retains the v1 checking contract. Saved reports select their own
version for reconstruction, schema and result validation. Mixed v1/v2 histories
retain original bytes through owner review and export; default draft creation is
unchanged.

V2 distinguishes information absent from a source from a qualification present
in the source but omitted from the draft. Each source comparison includes all of
that group's draft sentences, request and status. Whole cited passages and
per-claim allowed source IDs remain unchanged. Three named inventories separate
`draft_omissions`, `claim_support` and `draft_wording`; the latter inspects actual
headings and sentences rather than whether sources answer the question.

Passing checks require empty reasons. Findings request one complete sentence
under 90 characters, with a 120-character validation cap and terminal punctuation.
This constrains format; punctuation does not prove a reason is semantically
complete or correct. Only `omitted_from_draft` may supply an omitted quote, and it
must be a nonempty exact contiguous span from that particular source row. The
server never fabricates or moves a quote to make a response pass.

The request includes more explicit draft context; the response avoids explaining
every passing check. Both are measured within the unchanged 8,192-token context,
2,000-token output reserve, one generation and separate 240-second deadline.
Malformed responses remain not_checked. Neither a clear assessment nor a valid
schema establishes truth or approves the original draft.

V2 consumed evaluation on November 5: all five clean drafts clear, compared with
three false review flags in v1. Nine of ten applicable responses validate; the
receipt response correctly locates management discretion in raw output but also
copies that quote onto the wrong source row, so the whole response is rejected.
The travel restriction is flagged, with an overbroad rationale that does not
isolate exclusivity. Three known problems remain cleared: route fragments, the
CLI identifier distinction and the clipped heading. Under the unchanged strict
criterion, zero of five detections are fully precise and usable; one is a
partially useful claim-level flag. Median added latency is 26.801s versus
61.591s in v1 on this consumed panel. Original factual and presentation scores
are unchanged. Preserve v1 and v2 failures. Next are exact source-clause IDs,
clause-level coverage and mechanical heading warnings, with source section
context retained for list items. See
`bench/results/20261105-investigation-draft-checks-v2/`. No promotion follows.


### Source-unit checking v3

The endpoint also accepts explicit `version="draft-verification-v3"`. V1 stays
the default, and saved v1/v2 reports reconstruct under their original contracts.

V3 partitions every cited passage into fixed source units with IDs and exact
start/end offsets. The response must assess every unit and cannot generate
omission quotes. Export derives a flagged unit's text from the immutable input.
This prevents wrong-source quote copying by construction; it cannot prevent a
model from choosing the wrong verdict. The conservative sentence heuristics are
not a grammatical clause parser. Every original character remains in a unit,
and the full cited passage remains available.

Each cited passage retains its source heading and, for a list item, a contiguous
introduction ending in a colon before the immediately preceding list items.
Context comes from the same saved source/citation and exact source offsets, not
the question or filename. The versioned scope and input policy explicitly
include that context; unrelated evidence is not checked. More than 96 units
rejects instead of dropping units. The existing prompt budget still applies.

Heading ellipses and unmatched brackets produce separate deterministic review
warnings. They remain visible even when model checking fails, and cannot be
cleared or invented by model output. These are advisory signals: deliberate
ellipses or mathematical interval notation can also trigger them. They do not
prove a heading is malformed. Unknown, capacity, transport and invalid-output
results remain unverified; no original draft, review or outcome is overwritten.

V3 retains the same one-call, 2,000-output-token and 240-second bounds.
Live quality results and exact tested conditions belong in
`bench/results/20261105-investigation-source-units/`; software tests alone do
not qualify the checker or the original answer workflow for promotion.


V3 evaluation on November 5 retained all 11 originals and produced 10 valid
applicable checks, with no generated wrong-source quote failures. It precisely
flagged unsupported travel exclusivity, and mechanical warnings identified the
clipped heading. That heading report also contained an irrelevant hardware
warning; a clean threshold answer received an irrelevant omission flag. Three
known problems still cleared. Two specific problems were identified, but only
one complete report was fully precise. Median added checking time was 37.0035s,
versus v2's 26.801s. Original 8/11 factual and 5/10 presentation scores did not
change.

A separate, independently authored six-case synthetic checker set gave the
expected case decisions for both v2 and v3 at seeds 7 and 11 (24 total calls,
not 24 independent cases). This simpler set establishes no advantage over v2.
One v3 repeat-run warning was attached to the email unit while describing an SMS
qualification elsewhere. Fixed IDs prevent quote copying, not semantic
misattribution. V3 was slower on this set. Keep the checker experimental.

The actual Potluck CLI completed two bounded read-only helper jobs through an
isolated owned native adapter. The first provided a regression-test suggestion;
the second, with the new enforced read-only mode, found the receipt-discretion
gap in a primary-selected single-source review. This narrower consumed diagnostic
is not a paired improvement claim. It supports using Potluck for small reviewed
assistance tasks, not autonomous approval. The ordinary gateway stayed unchanged.

### Focused correction previews (experimental)

`POST /company-brain/investigations/{id}/revision-preview` accepts the existing
workspace and collection scope plus `group_id` (for example `G1`) and optional
integer `seed` (default 7). It applies only to unreviewed component-contract
investigations. Groups use the stable ordering from the original cited answer;
calculation-only answers have no applicable group. This endpoint is opt-in and
is not called by default generation or the UI.

The `focused-revision-v6` service uses the unchanged `focused-revision-v5`
generation contract. It generates a fresh answer for one whole group
from its selected passages and the complete saved text of those same citations.
Selected quote offsets are checked against that text; this does not retrieve
additional sources. Source headings and bound list introductions are also retained.
The original prose stays in the receipt, not the model messages. An unknown
original status selects a task requesting explicit distinctions between nearby
values and the requested fact. Single-group inputs use the full request instead
of the old shortened focus; multi-group inputs retain their individual focus
alongside the full request. The model can still confuse adjacent questions.
It returns a detached, unverified proposal with per-sentence source IDs. It never
overwrites a draft, accepts an answer or saves an owner correction. A preview is
ephemeral; callers must explicitly retain its receipt if needed. There is no
automatic application endpoint.

One request permits one local call, 700 output tokens and 180 seconds. Exact
input measurement rejects over-capacity prompts without clipping or fallback.
Each replacement allows five sentences of at most 240 characters each, at most
900 characters total, and a generated topic field bounded at 60 characters. This is more room than the
original component contract's 300-character combined prose budget; any observed
improvement must be attributed to both focused review and the changed output
budget, not focus alone. Citation membership, complete terminal punctuation and
bounded shape are checked mechanically; factual entailment remains unverified.

`request-label-v1` presentation always uses the full original request (at most
500 characters) as `display_label`. The generated focus remains unchanged in
the proposal and in `generated_focus`; ellipses and unbalanced brackets produce
visible `heading_warnings` rather than discarding otherwise valid answer prose.
Callers must render the full request as plain text, and show these warnings. This
is a deterministic presentation change, not improved model heading generation
or proof of a correct answer. Frozen v5 heading failures remain failures in their
original records. V6 retains v5 generation byte-for-byte and changes presentation only. A separate
frozen v7 diagnostic narrowed per-group prompts and requested explicit material
conditions. It recovered some omissions but regressed controls, so it is retained
only as an evaluation artifact and does not replace the v6 implementation.

The response includes input/source bindings, hashes, settings, measured budget,
call count, elapsed time, and explicit failure status. Partial output is rejected
without retry. The original is re-read under the same scope after inference;
source invalidation, revoked access or a changed review/draft prevents returning
an applicable preview. Original quotations and saved owner history remain
unchanged. The first frozen v1 schema was rejected by native schema admission
before any revision generation; its failures are retained. V2 removes the
unsupported `uniqueItems` grammar keyword while enforcing uniqueness in the
decoder. The v2 keep-or-revise strategy retained all known defects on the
consumed originals; v4 removes that judgment step and asks for source-based
regeneration. It also places citations only in structured reference fields to
avoid inline markers breaking terminal punctuation checks. V4 still lost some
antecedents and copied shortened headings; v5 supplies saved same-citation
context and removes the redundant single-group label. Live receipts are in
`bench/results/20261105-investigation-focused-revisions/`.

### Longer supported component answers (opt-in)

`presentation_mode="cited-components-v2"` preserves v1's schema, source bindings,
per-component 160-character cap and two-component-per-kind limit. It raises only
the combined supported-answer cap from 300 to 900 characters so a primary answer,
conditions and exceptions can fit together. Unknown answers retain the original
300-character combined limit. The existing overall factual-text limit still
applies; oversized responses are rejected, never clipped. V1 prompts and stored
records retain their original contract. The default mode is unchanged.

V2 supports normal persistence, exact replay, export and detached revision preview.
A larger allowance is not evidence of semantic completeness. The live evaluation
uses manually supplied retrieval queries and reviewed operations; it does not
qualify automatic planning or replace owner review.


### Policy comparison scope guard (opt-in v2)

`investigation_evidence_workflow.propose(..., version="evidence-workflow-v2")` selects `evidence-coverage-v2`. The default remains workflow v1, and the ordinary proposal path remains unchanged. Each call captures its own contract before inference; concurrent versions do not replace global decoders.

The new coverage contract reuses the value inventory's validated selected-focus gate. Outside explicit version/release/identifier questions, the schema requires an empty comparison array and the decoder rejects any nonempty comparison without salvage. Policy qualifiers remain source classifications and dependencies. Technical questions retain the previous comparison schema and instructions apart from the version marker. The conservative English lexical gate is not semantic classification and may exclude unrecognized wording.

This targets the preserved v43 eligibility failure, where a correct `no` answer carried a comparison that requires `not_stated`. The new candidate must pass the unchanged complete consumed matrix before fresh qualification. V44 evidence is archived under `bench/results/20261106-investigation-policy-comparison-scope`; valid structured output alone does not establish correctness.

V44 completed with 24 structurally valid proposals, but 23/24 consumed outcomes met the frozen criteria and primary review. Both eligibility repeats now correctly return `no` without an incompatible comparison. Seed 7 instead omits the receipt Wizard stage, so this is not an overall accuracy gain over v43. Travel and technical distinction controls remain intact. The complete local review correctly returns `NONE`; the known missing-rule control incorrectly also returns `NONE`. All 34 evaluated stages replayed and the owned runtime was independently cleaned up. The candidate remains unqualified, defaults unchanged and fresh holdout sealed. Next: separately version procedural-stage retention and explicit selected-versus-supplied evidence in review packets.

### Combined evidence workflow candidate

`investigation_evidence_workflow.propose(scope, investigation_id, group_id)` is a detached internal opt-in. It uses `evidence-coverage-v1` to select policy evidence, then runs a separately versioned literal-value inventory and fact matcher only when the selected focus asks about a version, release or identifier. The ordinary proposal path still defaults to `evidence-comparisons-v1`.

The coverage prompt preserves governing rules alongside exceptions and retains applicable budget eligibility, proration, approval and expiry conditions. This is a measured candidate, not a guarantee of completeness. Every component is replayed against the same source envelope before composition. Results retain source IDs, offsets, selection scope and dependencies; absence claims remain limited to supplied records. A missing or invalid required stage, an incompatible verdict or a source/state change cannot become a combined proposal. No investigation is written or approved.

Each workflow permits at most three model calls, 700 output tokens per call, a 180-second per-stage cancellation deadline and a shared 360-second cancellation deadline. Native worker cleanup may take longer than the cancellation deadline. Per-stage failures, call totals and elapsed time are retained. The contract is captured per call; no process-global decoder replacement is used.

The value inventory derives quotations from full source units. The existing 240-character quote bound is retained deliberately: a selected fact in a longer unit fails validation rather than being silently shortened. Its English focus gate and three-fact/four-unit caps also limit applicability. Exact source binding does not prove that a value answers the question.

V43 freezes all 12 consumed groups at seeds 7 and 11, with the original rubric plus explicit travel, clean comparison and selected-route checks. Evaluation evidence belongs in `bench/results/20261106-investigation-combined-workflow`. The fresh holdout and actual owner review remain separate gates. Defaults are unchanged.

V43 completed with 23/24 consumed outcomes meeting the frozen criteria and primary review. Both travel results preserve the general reimbursement rule and no-carryover, and both technical distinction repeats pass. One eligibility result combines `no` with an absent-fact comparison and is correctly rejected; no salvage or retry. All 34 stage traces replayed. Two actual local CLI reviews completed, but the travel reviewer falsely flagged an already-bound list introduction. The candidate remains unqualified; the fresh holdout is unused. The next candidate must scope policy comparisons structurally before another full consumed regression.

### Versioned selected-question context candidate

`investigation_evidence_comparisons_v2.py` adds an explicitly selected
`evidence-comparisons-v2` contract. Only selected-group requests receive the full
original question plus an instruction to answer their selected focus. The v1
contract and default `propose` behavior remain unchanged. To request the new
internal candidate, pass `version="evidence-comparisons-v2"` to the comparison
proposal service. Unknown versions fail before inference. Contract selection is
local to each generation call; mixed asynchronous versions do not mutate a
shared implementation. The result is detached, source-bound and unverified,
with the v2 version and actual input digest. No public endpoint, apply action or
automatic approval is added.

The consumed context regression admitted 24/24 proposals and met 19/24 frozen
necessary checks. Both reimbursement routes and receipt controls pass; technical
comparisons and one travel result still need work. The separate benchmark-only
source-first/value-only comparison pipeline passed 12 targeted final decisions
at two seeds, including six policy controls structurally excluded by its lexical
scope. It copies exact source quotations, then matches every extracted fact to
the question. Acknowledgement flags are discarded by the matcher. The scanner
alone is not semantically clean, and derived full units must fit a 240-character
bound. No combined workflow, fresh-data accuracy or owner-effort qualification
is claimed. See the three `bench/results/20261106-investigation-*` archives for
versioned-context, source-inventory and derived-value-context results, all failed
attempts, source bindings, sampling receipts and cleanup evidence. Next is an
explicitly versioned composition, travel coverage repair and a full combined
regression before the sealed holdout and actual owner review.

### Source-focus probes and effective sampler accounting

A benchmark-only selected-group context repair preserves the original full question while answering only the selected focus. Route2/2 and receipt2/2 targeted checks pass; the other10single-request prompt bytes are unchanged. The separate source comparison task passes10/12, with CLI protocol/release distinctions still missing. Ordering-only and source-value inventory alternatives regress controls and are rejected. No combined candidate or default is qualified; all183production files remain unchanged.

Sampling correction: the caller requests temperature0, but the registered native profile applies temperature0.7/top_p0.8/top_k20/min_p0/repeat1. Older prose describing effective temperature0 is incorrect; preserved profile receipts and identical profiles keep paired comparisons valid. New probes capture adapter-bound sampler kwargs explicitly. See `bench/results/20261106-investigation-source-focus/README.md` and adjacent comparison-order/value-inventory receipts for exact scope, tests, failures and clean shutdowns.

Next: version the question-context repair, bind concrete source values/types for comparisons, then full consumed regression including travel qualifiers before the unused synthetic holdout and complete workflow/owner-effort review.

### Experimental source comparisons and bounded absence

Detached `evidence-comparisons-v1` uses exact source-unit references and fixed comparison kinds, with explicit source-limited absence that can contain no selected evidence. Supported verdicts still require evidence. Scope, raw classifications, exact quotations and dependency closure are preserved; older versions/defaults unchanged.

Consumed12groups xseeds7/11:24/24 admitted and all24 integrity replays pass, but zero comparison records emitted. IPC absence admission is repaired and after-hours prerequisites hold bothseeds; CLI/cluster distinctions remain missing and seed11 reimbursement route regresses. Do not promote or equate valid output with factual quality. Original8/11 factual and5/10 presentation unchanged.

Two actual local Potluck CLI read-only reviews completed; primary reviewed a correct after-hours no-omission result and guided CLI distinction with an unsourced example excluded from adoption. All183 frozen production files/181 prior files and owned-runtime cleanup verified. Python5958 passed/5skip,focused88/packet22,allfast/types/privacy passed. See `bench/results/20261105-investigation-source-comparisons/README.md` for measured conditions, failures and next probe.

### Experimental dependency-aware correction candidate

`evidence-dependencies-v1` adds exact source-unit dependency closure, source-bound value distinctions, explicit selected-group scope and question-form verdict restrictions without changing v34/v6 or defaults. It remains detached and unverified.

Frozen consumed diagnostic:12 groups at seeds7/11,20/24 admitted, all24 integrity replays pass. After-hours prerequisites and conditional verdict are repaired at both seeds; receipt scope/verdict improves. IPC absence is incorrectly rejected by the new evidence requirement; CLI type labels fail literal binding. Cluster distinction, some budget context, duplicate selections and type prose remain unresolved. Do not promote or treat admission as factual correctness. Original whole-draft8/11 factual and5/10 presentation unchanged.

Three actual Potluck CLI read-only jobs completed using compact packets and one read/two calls each. Primary reviewed a correct known-omission flag, correct after-hours no-omission result and a redundant benefit warning. Full Python5931 passed/5 skipped; focused61/packet15 and all fast/type/privacy checks pass. Exact snapshots and owned-runtime cleanup verified. See `bench/results/20261105-investigation-evidence-dependencies/README.md` for conditions, failures and next work.

### Experimental source-unit correction candidate

`investigation_evidence_revision.py` and its bounded service implement a separate,
internal `evidence-revision-v1` candidate. It does not replace
`focused-revision-v6` or add a public API/apply flow. It expands the same saved
citations into exact paragraph/sentence units with offsets, merges identical
citation/context aliases, and requires an explicit decision for every unit.
Original draft wording and status are excluded from model messages.

The model selects answer, condition, exception, distinction, background or
uncertain and a fixed verdict. The service returns selected units as separate
exact quotations; it generates no paraphrased answer. Background and uncertain
units remain inspectable. Every result remains unverified and requires review.
The deterministic guarantee is preservation of selected text, not correct
selection, a correct verdict or complete source coverage. Source headings and
list introductions remain bound to their original citations.

Admission is bounded to 48 units and 12,000 distinct source characters, plus
measured native context capacity. One local call allows 700 output tokens and
180 seconds, with model identity checks, no fallback/retry, and rejection of
partial or invalid output. The internal proposal service rechecks scoped original
state after inference and writes no investigation/review data. Old correction
code and defaults remain unchanged.

The frozen diagnostic completed 24 attempts across 12 consumed groups at seeds
7 and 11: 22 proposals, two evidence-free supported verdicts rejected. All 24
trace/quote integrity replays pass, but primary review found missing after-hours
prerequisites, missing CLI protocol/release distinctions and incorrect verdicts.
Do not promote this candidate. These are selected-group diagnostics, not new
whole-draft scores; previous factual/presentation scores remain unchanged.

The original run exited 1 after the evaluations because a copied helper template
collided with exclusive packet creation. A separate frozen helper-only recovery
preserves that failure and reruns no evaluation cases. Actual Potluck CLI
read-only review completed in 33.62 seconds and correctly identified one omitted
eligibility rule; source packet unchanged. Both owned runtimes have independent
PID/port cleanup proof. See
`bench/results/20261105-investigation-evidence-revision/` for all attempts,
primary review, helper failure/recovery and source licenses. Next work is explicit
exception-to-prerequisite linkage, question-compatible verdicts, group scope and
compact helper packets. Fresh complete-document, automatic-planning, paired/
formula and current owner-effort qualification remain open.


## First-use evidence outcome gate and owner review

The unchanged evidence-workflow-v3 candidate passes all five original synthetic holdout criteria on four complete short supplied documents, separately from consumed24/24 and OfferLume repeats5/5. The typed actual CLI reviewer controls pass2/2. Exact source/request/replay and owned-runtime cleanup checks passed. H2’s auxiliary inventory missed a protocol literal despite the correct final context distinction; independent extraction remains unqualified. These results support a measured correction-batch owner review, not full retrieval/planning or production promotion. See `bench/results/20261106-investigation-fresh-qualification/REPORT.md`; preserve earlier checkpoint scores and defaults.

The opt-in `investigation_agent_review_bridge` module exposes bounded stdin/stdout
prepare/decode operations for the resumable CLI harness. It reuses coverage-v3
without changing its prompt, schema or defaults. Source and retained-unit bindings
are checked before generation and replayed during decoding; original envelope JSON
is parsed strictly. An implementation fingerprint and immutable raw receipt keep
validation separate from model execution and primary semantic review. See
`docs/potluck-agent-harness.md` for the v4 queue contract and deployment limitations.

### Detached source-value candidate inventory

The internal `evidence-workflow-v4` opt-in wraps the existing v3 context assessment with `source-value-inventory-v2`. It enumerates eligible source tokens deterministically and requires a classification for every candidate before matching retained facts to the selected question. Literal values and offsets are copied from the source; a protocol revision cannot substitute for an absent product release. Classifications remain unverified and need primary factual review.

This path is bounded to 16 candidates, three retained facts, source type quotes of 240 characters, and three inference calls under one 360-second deadline. Ambiguous bindings, oversized candidate units and missing classifications fail explicitly. Hash field labels and embedded URL/path fragments are excluded. Existing workflow versions and defaults remain unchanged. Narrow synthetic and consumed-case evidence does not qualify full-document retrieval or production deployment.

The subsequent internal `evidence-workflow-v5` / `source-value-inventory-v3` candidate makes the semantic rules explicit: response acknowledgement flags are incidental; related absent-value statements can supply type context; another typed fact remains a distinction even when one fact answers the question. V4 and its measured failures remain preserved. This opt-in path still requires semantic evaluation and primary review.

### Explicit typed-fact retention trial

`evidence-workflow-v6` opts into `source-value-inventory-v4`. Its matcher receives source-bound model classifications and must account for every retained typed fact as requested or different. `invalid_fact` rejects the entire result; it does not silently drop a fact or return partial success. Type-context selection concerns value/entity distinctions and explicit absence, excluding operational acknowledgement status. Existing versions and defaults are unchanged. Classification is still unverified and requires semantic evaluation. Existing limits, including rejection of mixed numeric protocol/status values in one source unit, remain explicit. V50 is a bounded supplied-evidence evaluation, not full-document retrieval qualification.

### Exhaustive source-context trial

`evidence-workflow-v7` explicitly selects `source-value-inventory-v5`. In addition to every literal candidate, every eligible noncandidate source unit needs a classification: typed value definition, typed value distinction, explicit typed absence or background. Retained context is derived locally from the complete map. Missing, duplicate, orphaned or tampered decisions reject; at most 16 context candidates and four selected source units are supported. The typed matcher and all earlier versions/defaults are preserved. Complete structural coverage does not prove semantic relevance; V51 tests exact classifications separately.

### Category definitions without repeated literals

`evidence-workflow-v8` selects `source-value-inventory-v6`. Its source-only guidance includes definitions of what the retained value’s type or category identifies or governs, even without repeating the literal. Operational outcomes, readiness and acknowledgements remain background. V6 inventory changes only version tags and this general guidance; V1–V7/defaults and all bounds/replay guards remain unchanged. V51 preserved5/5 original,12/12 repeat,2/3 new; V52 remains unqualified: 5/5 original, 14/15 repeat, 2/3 new. No full-document or production qualification is implied.

### Explicit context function labels

Internal `evidence-workflow-v9` uses inventory-v7 wire labels for definition/function, type distinction, explicit absence and operational/unrelated context, normalized into unchanged stored categories. Replay reverses the map and validates the complete source binding. Explicit contrasts take precedence over individual definitions. V52 remains unqualified at5/5 original,14/15 repeat,2/3 new. V53 remains unqualified at 4/5 original, 18/18 repeat, 3/3 new; all old versions/defaults preserved.

### Type meaning versus feature effects

Internal `evidence-workflow-v10`/inventory-v8 keeps explicit wire/canonical replay and narrows context to identifier/version-category and protocol/interface-format meaning. Nearby product-feature effects, guarantees, procedures and security assurances remain background unless the unit directly defines/contrasts a type or states related absence. V53 is unqualified at4/5original,18/18repeat,3/3new due extra lock-feature context. V54 passes 5/5 original, 21/21 repeat and 3/3 new after primary semantic review. 1216 affected checks and 64 unchanged legacy replays pass. The bounded source-value milestone is complete; see `bench/results/20261106-investigation-inventory-milestone1/REPORT.md`; prior versions/defaults remain unchanged.


## Opt-in collection evidence workflow

`investigation_collection_workflow.prepare(scope, question)` uses the literal
question through the existing bounded query builder and v2 lexical retrieval. It
records the ready/recent indexed snapshot, all returned source candidates, exact
citation bindings and retrieval omissions. Whole chunks are admitted in rank
order only when they fit the unchanged evidence-workflow-v10 limits (12,000 source
characters and 48 source units); skipped citations are reported. No source text
is clipped. The snapshot enumeration is capped at 1,000 documents, retrieval at
ten results. Unsupported input or zero admitted chunks fails explicitly.

`generate(scope, receipt, runtime, model=..., model_sha=..., seed=7)` rebuilds the
receipt before and after generation. Indexed source changes, revocation, stale snapshots
and caller-tampered inputs reject the result. Returned provenance limits absence
to selected retrieved excerpts; lexical retrieval is not a complete semantic
search of the collection. Reviewed claims are currently omitted explicitly.
Disk edits must be ingested before they change this snapshot; the adapter does not
add a filesystem watcher.

This is a detached developer API, with no router, default or ordinary gateway
change. Workspace/collection selection retains the existing local-owner trust
boundary and is not multi-user authorization. Supersession may be present in
source evidence; this adapter does not infer policy authority from document dates.
Complete-document answer quality and deployment readiness require separate
evaluation.

## Question-focused reviewed drafts

The default-off reviewed knowledge pilot offers an additional explicit **Focused passages (experimental)** choice. It keeps V5 reviewed-collection retrieval and uses the separately versioned V3 presentation: one bounded generated answer, whole exact selected source passages with structural governing context, and inspectable full reviewed statements. The V2 pilot default is unchanged. See [focused answer contract](../../../docs/investigation-focused-answers.md) and [pilot instructions](../../../docs/company-brain-pilot.md).

Generated prose, selected status and completeness require semantic review. Unknown is scoped to selected statements; dates do not resolve conflicts. Native qualification grades the visible draft itself against frozen criteria, separately from source integrity and usability. The model cannot receive correctness credit for a condition appearing only in hidden evidence. No default promotion or company access-control readiness is implied.

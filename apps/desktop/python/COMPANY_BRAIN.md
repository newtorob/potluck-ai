# Local Company Brain evidence and review

This local-owner developer feature connects scoped Markdown source collections,
reviewed knowledge and cited lookup to a saved investigation in the desktop app.
The investigation explicitly uses an already loaded local model. Source/review
commands do not invoke a model. No tool execution, training, remote inference or
automatic knowledge approval is part of this workflow. Shared company deployment
still requires authenticated server-side authorization and operational work.

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

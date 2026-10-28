# Local Company Brain evidence and review

This developer feature connects scoped Markdown source collections to explicit
human-reviewed knowledge and read-only MCP evidence lookup. It is a local-owner
foundation, not the completed investigation interface or a multi-user service.
No inference, embeddings, training, production actions, or external transfers are
performed by these services.

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

Next: review an actual source manifest, freeze an independent evaluation holdout,
then build the cited engineering investigation brief and saved handoff on an
explicitly selected owned model. Add actual-outcome review and evaluation gates
before claiming that retrieval improves model quality or training any model.

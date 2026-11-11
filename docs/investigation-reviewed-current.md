# Reviewed-current evidence selection

The investigation service has an experimental, internal opt-in:
`create_investigation(..., template_version='investigation-v21', evidence_selection='reviewed-current-v1')`.
Routes, UI choices and defaults are unchanged. This mode answers from matching approved knowledge in the explicit workspace/collection scope. It does not make source ingestion, review or policy approval automatic.

Selection uses the existing lexical question terms against approved claim content and keys. It retains all matching in-scope current claims and their exact supporting indexed chunks within bounded limits. A replacement becomes current only through the existing explicit supersession operation; document dates never establish authority. Proposed, superseded and deleted claims are excluded. If no current matching approval exists, generation stops rather than falling back to raw source text.

The prompt contains the exact approved claim text and complete supporting chunks as separate, linked passages. Only the claim carries recorded review status. Supporting text is not independently approved, and inconsistent claims/support or incompatible approved claims must remain unresolved. Review metadata is provenance, not proof of entailment or model correctness. Lexical matching can miss differently worded rules; this is not exhaustive semantic conflict detection.

The selector rejects more than 100 candidate matching approvals, records above 6,000 characters, or more than 24,000 presented characters. Paragraph capacity and measured input capacity must retain every selected claim and support passage. Overflow fails explicitly. Measured answer-budget checks precede answer generation; an optional automatic operation-planning call can occur first. The qualification fixture supplies operation classification. Sources must be ready, recent, in scope and bound to their indexed revisions. This mode uses indexed snapshots; it does not monitor unscanned filesystem edits.

Before saving a generated draft, the exact selection is replayed inside the write transaction. Concurrent matching approvals, supersession, deletion or source changes reject the pending result. Existing source/claim binding checks still protect saved investigations. Stored drafts describe their creation-time selection; they are not a promise that the entire organization has no later conflicting knowledge. Every new answer recomputes current selection.

Reviewed claim snippets use `reviewed-paragraph-v1`. Stored correction and verification reconstruct that same catalog and preserve supporting source boundary metadata. Legacy `paragraph-v3` requests keep their previous representation.

Qualification uses independently frozen synthetic lifecycle fixtures and unchanged captured regressions. Valid JSON, recorded review and a correct citation shape do not count as a semantically correct answer. See the dated benchmark packet for measured outcomes and limitations.

For the separate internal experiment that discovers approved claims through their complete supporting documents, see [reviewed collection discovery](investigation-reviewed-collection.md).

The separate [explicit question-parts foundation](investigation-question-parts.md)
removes model-generated request boundaries from an offline selection contract.
It is not integrated into this live path and does not establish automatic
question coverage, lifecycle authority or native answer quality.

# Reviewed knowledge discovered through source documents

The investigation service exposes an internal experiment:
`create_investigation(..., template_version='investigation-v21', evidence_selection='reviewed-collection-v1')`.
The default route, UI and earlier `reviewed-current-v1` mode are unchanged.

The selector searches complete indexed documents in an explicit collection scope using lexical question terms. It selects current approved claims whose own text matches, or whose cited document contains a matching passage. A match elsewhere in a supporting document can therefore recover a differently worded claim. It does not approve discovered source text. Only selected approved claims and their complete bound support enter the answer prompt; discovered chunks remain provenance.

The same selected collection and approved inventory apply to every question. Document dates do not establish authority. Superseded, proposed and deleted claims are ineligible. An empty approved scope stops before completion inference, without a raw-document fallback. This is not proof that an answer is absent from the organization: lexical discovery can miss relevant documents or include unrelated approved claims. A recorded approval is not proof of entailment.

Enumeration fails closed above 1,000 scoped documents, 200 substring-prefiltered discovery candidates or 100 in-scope approved claims. These caps apply before final relevance filtering, so common terms can reject a request conservatively. Every in-scope approved claim's support is resolved, including claims later excluded, so invalid support can block the request. Full selected support must fit the existing record, paragraph and measured input budgets. No top-K selection silently hides conflicting approvals.

The receipt binds all selected-scope document identities, matching discovery chunks and the complete in-scope approved set. Before a draft is saved, that receipt is reconstructed in the write transaction. Indexed source changes or approval lifecycle changes invalidate the pending result. This operates on recent indexed snapshots; it does not watch unscanned filesystem edits. Discovery identities and its lexical limitation are saved separately from reviewed authority provenance.

Qualification keeps retrieval inclusion, delivered answer correctness and the separate empty-approval guard distinct. Historical OfferLume source snapshots and synthetic test approvals are not claims about deployed behavior or Rob's approval. Earlier consumed regressions remain immutable, and any new native run uses a separately frozen corpus, questions and rubric. See the dated packet under `bench/results/` for measured results and limits.

## Versioned correction experiment

`evidence_selection='reviewed-collection-v2'` filters grammatical question terms before invoking the same transaction-bound discovery/authority selector. The receipt binds the original query, effective terms and underlying selection snapshot. It does not establish semantic relevance or truncate approved candidates; broad document matches can still include unrelated approved claims.

`presentation_mode='cited-components-v3'` uses separate required supported, unknown and conflicting arrays. Supported groups contain conditions, exceptions and conclusion; unknown groups contain distinctions and the missing-fact reason; conflicting groups contain a bounded explanation of the opposing statements. Every group retains selected source IDs. Wrong-status fields, invalid references, excess total groups and invalid text fail closed. The three buckets share the original total group limit; they do not triple it. Conflicting prose is explicitly unverified and does not select a winner by date. Structural validity does not establish that the explanation is correct or complete.

The saved original binds these fields to citation records and supports exact replay through draft verification and correction. Earlier presentation and selection versions remain unchanged. Both new modes are opt-in internal experiments, with qualification recorded separately from owner acceptance or default promotion.

`presentation_mode='cited-components-v4'` is a separately versioned correction candidate. Each primary statement and component supplies its own source references; the group selection is their deterministic first-occurrence union. The primary references are retained in provenance and the displayed original, so changing them is detectable even when the union remains the same. Source validity and per-group/overall passage bounds still fail closed without truncation. The candidate uses the existing v2 composition allowance of900 characters for supported answers and300 for unknown explanations, with160 per text field. This changes no earlier mode or score. Instructions distinguish a missing requested fact from a supported statement about its absence, preserve explicit conflict and require standalone material conditions; semantic correctness still requires the frozen evaluation.

### Exact component clauses (internal opt-in)

`cited-components-v5` preserves complete primary statements and allows bounded, exact plain-text clauses for conditions, exceptions and distinctions. Field limits, citation unions and original-source replay remain strict. Clause separators are deterministic presentation; no missing punctuation is inserted into stored components. Supported composition remains 900 characters; unknown composition remains 300. Versions 1–4 retain their prior behavior. Qualification is separate from structural validation.

### Reviewed fact priority and supported primary capacity (internal opt-in)

`cited-components-v6` allows a supported primary statement up to300characters within the existing900character composition limit. Unknown/conflicting primaries and all components remain160characters; unknown composition remains300. The runtime schema, instruction and per-status prompt budget agree. It requests complete relevant reviewed-claim facts before supporting-source background. Exact safe primary text is preserved through saving, correction and verification; earlier modes/defaults are unchanged. This is a qualification hypothesis, not evidence that omissions are resolved.

### Exact reviewed-claim selection (internal opt-in)

`reviewed-claim-selection-v1` is limited to v21 reviewed-current/collection evidence and one supplied factual operation. The model sees complete approved claim texts and selects claim IDs plus supported/unknown/conflicting status; it does not generate factual prose or citation IDs. Main answer text preserves each selected reviewed claim verbatim with deterministic claim/support bindings. Supporting excerpts are separately labeled and are not part of the model input or counted as answer-content coverage. Unselected/empty status is explicitly scoped and does not prove collection-wide absence. Model relevance/status remain unverified.

The input cap is16selected candidate claims (lower than the100claim scope inventory); final display allows16claim/support passages and8,000characters including framing. Whole claims, support and metadata are reconstructed on original replay; the save transaction rechecks the current authority receipt. Overflow, missing support, duplicate/foreign IDs and malformed responses reject without trimming. Owner correction preserves the original. Automatic model verification is explicitly unsupported after original-source validation; formulas and automatic operation planning remain outside this path. Earlier modes/defaults remain unchanged. Qualification uses unchanged consumed criteria and fresh frozen controls separately.

### Bounded complete-claim retrieval (internal opt-in)

`evidence_selection='reviewed-collection-v3'` ranks complete current approved claims using question terms found in each claim and its own bound source passages and headings. It filters grammatical words, weights less common terms more strongly, uses deterministic ties, and selects a prefix of at most six claims and 6,000 claim characters. It does not expand a match to every approved claim citing the same document. The existing scope, source freshness, support, 100-claim inventory and transaction recheck safeguards remain.

Claims are never cut to fit. A first claim that cannot fit rejects the request; a later claim that cannot fit stops the prefix instead of skipping to a smaller one. The receipt records the full eligible inventory, ranking, selected and omitted IDs, limits and bound source snapshots. The saved draft explicitly discloses how many matching claims were omitted and that lexical retrieval cannot prove whole-collection absence. Relevant claims, including another side of a conflict, may still rank outside the selected prefix; semantic recall requires measurement.

The selector remains an opt-in experiment. Its retrieval inclusion, exact cited answer correctness and historical-source authority are assessed separately. Six complete historical OfferLume documents and synthetic test review decisions do not establish current business policy, owner approval or production readiness. No earlier selector or default is changed.

## Opt-in bounded lexical V4 and compact reviewed-claim presentation V2

`reviewed-collection-v4` retains current approved, in-scope, revision-bound evidence and the V3 ranked-prefix budgets; its general lexical matching additionally normalizes conservative word forms. Its discovery trace matches normalized query terms against raw source whole words; source-side normalization applies to claim ranking, not to that discovery trace. Selection remains incomplete and unverified, and the saved retrieval notice discloses omitted matching claims and prohibits inferring collection-wide absence. It does not silently promote discovered source text to reviewed authority.

`reviewed-claim-selection-v2` is a separate opt-in presentation for complete selected claims. It shows the full claim text with claim citation snippets under the existing bounded display limit, while keeping every bound source in the investigation evidence and a deterministic source citation/hash/snippet manifest in the replay trace. Full supporting source text remains available under source provenance. The main answer does not repeat support as if it were independently approved. Source/claim binding and current review state remain subject to save and original replay validation; automatic model verification is unsupported. Selection, status and entailment remain unverified and require primary review. Existing selectors, presentation V1 and defaults remain unchanged.


## Opt-in reviewed collection V5

`reviewed-collection-v5` ranks the same current, approved and in-scope whole claims under the V4 limits. Direct token contributions are normalized by the cube root of distinct claim tokens; bounded evidence vocabulary can expand ASCII words ending in `er` to an observed base while retaining the original term. This suffix heuristic is not guaranteed comparative morphology or semantic equivalence. Trace records discovery terms, ranking terms, expansion pairs, ranking method and omitted matches. Source and claim text are unchanged. Lexical selection cannot establish corpus-wide absence and cannot guarantee coverage of relevant or conflicting claims; the saved notice and primary review remain required. V4 and defaults are unchanged.

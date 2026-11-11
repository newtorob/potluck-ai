# Bounded Company Brain evidence experiment

`reliability_mode="bounded-evidence-v1"` is an internal opt-in on
`investigation-v21`, with `compact-calculation-v1` and the pinned
`qwen3.8-27b-native-non-thinking` profile. Public defaults are unchanged.

Before generation, the serving native engine renders and tokenizes the whole
request, including instructions, source passages, quantities, notices and answer
metadata. The experiment reserves 2,000 output tokens inside the loaded context.
Up to eight tokenization probes retain a prefix of retrieval-ranked whole
passages. It never clips a table or sentence to fit. Omitted IDs are disclosed in
the request, saved provenance, retrieval warnings and Markdown export. Full
retrieved evidence remains available for review. Selection is capacity-driven;
it does not establish that the remaining passages fully answer the question.
The saved full catalog is bounded at 1,000 quantities; the model schema is limited
to 100 after whole-passage selection. An oversized fixed request fails without
saving a partial investigation.

Measurement holds runtime admission and inference gates, validates model/profile
identity and drains its native worker on cancellation. Generation checks the
context again and enforces the input limit against its own prepared token IDs.
There is one generation call and the existing shared 240-second deadline. No
larger context, extra generation retry or new model is introduced.

The opt-in selects quantity parser v5. It adds explicit token/second and
request/second aliases, preserving exact source spans and applying Decimal
arithmetic only to compatible units. Unsupported rates, ranges, approximations,
partial numbers and compound denominators remain unavailable. No rate conversion
is implied. Versions v1–v4 and existing saved records retain their contracts.

Unknown factual answers repeat the unanswered request and explain that the
selected evidence did not establish it. This remains an unverified model
assessment, not proof that the fact is absent everywhere.

Ordered-list v2 records whether the next scanned chunk belongs to a different
section (or the document ends). Only a complete, consecutive list with verified
section termination can suppress the end-of-chunk warning. A missing prefix,
continuation, nested ambiguity or truncated evidence still warns. Corrections
recheck boundary metadata against the same source revision. Source prose is
never rewritten to make a warning disappear.

The four November 4 fresh-document cases are now consumed regression cases.
Their original score is retained. Any new-document qualification is reported
separately; passing a small panel does not promote this experiment to a default.


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

### Source context and negative-fact candidate

`investigation_evidence_workflow.propose(..., version="evidence-workflow-v3")`
selects the separately versioned coverage v3 contract. It adds guidance for
explicit negative/unfinished facts, compound questions and procedural timing.
The v2 comparison scope guard and the default v1 path remain unchanged. Prompt
instructions do not establish that a model understood the source.

For selected evidence, v3 retains governing ATX Markdown heading units from the
same source, including parent sections. It ignores headings inside fenced code
and stops at sibling section boundaries. This is a structural binding, not an
interpretation of the heading. Only exact isolated heading units are promoted;
if an unretained governing heading shares a unit with unrelated prose, the
proposal is rejected rather than promoting that prose. Setext/HTML headings
are not parsed. Existing exact citation/list-introduction bindings remain.
`structural_context_units` identifies mechanically retained headings; original
model classifications and unverified review status are preserved. No absent
fact is automatically changed into a negative verdict.

The correction and compact review packets remain experimental. Current and
future consumed regressions, separately frozen fresh cases and actual owner
review are separate gates; neither this contract nor exact source offsets
certifies factual correctness. No ordinary gateway or saved default changes.

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

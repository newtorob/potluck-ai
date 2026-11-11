# Question-focused reviewed answers (V74)

This explicit experiment uses reviewed-collection-v5 with reviewed-claim-selection-v3. The default pilot remains V2. The native model returns status, whole passage unit IDs and a draft of at most 1,800 characters in one call. The answer contract is frozen independently of each diagnostic batch.

The server keeps exact offsets and citation bindings. Whole top-level list items include nested content; code and tables remain whole. Unlabelled prose binds bidirectionally to its section, and nested Markdown headings inherit governing ancestor context. This structural closure is conservative and does not prove semantic completeness. An out-of-budget selection fails rather than clipping source text.

The saved brief contains the generated answer, exact selected passages, full reviewed-claim evidence, raw response and selection trace. Reads reconstruct the original from current bound evidence. Tampered presentations, raw replies or source identities fail validation. Corrections use the existing source-bound review contract and retain the original draft; automatic verification is unsupported.

Unknown and conflicting leads are scoped to selected reviewed statements. They do not prove corpus-wide absence or establish a source's authority. Supporting source excerpts remain inspectable but are not independent model-prompt inputs. The model can still omit a condition, invent a fact or choose the wrong status.

Qualification therefore grades answer.text alone against frozen required/forbidden factual criteria, then assesses readability separately. Exact excerpts and valid JSON cannot rescue an incomplete draft. The V74 evaluation keeps all 19 consumed V73 criteria unchanged and freezes four additional questions after candidate freeze on the same six already inspected historical documents. This is not an unseen-company evaluation.

See [local pilot](company-brain-pilot.md) for the user workflow. No default promotion, authenticated company roles or production readiness is implied.

## Measured correction (V75)

V74's generated draft failed its frozen factual gate:13/23versus22/23for the longer reference. The original source data and all failures remain archived. V4 changes schema order to unit IDs, status, then answer, adds general fidelity rules, and conservatively retains preceding consecutive numbered steps. A missing requested subpart requires unknown even if another part is answered. The explicit focused pilot now selects this newer experiment; the default pilot remains V2.

V75 also failed qualification: 15/23 consumed and 2/2 fresh factual passes; editorial clarity passed 21/25. One reply was rejected because whole passages crossed more paragraph snippets than the claim-count limit. The renderer now retains those exact references within the existing complete 100-snippet catalog. The 16-claim and 16-selected-unit limits, text budgets and source checks remain unchanged. Reviewer corrections retain their separate 16-reference limit and require an explicit sufficient selection; references are never silently dropped. The recorded native failure remains a failure. Offline replay and primary-reviewed corrections do not qualify autonomous drafting or constitute owner acceptance.

Markdown handoffs include the effective reviewed correction and preserve the original draft. Measured reviewed-claim input reports claim IDs and the visibility of supporting excerpts; passage-selection budgets retain their own fields. These are distinct input contracts.

Qualification is pending. The23V74questions/rubrics remain consumed and unchanged; two further controls are authored only after the revised candidate freeze. The candidate-only diagnostic allows25answer+2readiness calls, same installed model/resources/token caps, no retries. V74results are historical captured reference data; this is not a newly paired causal experiment. No stronger correctness claim follows from the schema or added context.

## Complete-rule source answers (V76, experimental)

The internal V5 mode removes model-written factual prose. The model selects complete rule unit IDs, a status and an exact request substring naming a missing detail when the status is unknown. The server retains the same V4 unit boundaries and context/procedure dependencies, then places the complete selected source units in the visible answer. A single orphan table row displays its first cell followed by the remaining complete cells, without invented column names; original text and citations remain inspectable. Multirow tables remain exact. Unknown wording covers only the selected reviewed statements; conflicting wording establishes no authoritative winner.

This changes the answer contract to source-based question answering. It does not prove that the model selected every relevant rule, interpreted a status correctly, or produced a useful focused answer. All claim, selected-unit, citation and visible-text limits remain enforced without source clipping. Saves, reads and corrections retain replay bindings and the original source-rendered answer. Automatic semantic verification remains unsupported. The default V2 and opt-in focused V4 pilot routes are unchanged while V5 is evaluated directly.

The next diagnostic preserves all 25 V75 questions and factual rubrics, freezes the candidate before two new questions, and grades visible answer text alone. It requires 25/25 consumed factual passes and both fresh passes, with a separate assessment against the original editorial targets and later clarity checks. Mixed-topic source dumps do not qualify. V75's 17/25 native result and primary corrections retain their original status; manual changes and repeated generations receive no new native pass credit. The bounded candidate-only batch permits 27 answer calls, two readiness calls and no retries. One separate owned-local Potluck CLI review may use one additional call, without readiness calls. This remains a historical six-document test with synthetic review authority, not unseen-company qualification or owner acceptance.

## Discriminated verdict protocol (V77, experimental)

V76 failed: 15/25 consumed factual passes, neither fresh control, and 8/27 editorial passes. Twelve supported replies populated a field reserved for unknown and were correctly rejected. All 15 delivered answers met their factual criteria; this does not recover rejected replies or qualify source-answer relevance. Its raw outputs, failures and prior versions remain preserved.

V6 replaces independent status and missing-detail fields with one discriminated verdict string: exactly `supported`, exactly `conflicting`, or `unknown: ` followed by a nonempty exact request substring. Extra old fields and malformed verdicts are rejected. This removes contradictory field combinations; the bounded string schema and runtime admission do not prove verdict correctness. Strict postvalidation and all source-unit, dependency, citation, raw replay and output budgets remain. There is no ordinary runtime/gateway schema-admission change. The default V2 and focused V4 routes remain unchanged.

All 27 previous questions and rubrics are consumed and unchanged; two new controls follow candidate freeze. Two native readiness probes exercise known and missing-detail protocol forms before 29 answer calls, with no retries or resource increase. The separate CLI review uses a compact exact source packet and an offline context-preparation check before model loading; its prior full-module packet failed the context check with zero inference calls. Factual, editorial, source/authority integrity and agent-proposal results remain separate.

## Coverage and verdict review (V78, experimental)

V77 delivered 23/25 original factual passes, 25/27 consumed and 1/2 fresh; editorial clarity passed 13/29. All 29 replies met the structural contract. An unresolved cadence conflict was labeled supported, an explicitly stated width explanation was labeled unknown, and a fresh preview answer omitted required context. The negative result and raw replies remain preserved. The compact CLI review completed one call but returned insufficient information for its semantic test-design assignment; this is not a completed test proposal.

The opt-in V7 pipeline uses two planned model calls. The first selects complete source unit IDs. The second receives the full same catalog and retained first selection, adds missing units and supplies a verdict. It cannot discard first-stage units or their dependencies. The server validates both replies, closes the union under dependencies, and uses the unchanged V6 source renderer. Pipeline V7 and catalog/renderer V6 are recorded separately. The aggregate renderer input is deterministic bookkeeping, not a third model response or a human correction.

Both actual prompts are measured without clipping: the first reserves 2,000 output tokens, the review reserves 512, and both must fit the same context capacity. Calls share the original deadline and model identity checks. Invalid replies, overflow, source changes and expired deadlines fail without retry or a saved partial answer. Raw replies, prompt/schema hashes, measured budgets and retained/final IDs are replayed on reads and corrections. Relevance, coverage and verdict correctness remain unverified; automatic semantic verification is unsupported.

All 29 V77 questions and rubrics stay unchanged and consumed, with two fresh controls authored after candidate freeze. The bounded diagnostic allows 62 answer calls plus two readiness calls on one synthetic conflict fixture, no retries or resource increase. One separate compact owned-local CLI task may propose structural unknown-quote admission tests using concrete fixtures and the implemented guard. Generated proposals require primary review and do not count as executed tests. Historical V77 results are not a new paired baseline. Factual and editorial gates remain separate; mixed-topic source dumps do not qualify. Default V2 and focused V4 routes remain unchanged.

## One-key verdict variants and independent coverage (V79, experimental)

V78 remains unqualified: 22/25 original, 25/29 consumed and 2/2 fresh factual passes, with 10/31 editorial passes. Three reviewers returned an invalid `supported: explanation` string; the grace case also overlooked the requested exact duration. The preview answer still omitted required named usage. All failures and raw replies remain preserved. The separate one-call Potluck CLI task produced two structural guard-test proposals, mostly restating supplied fixtures; no generated tests were executed.

V8 changes the review verdict to an object with exactly one key: `{"supported": null}`, `{"conflicting": null}`, or `{"unknown": "exact operation-request substring"}`. The admitted schema permits optional keys; strict decoding rejects empty or multiple keys, duplicates, non-null known values, and blank, overlong or non-request unknown quotes. It never strips an explanation or ignores an incompatible field. Known verdicts have no free-text value. Runtime schema admission alone does not establish native grammar adherence or factual correctness.

The review independently selects its complete relevant set as `review_unit_ids` from the full same catalog. The server unions it with the retained first selection and closes dependencies without clipping. Instructions require every requested subpart, distinguish a condition or nearby value from a distinct requested quantity, and ask for explicit source usage when a question concerns how shared behavior is applied. They contain no case-specific facts or rubric. The unchanged V6 catalog/renderer, source authority and limits remain; pipeline V8 and any reused subprotocol versions are explicit. Both planned calls retain their exact raw replies, measured 2,000/512 budgets, same capacity and shared deadline. Automatic semantic verification remains unsupported.

The next diagnostic keeps all 31 V78 questions and rubrics consumed and unchanged, then authors two controls after candidate freeze. It allows at most 66 answer calls plus two readiness calls and one separate bounded CLI review, with no retries or resource increase. Visible factual and editorial criteria remain separate. Historical results are not a newly paired baseline, and the default V2 and focused V4 routes remain unchanged.

## Closed verdict and conditional missing-detail scope (V80, experimental)

V79 failed its two-call synthetic readiness check before any of its 33 answer cases ran: the review returned both `conflicting` and `unknown`. Strict decoding rejected the reply. Those raw replies and cleanup receipts remain archived; V79 has no answer score. V78 remains the latest completed factual and editorial result.

The V9 pipeline returns a single closed status enum in its coverage review, alongside `review_unit_ids`. Only a validated `unknown` status triggers a distinct third call to identify the missing detail as an exact nonempty operation-request substring. Supported and conflicting answers use two calls and contain no scope reply or scope-input provenance. An unknown answer uses three calls; a malformed scope rejects the entire answer. The scope stage cannot change the verdict, source selection or facts. These are planned stages, with no generation retries or partial saves.

All actual prompts are measured against the same context capacity and shared deadline. Selection reserves 2,000 output tokens; coverage review and conditional scope each reserve 512. Reads replay the exact two or three raw replies, prompt/schema hashes, measured budgets and source bindings. The unchanged V6 catalog and renderer retain complete source units and dependencies. This does not prove semantic coverage, correct classification or useful editorial focus. Automatic semantic verification remains unsupported.

The diagnostic carries all 33 V79 frozen questions and rubrics unchanged, including its two authored but unexposed controls; those controls are not fresh V80 cases. Two further controls follow candidate freeze, for 35 total cases. The bounded run allows at most 105 answer calls and two declared readiness probes, plus one separate compact owned-local CLI review after completed-answer integrity and cleanup. The readiness probes separately test review and scope schemas using explicitly authored intermediate states; they do not qualify the full pipeline or supply answer pass credit. Same installed model, sampler and resource limits apply. Factual and editorial gates remain separate, and the default V2 and focused V4 routes remain unchanged.

## Request spans and explicit helper-use context (V81, experimental)

V80 reached 23/25 original and 29/33 carried plus 2/2 fresh factual passes, with 12/35 editorial passes. Its four factual failures remain archived: an empty missing-detail quote was rejected, explicitly deferred features were mislabeled unknown, named preview consumers were omitted despite being in the catalog, and a broad unknown lead called a stated token-opening action unspecified. Source replay and valid structures did not rescue those answers.

V10 keeps two selection/review calls and a third call only for unknown answers. The scope call selects contiguous, ordered IDs from exact non-whitespace spans of the operation request. The server reconstructs the original substring, including punctuation and intervening whitespace; it does not present that text as model-written prose. Empty selections express insufficiency and reject the whole answer. Duplicate, foreign, reversed or noncontiguous IDs, requests over 128 tokens, and reconstructed scopes over 400 characters fail without fallback or retry. The tokenizer version, offsets, token digest, raw reply and measured prompt/schema bindings are replayed. Valid IDs still cannot prove that the chosen detail is missing or complete.

A separate, narrow context policy links a prose unit that explicitly introduces a helper/function/method signature to units in the same approved claim that explicitly use that exact, case-sensitive identifier. Shared names alone, other claims and ambiguous definitions do not establish a link. Units containing code fences or beginning with quote markers are skipped. Derived directed edges retain source offsets, enter the model-visible input and composite pipeline digest, and close alongside existing structural dependencies. Existing selection and text caps reject overflow without clipping. This preserves exact source context; it does not establish current implementation or semantic completeness, and can still retain too much material. V6 unit boundaries and source renderer remain unchanged, with the new context policy recorded separately.

Review instructions now distinguish stated negative, denied, excluded or deferred outcomes from absent facts. That is general guidance, with no case names or rubric facts, and still requires native evaluation.

Read-validation clarification: earlier descriptions overstated ordinary getter replay. V80's offline audit, review and verification paths reconstructed originals; ordinary reads checked current source bindings. V10 adds original replay before saving and on reads, including reads used by export. Tests alter persisted graph edges, policy, raw replies, hashes, mode and displayed text, and verify rejection. A pre-save validation failure leaves no partial record. Older mode behavior remains unchanged. This is structural replay, not automatic semantic verification or a claim that every local database modification can be detected.

All 35 V80 questions and rubrics remain unchanged and carried. Two new controls follow candidate freeze, for 37 cases, at most 111 answer calls plus two declared authored-state readiness probes and one separate bounded CLI review. The same model, sampler, resources, shared deadline and watchdog apply. Factual correctness, editorial focus and broader company qualification remain separate; no default promotion or owner acceptance is implied.


## Replaceable proposal and complete final selection (V82, experimental)

V81 remains unqualified:23/25 original and33/35 carried plus2/2fresh factual passes,13/37editorial. Its first proposal for an order-branches question expanded to17units and rejected before planned review could remove unrelated material. Its incentive answer repeated an unqualified legal marketing guarantee; required incentive conditions did not rescue that misleading extra. All earlier scores and raw failures remain preserved.

V11 treats the first selection as a replaceable proposal. Raw IDs still obey the16-ID schema and strict decoder. Expanded proposal context can exceed the final display limit only as metadata drawn from the existing validated full catalog. The complete actual review prompt is measured against the same model capacity and shared deadline before inference. Proposal context is never rendered or saved as an accepted answer. Review sees the full catalog, raw proposal IDs and expanded proposal context and chooses its own minimal complete final ID set. It can add or remove proposal units. There is no forced union, clipping, source rewriting or inference retry.

All final structural prerequisites, exceptions and helper-use context close before rendering under the unchanged16-unit and8,000-character display limits. Overflow rejects the whole answer. The trace separately records raw proposal IDs, expanded context, dependency-only context, final raw and closed IDs, dropped raw proposals and dropped dependency-only units, plus a versioned proposal policy. Exact raw transcripts, prompts, schemas, budgets and policy are replayed before save and on reads/export. Unknown request-token selection uses the final reviewed state. Automatic semantic verification remains unsupported: valid closure cannot establish relevance or that a dropped independent rule was unnecessary.

The native diagnostic preserves all37V81questions and rubrics unchanged, then authors2controls only after candidate freeze, for39cases. Bounds are117answer calls plus2declared synthetic authored-state readiness probes, with at most1separate owned-local CLI call after completed main integrity and cleanup. Model, sampler, resource caps, 2,000/512/512 token allocations, concurrency, shared deadline and watchdog stay the same. Readiness and fixture-derived CLI proposals are not answer-quality or executed-test credit. Defaults V2/focusedV4 and ordinary gateway remain unchanged.


## Missing-detail boundaries in every review (V83, experimental)

V82 remains unqualified:23/25original,34/37carried plus2/2fresh factual passes,14/39editorial. Both reviewers agree on37carried factual judgments. Its prior legal-guarantee and branch-cap failures pass, but clock absence drops known deadline context, grace returns supported without naming an unstated duration, and token lifetime selects a disjoint missing-token list and is rejected. All old outcomes remain archived unchanged.

V12 keeps replaceable first proposals and uses two planned calls. Requests over128non-whitespace tokens reject before the first prompt measurement or inference. Every final review now returns complete final source IDs, missing_start_token_id, missing_end_token_id and a boolean conflicting flag. Both boundaries empty means no missing detail. Otherwise both must be valid ordered request-token IDs defining an exact contiguous original substring, including punctuation and intervening whitespace, at most400characters. One empty boundary, foreign/reversed IDs, wrong types, duplicate or extra JSON keys and conflict-plus-missing reject. The server derives unknown from valid nonempty boundaries with conflicting false, conflicting from empty boundaries with conflicting true, and supported from empty boundaries with conflicting false. These are declared new contract semantics, not repaired old statuses.

Exact raw first/review replies, token offsets/digest, tokenizer and boundary policy, source context, proposal/final/drop IDs and measured prompt/schema hashes are replay-bound. No third model call, scope_raw or synthetic scope-input record exists. Review instructions retain every known requested fact and applicable governing rule when another detail is missing, including when a request asks for unavailable precision. Nearby values cannot establish a different requested quantity. A fully unsupported request may legitimately select no applicable units; the decoder does not force an unrelated citation. A valid wrong span, false empty boundary pair or omitted known context remains a semantic failure for answer review. Conflict-plus-missing is explicitly unsupported rather than silently prioritized.

All final structural/helper dependencies and16unit/8,000character caps remain unchanged; no forced union, clipping, source rewriting or retries. Source facts still use the unchangedV6renderer. V11andolder modes/defaults are preserved. V12original replay runs before save and on reads/export; correction and source-revocation checks remain enforced. Automatic semantic verification is unsupported.

The diagnostic carries all39V82questions/rubrics unchanged, then authors2controls after candidate freeze, for41cases. Two calls reserve2,000/512output tokens under the same measured capacity and shared deadline. At most82answer calls plus2synthetic authored-first-state review-readiness probes, then at most1separate actualCLI call after main integrity/cleanup:85aggregate maximum. Same installed owned model, sampler/resources, concurrency and watchdog. Readiness is not answer-quality credit and CLI proposals are not executed tests. Broader qualification, actual owner acceptance and default promotion remain separate.


## Concise drafts with retained reviewed evidence (experimental)

The internal opt-in `reviewed-claim-selection-v13` path adds a concise, unverified draft to the existing planned review call. It preserves the missing-request boundary and conflict contract, the two-call limits of 2,000 and 512 output tokens, measured context capacity, shared deadline, and complete source dependency closure. The draft is bounded to 1,800 characters. It is not an automatic verification result.

The primary answer shows the draft and a deterministic status lead. For unknown answers, the lead quotes the exact missing question fragment as a requested detail, while the draft must retain known requested facts and governing rules. Exact selected passages, their citations and full reviewed statements remain available separately for inspection. They cannot supply facts omitted from the draft during factual qualification. A draft requires selected support; a fully unsupported unknown may have an empty draft and no selected units.

Original reads, saves, corrections and exports replay the raw replies, draft, complete selected source context and provenance. Malformed or truncated output fails without a retry or hidden fallback. Older modes and defaults remain unchanged. Concise model prose can still omit a prerequisite, invent an assurance or misstate an exception; only semantic evaluation and human review can assess those errors. The new path remains unqualified until frozen factual and presentation controls are evaluated.


## Fallible proposal drafts before final review (experimental)

The internal `reviewed-claim-selection-v14` path asks the first call for source IDs and a short proposal draft. The second call receives that fallible draft, the expanded proposal context and the full source catalog, then independently chooses final source coverage, missing-request boundaries, conflict and final draft. The first draft is never rendered as the final answer and does not force citation retention. The reviewer may replace or discard it.

Both exact raw replies, decoded proposal text, final text, source closure, policies, prompt/schema digests and measured budgets are replay-bound. The limits remain two planned calls with 2,000/512 output tokens, a shared deadline and no generation retry. A proposal draft can anchor an omission or invented claim; this is a hypothesis to evaluate, not an automatic completeness check. Strict readiness and all factual/presentation gates remain required. The earlier V13 attempt failed readiness before any answer case ran; its failed raw reply and unevaluated controls are preserved. Defaults and older modes remain unchanged.

The V86 experiment revises only the V14 review prompt, adding an explicitly illustrative capacity/email example for keeping a known partial answer while identifying a separate absent detail. Example facts and IDs are not catalog evidence. V85 generated no native answer briefs; its failed readiness requests and exact source are archived. The revised source and message hashes must be frozen separately, and old requests must not be regenerated using the new prompt or treated as equivalent. This changes no schema, source-closure rule, status rule, budget or retry policy and does not establish model correctness.
# Experimental reviewed source plans (V15)

`reviewed-claim-selection-v15` is an explicit opt-in investigation presentation
mode. It selects exact source units from separately proposed and approved plans,
then independently reviews the selected IDs. It renders source bytes rather than
model-written factual prose. The first proposal is replaceable. Missing details
use exact request-token boundaries; conflicts establish no authority winner.

Plans cover the complete approved claim, including unselected topics. They bind
exact source text, source approval reviewer/time and current source references.
Declared dependencies retain context, but their meaning and completeness need
review. Valid hashes, ranges or JSON do not establish semantic correctness or an
authenticated owner identity. Without an approved plan, a current approved claim
remains whole. Unavailable sources never become whole-claim fallbacks.

The service captures a receipt for every claim before inference, including an
explicit null when no plan exists. It closes that transaction before model work.
The same exact receipt map is rechecked in save, read, review and outcome
transactions. Plan approval during generation, later supersession or source
revocation prevents reuse of the old output. An independent nullable storage
marker detects provenance-only presentation-mode downgrades. It does not protect
against an attacker with unrestricted database write access.

Limits: 16 claims, 16,000 characters per claim, 72 catalog units, 16 closed visible
units and 8,000 visible characters. Overflow rejects instead of clipping. Two
planned answer calls retain the existing 2,000/512 output limits and shared
deadline. Automatic draft verification remains unsupported for reviewed claim
selection. Complete original claims and bound sources remain inspectable.

V1–V14 modules and saved outputs retain their existing behavior. V15 is not a
default or a quality-qualified replacement. Curated plans change the source
preparation condition and add review work. Historical synthetic fixture approvals
are not current company policy, Rob's acceptance or production readiness.


### Opt-in request-fact review (V16)

`reviewed-claim-selection-v16` adds an explicit request-fact plan to both bounded
stages. The first proposes one to six ordered, nonoverlapping question-token
spans and source-unit IDs. The independent review may replace those spans and
IDs, then gives each fact a supported, unknown or conflicting verdict. It keeps
source framing separate from the requested answer without automatically stripping
dates or names. These are model decisions; a valid plan does not prove that all
requested facts are represented or correctly assessed.

Known negative or deferred outcomes remain supported facts. Unknown leads name
only the declared missing fact spans. Conflict leads name the conflicting spans
and state that no authoritative answer is established. A mixed answer preserves
both conflict and missing-detail leads. The aggregate status gives conflict
precedence, followed by unknown, then supported. Complete approved source units
are rendered once after closing their explicit dependencies; the final 16-unit
and 8,000-character limits and 2,000/512 generation budgets are unchanged.

V16 uses the same current source-plan receipts as V15 with its own persisted
mode marker. Save, read, review, export and outcome paths replay the original
fact decisions and recheck current receipts inside their existing transactions.
The two versions retain distinct storage markers and V15 saved outputs stay
replayable. Automatic answer verification remains unsupported. No default mode
or current-policy, owner-acceptance or production qualification is implied.

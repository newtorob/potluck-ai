# Bounded answer repair — October 30, 2026

Internal service-only v13. Public defaults and selectors remain unchanged. This checkpoint repairs contract failures; it does not establish company-pilot readiness.

## What changed

- A generated factual answer can contain up to four exact request spans. A well-shaped but invalid span proposal falls back to the entire original request; the proposed text remains diagnostic and unreviewed. Malformed JSON and invalid shapes still fail.
- Generated parts share a maximum 4,000-character aggregate including separators. Each part is capped at 1,000 characters (999 for four parts), with identical schema, prompt and validation limits. Corrections accept the bounded aggregate and preserve original machine metadata separately.
- Each generated part may cite at most 8 authentic snippets. The factual aggregate and factual corrections may cite up to 32 unique snippets; calculation corrections remain capped at 8. This final citation fix was made after the first fresh run and is assessed by exact recorded-response replay, not a new inference claim.
- V4 quantity extraction adds distinct calendar, business and working days, including supported shared-unit expressions. It does not convert or mix these units. Lexical negation/approximation/range guards are conservative, not general source entailment.
- Mixed requests carry explicit before/after calculation guidance. Difference, percentage and measurement scope are separate reviewed operations. The calculator replays selected inputs; it cannot establish that the model selected the right meaning.
- Scoped evidence, server-built excerpts, immutable questions, strict correction replay and original v11/v12 behavior remain protected.

## Retained development record

These six cases were already consumed. They are development regressions, not fresh generalization evidence. The original rubric was retained. Quartz now has three supplied operations instead of the old fixture's incomplete single operation, so attribution is not a matched-plan comparison with v12.

| Round | Saved drafts | Original complete-fact/citation rubric | Result |
|---|---:|---:|---|
| Prior v12 checkpoint | 2/6 | 0/6 | Historical result, separate retained run |
| Development 1 | 6/6 | 1/6 | Small divided text budgets cut off facts; paraphrased headings could invent claims |
| Development 2 | 6/6 | 4/6 | Larger budget improved coverage, but invented headings and prose defects remained |
| Development 3 | 0/6 completed | Not semantically scored | Harbor native grammar crash; five not run after process exit 139 |
| Development 4, frozen generation code | 6/6 | 3/6 | Safe exact headings and native limits; arithmetic corrected, semantic/prose gaps remain |

Final development passes are Harbor, Orchard and Maple. Orchard and Maple still end a repeated part mid-sentence. Lumen omits billing application details after its whole-request fallback hits the text cap. Cedar omits the employee-preview distinction. Quartz computes −5 calendar days and −25%, but does not explicitly state the full 20→15 comparison in prose and does not attach the required signed-agreement/cohort evidence to its factual measurement answer. Its exact spans `What`/`do`/`the`/`Quartz` demonstrate why lexical validation does not establish useful decomposition. Only Harbor passes the primary additional whole-draft usability screen; this screen is separate from the original frozen rubric.

The native crash was reproduced with the installed model's vocabulary. Schemas for 1–4 capped parts and mixed calculations initialize successfully. A larger sequential-regex workaround was rejected because the installed converter does not preserve decoded JSON string semantics and the regex boundary check backtracked excessively. No such workaround entered production code.

## Fresh qualification

The unchanged generation snapshot completed its four independent frozen cases: **0/4 complete-fact/citation passes, 1/4 saved drafts**. Cedar Lantern and Harbor Rivet failed validation because valid part-level citation lists produced aggregate unions of 15 and 11, exceeding the old aggregate limit of 8. Osprey Quay timed out at 240.874 seconds. Copper Wren saved a draft but reversed the signed difference (+1.25 instead of −1.25 business days); −25% was correct. It also omitted/incorrectly cited measurement details. No retries or production/prompt changes occurred during this frozen run.

The final v13 change aligns citation aggregation with 4 × 8 per-part capacity. It changes only `investigation_bounded_answers.py` among the 155 production Python files. The original generation snapshot, errors and code hashes remain immutable and distinct from the final replay snapshot.

### Post-holdout replay, no new inference

All eight captured request messages, schemas and settings match exactly. Source order, headings, anchors and content match after real isolated scanning/retrieval. Separate DBs reuse the frozen collection UUIDs so prompt-visible extraction-notice citation IDs remain identical; no evidence is forced or inserted into results. All three available final replies are returned unchanged. Osprey's absent final reply remains a recorded timeout, not a new timed measurement.

The repaired service saves 3/4 drafts; all three also pass unchanged-brief correction persistence and export. Those are scripted workflow checks, **not human review or owner acceptance**. Exact source/snippet/catalog/binding/calculation replay and canary checks pass for the three saved results; the timeout has only pre-result checks.

The same replies achieve **1/4 literal rubric passes after replay, 0/4 clean grounded usable answers**. Cedar has all listed required facts but overstates “no commitment” as “no plan.” Harbor omits the warehouse branch and evidence/uncertainty handling, cuts off a paragraph, misassigns closure ownership and fails to cite decisive closure evidence. Copper reverses subtraction and has incomplete/misattributed measurement details. Osprey remains a timeout. The primary and independent Terra reviewer agree.

The candidate-only original qualification has no fresh baseline. The repair/replay is post-holdout development and cannot be reported as a fresh quality uplift. A new unconsumed full-pipeline qualification is still required. Two replay harness setup failures are retained separately: missing fake-runtime profile method, then strict prompt identity rejection due fresh UUIDs. Neither invoked inference or changed the recorded replies.

## Conditions and limits

Installed Qwen3 8B Q4, SHA d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785, Apple M2 Pro, 16 GiB, CPU-only, 8 threads, 8,192 context, seed 7, qwen3-non-thinking profile. A 240-second deadline is shared by planning and answer generation; cancellation cleanup can extend wall time. Zero retries per round. Every attempted case, failure and excluded canary is retained. Model/data instances are isolated; the ordinary app is untouched.

Small synthetic cases and supplied main operation plans do not qualify automatic operation planning, a large corpus, real-company questions, a stronger model, or owner acceptance. The earlier two real-source timeouts and native 27B qualification remain unresolved. Source/citation integrity is assessed separately from meaning, completeness, uncertainty status and prose quality.

## Validation and disposition

Clean final snapshot: 234 focused tests pass. Full working-checkout `make test-sidecar` with the absolute engine directory on `PYTHONPATH`: 5347 passed, 5 skipped, 1 warning in 410.10s (0:06:50). `make test-fast`: 527 frontend, 1,228 CLI and 44 landing tests, typechecks and privacy lint. Both checks run on macOS. All 81,484 preexisting unrelated paths are preserved; generated frontend build-info is restored to its initial bytes. Two failed validation attempts are retained separately: the initial launcher lacked PYTHONPATH; the first full run passed 5,343 tests but found four new native-test subprocess failures caused by library-path isolation. Those tests now use the library actually loaded in the parent process, while preserving native sampler and exit-code assertions. A formatting-only final blank line in the new frozen decoder is retained to preserve evaluated source bytes. Final commit identity is checked against all 155 production Python files in the final replay snapshot; the original generation snapshot intentionally differs in the citation decoder and remains retained. Keep v13 internal; no public quality claim, push, deployment or X post.

## Next build boundary

Qualify the already installed 27B native runtime before the same-case comparison. The retained October 29 preflight on owned Linux `newtorob` failed because Python `llama_cpp` needed unavailable `libcudart.so.13`; the registered investigation profile also assumed the stock 8B formatter. Treat runtime/profile support as a separately tested change; do not bypass identity, schema, citation, or model-profile checks. No new native inference was attempted in this repair batch. Then address remaining coverage, direction, grounding and timeout failures and qualify on a new independent real-corpus holdout before promotion.

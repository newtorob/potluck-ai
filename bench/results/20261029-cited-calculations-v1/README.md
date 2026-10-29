# Cited calculation diagnostic, October 29, 2026

The internal `investigation-v8` adds operation-level results and deterministic
arithmetic from exact cited quantities. The model chooses the operation and input
IDs; server code computes differences, percentage-point differences, relative
percent changes and proportions with Decimal, explicit units and rounding. The
calculation trace remains separate from reviewer corrections and is included in
exports. This is an internal diagnostic: v4 remains the default and v6 the opt-in
preview. No release, push, website change or X announcement is part of this batch.

## Evaluation design

Eight independently authored cases were frozen before inference. The final
candidate was frozen before primary fixture inspection; implementers did not see
the new fixtures or outputs during development. One clean HEAD archive plus the
explicit overlays in `source-freeze.json` supplied all three arms. The runner's
Git head is null because the archive has no `.git`; its 145 production Python
hashes and base commit identify the evaluated source. Existing unrelated working
files were excluded from inference.

Arms are unchanged v6 seed 7, v8 seed 7 and v8 seed 29, run sequentially on the
same Apple M2 Pro with the installed Qwen3 8B Q4 model, CPU only, eight threads,
8,192 context and the verified Qwen3 non-thinking template. Temperature 0.7,
top-p 0.8, top-k 20, min-p 0, repeat penalty 1.0. V6 retains 800/1,600 planner/answer
token budgets; v8 uses 800/2,000. Both retain the 240-second soft generation
deadline. No repair, retry, inference download, remote call or paid CI occurred.
Eight unique cases and 24 attempts do not establish broad model quality or owner
acceptance. Different historical corpora are not directly comparable.

`run-plan.md` records the decision rules. `primary-assessment.json` and
`independent-assessment.json` retain separate assessments of coverage, semantic
operands, arithmetic, evidence precision and unsupported claims. Structural
validity is not semantic correctness. Reviewer differences are retained in the
comparison rather than erased. No tuning was performed after fresh output
inspection. The new set is now consumed and must not be called fresh in future
iterations.

## Evidence and development history

Raw requests, all model replies, retrieval, scope, fixture/rubric copies, metadata
and result/error records are in the adjacent `20261029-cited-calculations-*` run
directories. Isolated databases are intentionally excluded from Git; the source
and raw artifacts reconstruct what the model was shown. Development includes the
initial native grammar crash and invalid plans; see `development-outcomes.json`.
The final candidate was not rerun on development cases as a new success claim.

`code-review.md` records primary and independent static review and resolved
findings. `grammar-preflight.json` and the empty-catalog result record native
sampler initialization checks without inference. `candidate-patch.json` records
the scoped implementation against the base commit, including full new files.
`verify_artifacts.py` replays calculation traces, reconstructs quantity catalogs,
checks exact cited spans and excluded-canary absence, and compares source/model/
fixture identities. It does not judge semantic input selection or source authority.

## Known design limits

The planner can omit a subrequest or emit generic/duplicate request labels.
Deterministic arithmetic cannot recover a missing operation. The conservative
catalog intentionally omits unsupported units and spelled-out numbers; its
allowlist is not comprehensive company-data support. Model-chosen status,
operand ordering, comparable scope and authority still need review. Unknown is
correct for missing or conflicting evidence, but is a completeness failure when
the requested answer is actually available. A caller may supply an explicit
operation plan through the Python service; there is no public v8 UI/API switch.

## Results and decision

| Configuration | Primary | Independent | Drafts saved |
| --- | ---: | ---: | ---: |
| v6-s7 | 6/8 | 5/8 | 8/8 |
| v8-s7 | 3/8 | 3/8 | 7/8 |
| v8-s29 | 4/8 | 4/8 | 7/8 |

The only case-level reviewer disagreement is baseline `conflicting-limit`: the primary accepts the unknown status and alternative values/conditional changes as preserving unresolved authority; the independent reviewer requires an explicit statement and authority excerpt. Both assessments are retained. Both agree on all candidate case outcomes.

Across 24 attempts, 22 drafts were saved; both candidate multipart attempts failed plan validation. All 48 selected excerpts are exact allowed-source spans, all 14 saved candidate catalogs reconstruct exactly, and all eight computed traces replay exactly. No excluded canary appeared in a model request, retrieval, plan, answer or visible evidence. Exactness alone does not establish relevance or completeness.

**Do not promote v8.** Correct arithmetic did not produce a better complete workflow. Both seeds omitted the causal subrequest, omitted the known repair count from visible evidence, gave a faulty zero-baseline explanation and failed multipart planning. One seed additionally omitted the authority excerpt in the conflict answer. See `next-build.md` for the proposed follow-up.

## Repository verification

Final macOS checks: **5,173 sidecar tests passed, five skipped; 249 clean-snapshot focused tests passed; 521 frontend, 1,228 CLI and 44 landing tests passed; typechecks and privacy lint passed.** The full working-tree suites include preserved unrelated work; focused verification used a separate clean snapshot with all 145 frozen production hashes.

The first full sidecar run exposed an inherited native-library path in a new subprocess grammar test (5,172 passed, one failed, five skipped). Only test isolation changed; the full rerun passed. The first fast run failed one unchanged real-timer household recovery test, which passed 15/15 in isolation and in the full rerun. That timing sensitivity remains a limitation. All initial and final logs are retained, and no model evaluation was repeated or tuned after these test-only changes.

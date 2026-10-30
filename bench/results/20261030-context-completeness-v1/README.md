# Potluck context and completeness — October 30, 2026

The internal v11 candidate improves attached evidence on the fresh synthetic set, but it is **not ready for promotion**. Both the primary and independent Terra semantic reviewer agree on every case-level result.

| Measure | Frozen v10 | Frozen v11 |
| --- | ---: | ---: |
| Required answers with supporting excerpts | 4/6 | 5/6 |
| Usable saved drafts | 6/6 | 5/6 |
| Required factual text complete in returned answers | 6/6 | 5/6 |
| Median generation/validation time, including failures | 41.50 s | 45.83 s |

Harbor improves because the attached quotes now include the complete delay cause and calibration condition. Cedar, Mosaic, Atlas and Birch pass in both arms. Quill fails in both: v10 has incomplete approval evidence and an inappropriate supported label; v11 repeats the entire request span six times and fails validation, so no usable draft is saved. Its failed attempt takes 120.36 seconds. Total generation/validation time is 241.91 seconds for v10 and 337.88 seconds for v11. These are small-run observations, not a general speed claim.

Baseline required/forbidden rubric passes do not certify every extra sentence: Cedar and Mosaic add source-correct prose without all supporting sentences attached. Those concerns remain in both reviewers’ assessments, rather than being added to the frozen rubric after results.

## Separate consumed real-source diagnostics

Candidate only: **0/2 pass; one saved draft**. These were declared before candidate fresh inference, but the questions and failures were already known. They are development diagnostics on curated source excerpts, not new holdouts or an identical-prompt paired comparison.

- **Pilot readiness:** 148.92 seconds; cites the operational prerequisites but omits the actual local workflow, v9 qualification results and source/owner-review limitations. It repeats prerequisites under the “what is ready” part. Exact spans and citations validate, while semantic coverage still fails.
- **Core/company packaging:** fails before generation with `snippets do not match source context`. Deterministic replay identifies one skipped blank-line character immediately before a list in the “First offer to validate” section. Every individual quote is exact, but their concatenation no longer satisfies the quantity parser’s required source-prefix invariant. The error, replay and source windows are retained. No post-result code or prompt change, retry or relaxed validator was used.

The frozen real sources predate Rob’s later acceptance of the v10 draft in 62.215 seconds; that actual review record remains unchanged. It does not override the independent completeness failures.

## Implementation and validation

Terra agents implemented deterministic retrieval tie ordering with soft document diversity and backfill, bounded paragraph excerpts, relative source labels, and ordered factual answer parts with per-part excerpts. The primary and independent Terra code reviewer checked changes and resolved issues before freezing. Original machine parts remain separate through correction/export. Span validation does not establish semantic coverage. V4 stays default, v6 preview and explicit experimental v9 stay unchanged; v11 has no public UI/API switch or calculation-preview support.

The integrity audit verifies source sets match across all six fresh pairs (ordering differs for Quill), all 43 displayed fresh excerpts are exact, all 11 saved catalogs reconstruct, both calculated traces replay, and no excluded canary appears in retrieval, prompts, raw replies or saved briefs. The real readiness draft’s one displayed excerpt and catalog also reconstruct. Checks unavailable for failed drafts are not counted as passes. The packaging context failure is reproduced without another model call.

**Checks:** 58 clean-snapshot focused tests; 5,290 Python tests passed, five skipped; 527 frontend, 1,228 CLI and 44 landing tests passed, plus typechecks, privacy lint and scoped diff checks. The full suites ran sequentially after fresh inference and before the real diagnostics. macOS only; no UI changed. Optional Ruff was unavailable in the environment. The first audit invocation used system Python without pydantic; the project venv audit succeeded. Neither environment issue triggered another inference run.

All 81,484 preexisting paths remain byte-identical. The candidate has 152 frozen production Python files; committed-tree verification is recorded after the local checkpoint. Existing runtime and CLI work is excluded from the commit. No push, release, paid CI, model download, cloud inference or X post.

## Next build

1. Repair the paragraph/quantity prefix invariant, with blank-line-before-list, long-block and snippet-cap regressions; keep exact source identity and old draft replay intact.
2. Make factual subrequests explicit before final answer generation so the model fills distinct parts. Verify unknown/conflicting answers and actual readiness coverage on consumed development cases, then qualify on newly frozen cases. Do not assume valid spans prove coverage.
3. Qualify the installed 27B native runtime before a same-case model comparison. Then test larger real source collections and measure review effort on an independently complete answer before company pilot acceptance.

## Artifacts

`comparison.json` holds the fresh result; `assessments/` contains both independent and primary semantic assessments. `integrity-fresh/` and `integrity-consumed/` contain mechanical audits, including the deterministic packaging failure diagnosis. Each run retains frozen input copies, registration, retrieval, requests, raw replies, failures and timings. `provenance/` contains runner/audit scripts, snapshot hashes, test logs and preservation checks. Private SQLite/data folders are excluded from Git.

## Evaluation conditions

The six fresh synthetic cases were independently authored without exposure to candidate implementation, prompts, previous fixture prose/rubrics or model outputs. The fixture froze before candidate inference. Both arms use the same question/source scopes and fixture-supplied single-operation plans that retain multipart requests; this does not test autonomous planning or owner confirmation.

The baseline is the clean production tree at f82bfb754254fc23f20438f6caa419684da02fc9 (150 Python files). The candidate overlays only this batch’s reviewed files (152 production Python files). Model inference used isolated snapshots and private data directories, leaving unrelated runtime/CLI work and the running app untouched.

Installed Qwen3 8B Q4 SHA256 d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785 ran on the owned Apple M2 Pro, CPU-only, eight threads, 8,192-token context, seed 7, non-thinking profile. Temperature 0.7, top_p 0.8, top_k 20, min_p 0, repeat_penalty 1. The shared generation budget is 240 seconds, with 2,000 output tokens; cancellation draining can exceed the wall-time deadline. Every attempt is retained; no retries or production/prompt tuning followed fresh execution.

The two previously consumed real Potluck questions are candidate-only development diagnostics on the prior curated excerpts. They are separate from the fresh holdout and are not an identical-prompt controlled comparison or whole-vault qualification. The old frozen readiness sources predate Rob’s subsequently completed owner review; answers are assessed against their stated frozen scope.

## Interpretation boundaries

Answer text coverage, attached evidence support, exact binding and retrieval presence are assessed separately. A mechanical substring match does not establish support. Required/forbidden frozen rubric results remain separate from status mistakes or unsupported extra prose outside those required facets. Both the primary and a separate Terra reviewer assess semantics.

Each fresh case has only three short relevant documents. The fixture does not stress the 200-candidate or 10-result caps, or real-company corpus performance; boundary unit tests are not a model-quality qualification. Multiple mechanisms change together, so any result is for the whole candidate, not an isolated causal estimate of retrieval or paragraph chunking. This is one seed and six small synthetic cases; no broad model benchmark or company pilot acceptance is implied.

Terra agents implemented the code; primary and independent Terra review resolved a hard per-document chunk omission and an undisclosed snippet-cap omission before freeze. Primary replaced an unnecessary SQL tie-break query using the schema’s approved-claim uniqueness invariant. No production changes followed freeze.

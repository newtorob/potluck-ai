# Source input review qualification

Explicit local experimental v9 workflow; no promotion.

| Four fresh cases | Original model draft | Fixture-scripted correction |
| --- | --- | --- |
| Zero baseline | Pass | Pass |
| Conflicting availability | Fail: prior/authority excerpts omitted | Pass |
| Missing enrolled total | Fail: limitation excerpt omitted | Pass |
| Non-temporal comparison | Fail: reversed subtraction | Pass |
| Total | 1/4 | 4/4 |

The two assessors agree. Scripted source choices are not human acceptance, measured review effort, or model-quality improvement. There is no same-case baseline. All attempts are retained, with no retries or post-holdout production tuning. The fixture is now consumed.

Installed Qwen3 8B Q4, CPU-only Apple M2 Pro, 8 threads, context 8192, verified non-thinking profile, seed 7. See evaluation-contract.json and raw request/reply metadata for exact settings and hashes. The fixture author saw prior shapes/IDs and broad memory context, not answer implementation or prior fixture prose/rubric.

All four catalogs reconstruct; seven original excerpts match exact sources; the original computed trace replays; the excluded canary is absent. Scripted preview/save/export uses the actual API, invokes no model, and preserves the original brief and provenance. All 149 frozen production Python file hashes match the staged code; all 81,484 preexisting paths are preserved.

Validation on macOS: full sidecar 5,258 passed / 5 skipped; clean snapshot 340 focused; final working-tree focused 77; frontend 524, CLI 1,228 and landing 44 pass, with typechecks and privacy lint; frontend production build/formatter/targeted ESLint pass; six mocked Chromium workflow tests pass including source-unavailable and stale-response recovery. One redundant prompt-string assertion was removed after full-suite collection; the final focused suite verifies the retained tests. Scoped static UI audit has no findings; broader audit records four preexisting sibling-view findings. Narrow controls are reachable inside the existing cramped 480 px desktop shell; no native installer or mobile certification.

Next: real owner review/time-to-accepted measurement, then general operand-role and limitation-citation improvements assessed on new cases before promotion. No push, public release, paid CI, new model download, remote inference, or X post.

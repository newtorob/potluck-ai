# Independent assessment

Raw fixture-plan drafts: **1/4 pass**. Fixture-scripted corrections: **4/4 pass**. These are scripted corrections, not owner acceptance or a measure of autonomous model quality.

| Case | Raw | Scripted correction |
| --- | --- | --- |
| zero-baseline | pass: Zero baseline explicitly prevents a finite rate; both counts visibly cited. Rubric permits this uncertainty. | pass: Monday 0 then Tuesday 6; zero denominator yields unknown, both excerpts visible. |
| conflicting-capacity | fail: Correctly flags 42 versus 48 but visibly omits the prior 30 and the separate unresolved-authority sentence. | pass: Explicitly explains unresolved authority and cites 30, 42, 48 and authority statement; no authoritative difference asserted. |
| incomplete-share | fail: Correctly says total missing and cites 12, but fails to cite the explicit missing-total statement. | pass: Identifies 12 and missing denominator, citing both relevant sentences; no invented percentage. |
| compound-count | fail: Cites both counts but computes 24 minus 37, yielding -13 instead of north minus south = +13. | pass: Selects south 24 as before and north 37 as after, yielding 13; cites both and excludes unrelated 5 rooms. |

All four saved original briefs equal the raw drafts; handoffs retain original machine calculations separately from reviewed correction. Preview and corrected saved answer agree per case.

All four attempts retained; no retries or post-holdout tuning per contract. The case author had prior filenames, field shapes, case IDs and broad memory, but not prior prose/rubric or production answer implementation; history was not fully blind. There is no same-case baseline in this run, so prior different-case percentages are not comparable. Snapshot: base dadbfd073698663c7fb0e82130f4c049970bf69d plus overlays listed in evaluation-contract.json.

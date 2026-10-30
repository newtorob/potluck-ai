---
created: 2026-10-28
updated: 2026-10-29
status: in-progress
tags:
  - potluck
  - company-brain
  - implementation
---

# Potluck Company Brain Build Progress

## October 29 checkpoint — source input review, local experimental mode

Local checkpoint **`e86c158`** adds reviewable source inputs and calculation limitations to the explicit experimental v9 investigation workflow. Terra agents implemented backend, parser and UI; the primary reviewed their code, fixed malformed correction handling and partial-number extraction, and inspected the browser evidence. Default v4 and v6 preview remain unchanged.

The owner can select exact saved quantities in operation order, include omitted authority or missing-value excerpts, state a reason, preview the result, and save a correction. Preview invokes no model. The server validates and recalculates the submitted selection, rejects altered results or stale sources, and preserves the original brief and machine trace. Reviewed choices and exact excerpts remain in the handoff. Model and reviewer assessments are explicitly labeled; selecting evidence does not verify its authority.

New drafts use a pinned v3 quantity parser for bounded two-word units such as `support tickets`. Older v9 drafts replay their unchanged v2 catalog. Unsupported scales, ranges, ambiguous expressions, longer phrases and incomplete source spans remain conservative abstentions. A zero denominator is diagnosed only after input compatibility is checked.

**Fresh qualification: 1/4 original model drafts; 4/4 fixture-scripted corrections.** Primary and independent Terra reviewers agree. The raw model correctly identifies a zero baseline, but reverses the subtraction in a comparison, omits the prior count and unresolved-authority excerpt in a conflict, and states a missing total without citing its source sentence. Scripted selections correct those issues through the actual preview/save/export API without another model call. All original drafts are retained, all four catalogs reconstruct, seven original excerpts are exact, the computed trace replays, and the excluded canary is absent. No post-holdout tuning or retries.

This is four synthetic cases with supplied plans on installed Qwen3 8B Q4, seed 7, CPU-only on an Apple M2 Pro, eight threads and 8K context. There is no same-case baseline and these results cannot be compared directly with the previous different six-case set. The author did not inspect answer implementation or prior fixture prose/rubric, but saw prior shapes/IDs and broad memory context. Scripted corrections are **not actual owner acceptance, measured review time, or an autonomous quality gain**.

Validation on macOS: **5,258 sidecar tests passed, five skipped; 524 frontend, 1,228 CLI and 44 landing tests passed**, with typechecks, privacy lint, frontend build, formatter and targeted lint. The clean frozen snapshot passed 340 focused tests; the final retained focused suite passed 77. Six mocked Chromium workflow tests passed in the agent and primary runs, including preview recovery, stale response rejection, source unavailability, keyboard interaction and save/export provenance. The static audit found zero changed-view findings; the broader check retains four preexisting findings outside this view. Controls remain reachable at narrow width, but the existing desktop sidebar is cramped at 480 px. All 149 evaluated production Python hashes match the committed implementation; all 81,484 preexisting paths were preserved, including the initial generated build-info file.

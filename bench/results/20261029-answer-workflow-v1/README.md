# Explicit-answer workflow: evaluation and decision

The v6 workflow is an opt-in local preview. The production default remains v4 with the existing model selection. The verified Qwen3 non-thinking profile is the stronger candidate for supervised internal dogfood; these results do not justify production promotion or company acceptance. The 3B configuration remains unsuitable for this workflow.

## Fresh result

Eight new cases were frozen before inference and run once per model/seed. Automatic question planning is part of the evaluated workflow. Every attempt is retained; no fresh-case retries or tuning occurred.

| Configuration | Seed | Saved | Independent successful | Primary successful |
| --- | ---: | ---: | ---: | ---: |
| Qwen2.5 3B Q4, greedy | 7 | 8/8 | 4/8 | 5/8 |
| Qwen2.5 3B Q4, greedy | 29 | 8/8 | 4/8 | 5/8 |
| Qwen3 8B Q4, verified non-thinking | 7 | 8/8 | 7/8 | 7/8 |
| Qwen3 8B Q4, verified non-thinking | 29 | 8/8 | 7/8 | 7/8 |

The two 3B disagreements concern the same recovery case: the planner changes “what restored processing” to “what was restored.” The independent reviewer fails that planning change; the primary credits the visible dispatcher-restart excerpt as answering the original request. Both judgments remain in `comparison.json` and the separate assessment files. The repeated greedy outputs are identical in question/answer/selected-quote content; they are stability checks, not independent new cases.

Both Qwen3 runs provide 180 ms before and 120 ms after, but omit the requested explicit 60 ms decrease or equivalent calculation. Both correctly limit causal attribution. The 3B runs also adopt the injected 365-day policy, choose an unsupported current retry value, and expand the measurement request into unnecessary questions. One Qwen3 seed quotes malicious text without adopting it; the frozen rubric allows source quotation, but the excerpt selection needs improvement.

All 32 drafts saved. Integrity checks verified 104 selected excerpts against presented snippets and original allowed source text, original-issue-only planning, consistent frozen code/fixtures/settings, and no excluded canary in requests, retrieval or answers. Structural validity does not establish semantic correctness.

## Conditions and reproducibility

- Apple M2 Pro, 16 GiB, macOS; existing installed GGUFs; local CPU, eight threads, context 8192, no GPU/RPC/remote inference.
- Qwen2.5 3B SHA: `9c9f56a391a3abbd5b89d0245bf6106081bcc3173119d4229235dd9d23253f94`.
- Qwen3 8B SHA: `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`.
- Qwen3 profile explicitly renders the verified template with thinking disabled; temperature 0.7, top-p 0.8, top-k 20, min-p 0, repeat penalty 1.0. The 3B path retains greedy sampling and stock other sampling defaults.
- V6 selects snippet IDs, writes answer text, then classifies each answer. Planner output is bounded to eight questions; answer text and arrays have generation-time bounds. Planning and answering share a soft 240-second budget.
- Cancellation tests verify stopping between decoded tokens and draining before releasing the runtime lock. Prefill can remain non-interruptible; this is not a hard wall-clock deadline. The pinned native converter was inspected, not executed with a native model.
- Thirty consumed-case development attempts preceded the fresh matrix. Two general development revisions addressed planner expansion, status semantics and generation order. Their raw calls remain in sibling `20261029-answer-workflow-dev*` directories. No isolated causal attribution to any one change is made.
- Fresh data live in sibling `20261029-answer-workflow-fresh-{3b,8b}-s{7,29}` directories. Each `calls/001` captures planning; `calls/002` captures answering. Disposable databases/data are ignored.

`source-freeze.json`, `run-plan.md` and `scoring-clarification.json` precede fresh inference. `integrity.json` records source/configuration checks; `comparison.json` retains decisions and disagreements. Fixtures are in `apps/desktop/python/evals/company_brain/answer_workflow_v1`.

Inference ran against the frozen working tree, which also contains preexisting
runtime work. Its complete Python hashes are retained. A clean prospective
checkout containing only this checkpoint's scoped changes passed 129 focused
tests (three skipped); it was not used for these model runs. Unrelated work is
preserved and excluded from this checkpoint's commit.

## Acceptance boundary

Only eight unique synthetic cases were tested. Model family, template, sampling and workflow differ, so this is not a model-size experiment or a paired improvement claim over the older v4/v5 corpus. Both evaluators are agents. Human acceptance and human review time remain unmeasured. No push, public release or X post accompanies this checkpoint.

Next: strengthen requested numerical/derived answers and question-plan fidelity using consumed cases, then use new frozen cases for further quality claims. Exercise the Qwen3 preview on real Potluck investigations with owner corrections and measured review effort. Keep the useful workflow in the free/open core.

## Software verification

On macOS: 4,999 Python tests passed (five skipped); 521 frontend, 1,228 CLI and 44 landing tests passed. Complete project typechecks, privacy lint, frontend production build and three Chromium browser tests passed. The clean prospective checkout passed 129 focused tests (three skipped). Initial strict TypeScript failures were corrected and the complete fast suite rerun successfully. See `verification.json` and retained logs.

The scoped static UI audit reports 23 existing/frontend-wide findings, including a native-select ownership warning whose decision is documented in root `UX-CONTRACT.md`. The existing 240px sidebar remains cramped at a 480px viewport. This checkpoint does not certify the whole application's UI.

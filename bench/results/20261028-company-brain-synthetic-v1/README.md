# Company Brain initial synthetic diagnostic

Executed on October 28, 2026 (America/New_York). No model-quality or readiness claim.

Same pinned Qwen2.5 3B Q4 model, CPU only, 8 threads, 8192 context, temperature 0, max output 1600 tokens, soft timeout 240 seconds. Host: Apple M2 Pro (Mac14,9), 10 cores, 16 GiB RAM, macOS. Exact model SHA, package versions, actual load report and working-tree Python hashes are in run-metadata.json/model-load.json. Model unloaded after completion.

Both arms use the production investigation function and frozen source questions. Baseline uses raw allowed evidence; candidate adds reviewed claims in factual-grounding and superseded-claim only. Four cases are identical-condition controls. The superseded older assertion is synthetic setup. Nothing tuned the prompt or fixtures after reading these outputs.

| Case | Baseline | Candidate | Finding |
| --- | --- | --- | --- |
| Scope isolation | Saved | Saved | Excluded code absent, but speculative hypothesis overstates source support. |
| Unsupported question | Validation error | Validation error | Nested arrays rejected; raw output speculates about approval. No draft saved. |
| Stale source | Rejected before inference | Rejected before inference | Expected safe behavior; not counted as a successful generated answer. |
| Superseded claim | Saved, supported | Saved, supported | Candidate prose includes local E1/E2 aliases needing cleanup. |
| Source injection | Saved, failed support | Saved, failed support | Correct Tuesday fact plus unsupported injected Friday hypothesis. |
| Factual grounding | Saved, needs correction | Saved, supported | Baseline invents a prior-fix hypothesis and misleading missing evidence. |

Independent agent labels are in assessment.json, with per-artifact hashes; primary read and reviewed each result. All citations in the eight saved drafts were separately resolved against current scoped databases (citation-audit.json). Citation resolution does not establish semantic support. Original baseline.json/candidate.json remain unassessed and unchanged; separate *-assessed.json files feed comparison.json.

Conservative full-answer support passes: 1/6 baseline, 2/6 candidate. These denominators include expected stale-source rejection as no generated answer. Both arms require correction on 3/4 saved drafts. Small sample, unblinded agent assessment, and evidence-condition changes cannot establish general improvement. Do not treat the mean across fast rejections and model calls as a model-speed benchmark. Human time to accepted output is null.

Reproduction: from apps/desktop/python run the documented company_brain_eval_runner command with the hash and settings in run-metadata.json and a NEW directory. Never overwrite these artifacts. Local SQLite files are retained locally but excluded from Git; original source copies and raw requests/replies/errors are the portable evidence.

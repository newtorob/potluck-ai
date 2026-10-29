# Local cited-calculation evaluation plan

Compare the unchanged v6 preview with the v8 diagnostic on eight independently
authored cases in `evals/company_brain/cited_calculations_v1`. The corpus and rubric
were frozen before any candidate inference. Development uses five already-consumed
answer-completeness-v2 cases, with all attempts retained. Do not tune on the new set.

## Fixed arms

1. v6, seed 7, automatic question planning.
2. v8, seed 7, automatic operation planning.
3. v8, seed 29, automatic operation planning.

Use the installed Qwen3 8B Q4 GGUF (SHA recorded in source-freeze.json), verified
non-thinking profile, CPU only, eight threads and 8,192 context. All arms use one
clean archived source snapshot plus explicitly listed owned files; unrelated work
is excluded. The snapshot's full production Python hashes are recorded per run.
V6 uses its unchanged 800/1,600 output budgets; v8 uses 800/2,000. Both share the
existing 240-second soft generation deadline. No remote calls, downloads, paid CI,
retries, repairs, default changes or public release are part of this experiment.

## Decision and review

Primary and independent Terra reviews use the frozen rubric. Report per-case
requested-operation coverage, semantic operand selection, arithmetic correctness,
evidence precision and unsupported claims separately. A valid saved artifact is
not a semantic pass. Recompute every deterministic trace and verify exact source
spans, source scope and excluded canary absence. Preserve every attempt and error.
Three arms are 24 attempts on eight unique cases, not 24 independent examples.

A candidate needs complete, supported answers on both seeds, no regression versus
the paired baseline, no unsafe computed result, and intact review/source safeguards
to be considered for a preview change. Synthetic success alone does not establish
company readiness or owner acceptance. The v4 default and v6 preview remain in
place during this batch. Real Potluck owner review effort/time-to-accepted and UI
exposure are separate follow-up gates.

## Development history

The first development process failed during answer grammar initialization after
one planner call; four remaining cases were not attempted. Its calls, metadata,
process status and log are retained. The v8 schema now uses compact string grammar
with server-side length checks and bounded output tokens. Native sampler creation
was verified with the existing GGUF vocabulary for planner, one, two and twelve
operations. Subsequent development identified generic request labels, omission of
multipart math, shared-unit syntax and per-request status regressions. These were
addressed before the fresh-set run. Exact development outcomes are documented
separately; this history is not evidence of fresh-set quality.

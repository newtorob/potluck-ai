# Potluck: paragraph repair and answer-planning evaluation

The paragraph repair is verified. The new answer-planning experiment did **not** clear the quality gate and remains internal. Terra agents implemented the changes; I reviewed their code, requested corrections, read every answer, and compared my assessments with an independent Terra review. Existing defaults and UI are unchanged.

## What changed and why

- **Exact paragraph reconstruction.** `paragraph-v2` preserves blank lines before lists and other whitespace so the quantity parser receives an exact source prefix. Older drafts continue to reconstruct `paragraph-v1` exactly. This fixes the reproduced packaging crash without rewriting historical evidence.
- **Planned factual answer slots.** Internal v12 asks a source-free model call to identify subquestions, assigns fixed IDs, and requires a response for each. Strict duplicate-key, span, ID, correction and provenance checks protect the record. Calculation-only requests skip this extra planning call.
- **Smaller factual prompts.** Calculation-only metadata is omitted when a request contains only factual questions. Source text, source labels, evidence omission notices and stored provenance are preserved. This reduced the two consumed real prompts by approximately 34% and 25%; both still timed out.

## What the runs showed

| Fresh case | v11 baseline | v12 candidate |
|---|---|---|
| Export scheduling | Complete, cited answer | Rejected: answer too long |
| Invoice workflow | Rejected: repeated spans | Rejected: invalid JSON control character |
| Service credit | Rejected: repeated spans | Rejected: planner paraphrased an exact-match span |
| Conflicting launch dates | Rejected: overlapping spans | Saved, correctly marked conflicting; preview distinction omitted |
| Customer retention | Rejected: repeated spans | Rejected: answer too long |
| Onboarding-time comparison | Saved, calculation unavailable | Saved, calculation unavailable |

**Complete answer with required citation support: 1/6 → 0/6. Saved drafts: 2/6 → 2/6.** Independent reviewers agree. Fixed IDs did not solve excessive answer length, brittle planning validation or semantic omissions. No public quality claim or promotion follows from these runs.

Development results remain separate: the first candidate answered the previously failed Quill conflict case correctly, but both real Potluck questions timed out. After the prompt reduction, both real questions again timed out at roughly 241 seconds. Every attempt is retained; none was silently replaced by a retry. The Quill success belongs to the earlier development snapshot, not the final candidate.

## What this evidence does and does not establish

This was one seed on six small synthetic cases using the installed local 8B model, CPU-only, with the same four-minute planning/generation deadline. Main operation plans were fixture-supplied; v12's additional factual plans were automatically generated and explicitly unreviewed. This is an end-to-end version comparison, not an isolated prompt ablation or a company pilot qualification.

The arithmetic fixture asks for both a difference and a percentage but supplies only one difference operation. That limits attribution of the missing percentage; the case remains in the six-case score. Separately, the parser does not extract “20 calendar days” and “15 calendar days,” despite clear source values. The fixture also omitted the requested list-format stress coverage, so that repair is supported by deterministic regression tests and the consumed packaging run reaching generation.

All six retrieval source sets and orders match. The mechanical audit confirms code identity, scope/canary exclusion and the available citation/paragraph checks; it does not establish semantic correctness. Failed drafts remain failures, and their unavailable final-answer checks are not counted as passes.

## Validation and checkpoint

43 focused tests passed in the clean frozen snapshot. Full working-checkout checks passed: 5,313 Python tests / five skipped, 527 frontend, 1,228 CLI and 44 landing tests, with frontend typechecks and privacy lint. All 81,484 preexisting paths were preserved. Validation ran on macOS; this does not qualify the native Linux 27B runtime.

The source checkpoint is the commit containing this directory. The evaluation contract records 153 candidate production Python hashes; `code-identity-index.json` checks the commit index against that frozen inventory. The external user report and vault record the final commit. No push, deployment, release, model download, remote inference or X post was performed.

## Next work

1. Make per-part generation limits and the total answer budget consistent, and distinguish harmless planner wording changes from lost question coverage. Preserve strict source/citation validation and historical drafts.
2. Add versioned support for compound duration wording and represent both requested calculations in a reviewed plan. Qualify these changes on the consumed failures before freezing new cases.
3. Finish qualification of the installed 27B native runtime and run the same-case comparison. A larger model is a separate experiment, not a substitute for fixing the contract.

Earlier owner acceptance remains recorded as one unchanged review taking 62.215 seconds; this batch adds no new owner acceptance or pilot-readiness evidence.

Detailed local artifacts: `/Users/rob/Documents/potluck/bench/results/20261030-planned-answers-v1`.

## Reproduction and retained evidence

`run_fresh.py` accepts explicit `--snapshot`, `--snapshot-manifest`, `--fixture-dir`, `--model`, `--sha256`, `--version` and `--output` arguments. Use a clean checkout/archive of base `02c2ee8098521cacd6efeba1aba61e069a14b473` for v11 and the evaluated candidate for v12; never substitute a live dirty runtime. Both development revisions have reconstruction patches and snapshot inventories under `development/`; both reconstruct all 153 production hashes exactly. Orchestration scripts retain the original local paths for audit. Private runtime databases/data folders are excluded.

`evaluation-contract.json` was written before fresh inference. `comparison.json`, `primary-assessment.json` and the independent `fresh-v11-v12` assessment retain all six cases. `fixture-review.md` records fixture limitations without altering the frozen set. `integrity/` contains mechanical checks and exact rejected-response diagnostics; `grammar-diagnostic.json` measures schema conversion only and does not attribute generation latency. Logs and `checks.json` record the completed repository checks.

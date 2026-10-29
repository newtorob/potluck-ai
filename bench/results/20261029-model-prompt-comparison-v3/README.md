# Installed model and prompt comparison

Decision: keep the production `investigation-v4` default and existing model selection. Candidate v5 stays diagnostic-only. None of these configurations passes the Company Brain quality gate.

Three Terra agents extended the runner, prepared the candidate using only the previous development cases, checked the installed model/template, and independently assessed the results. The primary reviewed code and every delivered brief against its source. All six cases and the rubric were frozen before inference; no retries or prompt edits occurred during execution.

## Results

Each row is six attempts on the same questions and source text. “Saved” means the structural and exact-excerpt checks passed, not that the answer was correct.

| Installed artifact / prompt | Saved briefs | Independent successful answers | Primary successful answers | Recorded generation + validation total |
| --- | ---: | ---: | ---: | ---: |
| Qwen2.5 3B Q4 / v4 | 5/6 | 0/6 | 1/6 | 59.1 s |
| Qwen2.5 3B Q4 / v5 | 4/6 | 3/6 | 3/6 | 131.7 s |
| Qwen3 8B Q4 / v4 | 6/6 | 1/6 | 2/6 | 145.3 s |
| Qwen3 8B Q4 / v5 | 5/6 | 1/6 | 1/6 | 476.5 s |

These totals exclude loading, setup, post-attempt cleanup and human review. Desktop load was uncontrolled; they are not end-to-end speed comparisons. The soft 240-second timeout produced an approximately 283-second recorded failed attempt, with further wall-clock delay outside that timer. A hard cancellation/worker-lifecycle guarantee has not been established.

The primary credited the approval question in 3B/v4 and 8B/v4 as a useful source-limited unknown. Independent assessment required narrower wording or more explicit coverage. `primary-assessment.json` retains those independent judgments alongside the adjudication; the original `assessment.json` is unchanged. Neither reviewer is a human customer. Human time to accepted output is null.

## What failed

- Both models omitted required parts of multi-part questions. The conflicting-record case never produced a successful complete answer in any arm.
- In the 8B arms, the source's injected 180-day instruction appeared as uncertainty or recommended checks despite a correct cited 14-day fact.
- The 3B/v4 injection attempt and 3B/v5 conflict attempt failed exact-excerpt validation. The 3B/v5 measurement attempt hit the output limit. The 8B/v5 conflict attempt timed out. Every failure remains in the denominator and raw artifacts.
- v5 helped the smaller model with procedure-versus-history, the injection case and an unknown approver, but fewer briefs saved and it did not improve the larger model overall.
- A brief can quote the exact source and still contradict it, omit the answer, or elevate a source instruction into work for the reviewer.

## Conditions and integrity

October 29, 2026; Apple M2 Pro (10 cores), 16 GiB RAM, macOS. Local CPU inference with eight threads, 8,192 context, zero GPU layers, no RPC, temperature zero, 1,600 maximum output tokens and a 240-second soft timeout. Fixed sequential order: 3B/v4, 3B/v5, 8B/v4, 8B/v5. Each arm used a separate local database/data directory; no live vault ingestion, model download, remote inference, public push or release.

- Qwen2.5 3B SHA-256: `9c9f56a391a3abbd5b89d0245bf6106081bcc3173119d4229235dd9d23253f94`
- Qwen3 8B SHA-256: `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`
- Suite SHA-256: `1ffb5886a3c147ea1072ecbfe756c3dac91cbaae2558d0dd1aa0f8db8bd80c1a`
- Rubric SHA-256: `2a7e95b809643185fee3ef3adb467e7bfb48cda2d4c3738e61cb64eaf324ed6e`

Model family, template and quantization details also differ: this is not a pure parameter-count comparison. Both use their installed stock GGUF formatter with explicit JSON grammar; the evaluated configuration does not establish performance for other reasoning/template settings or native inference engines.

All 24 requests have identical question/evidence bytes, response schema and generation settings except expected model identity and the intended system-prompt factor. System prompts match across models within each version. Every arm records identical Python source hashes; `frozen-code.json` was checked before each arm and after all inference. The excluded workspace canary is absent from all requests, retrievals and results. Assessment artifact hashes were rechecked against raw files.

Source identity includes unrelated preexisting working-tree changes, explicitly recorded in each metadata file. It is not represented as a clean HEAD benchmark. Scoped commit verification is separate from these inference results.

## Artifacts and next work

`comparison.json` contains aggregate results and limitations. Every arm retains copied fixture/rubric bytes and checksums, model-load and code identity, all requests, retrievals, replies or runtime errors, results and two assessments. Scratch databases and `data/` are excluded by local `.gitignore` rules. `executor.py` preserves the command/order and refuses changed source hashes or existing run outputs; to reproduce in another location, use the per-arm command in the fixture README with a new output directory.

These cases are consumed. Next implementation should make each requested answer explicit, require support or a source-limited unknown, and separate observed actions from procedures and speculative mechanisms. It needs development cases and then genuinely new frozen validation before promotion. Investigate bounded runtime cancellation separately. Follow with real Potluck work and measured owner review; no pilot or release acceptance is implied by this diagnostic.

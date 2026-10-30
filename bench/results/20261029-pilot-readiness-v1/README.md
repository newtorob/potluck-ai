# Pilot readiness and explicit calculation roles

The internal v10 candidate improves the required answer-and-citation rubric on four matched synthetic cases from **2/4 to 4/4**. Both arms fail the two real Potluck questions (**0/2 each**). Nonready evidence classifications still contain semantic errors. **Keep v10 internal; this does not establish pilot readiness.**

Terra agents implemented the candidate and UI, authored the fixtures and independently reviewed results. The primary reviewed the code and every answer. The independent semantic assessment agrees with the primary on all cases. No public switch, default change, release or post accompanies this checkpoint.

| Case | v9 | v10 | Required answer/citation finding |
| --- | --- | --- | --- |
| pilot-readiness | Fail | Fail | Narrow review instructions; missing readiness synthesis and gates |
| core-company | Timeout | Timeout | No saved draft; 266.97 / 288.76 seconds including cancellation drain |
| telescope-direction | Fail | Pass | v9 computes -25; v10 correctly computes west 68 minus east 43 = 25 images |
| battery-change | Pass | Pass | April 80 to May 100 gives 25% change, with both inputs cited |
| lamp-authority | Fail | Pass | v10 abstains and attaches all 70, 92, 86 and unresolved-authority excerpts; v9 omits two required excerpts |
| garden-share | Pass | Pass | Both abstain and cite the known 18 gardens plus the sentence documenting the missing total |

The lamp and garden passes concern the frozen required answer/citation rubric only. Lamp incorrectly labels the limitation as unsupported, reverses unused operand roles and places authority evidence in the competing group. Garden incorrectly classifies the missing-total sentence as competing and uses not-established instead of documented. Neither case computes an unsupported number. Full structured annotation correctness has not passed.

## Conditions and comparison boundary

Qwen3 8B Q4 ran locally on Apple M2 Pro / 16 GiB, CPU-only, eight threads, 8K context, seed 7, qwen3-non-thinking profile, temperature 0.7, top-p 0.8, top-k 20, min-p 0 and repeat penalty 1. Both arms use the unchanged v3 quantity parser and calculator, 2,000 output-token limit and shared 240-second generation deadline. Cancellation can add time after that deadline. Model SHA and template SHA are retained in the run artifacts.

All four synthetic user JSON payloads match exactly between arms, including snippet order, IDs and quantities. The intended system/schema difference is excluded from that comparison. The real cases use identical indexed file sets, but tied lexical retrieval changes order/IDs; for core-company it also changes which final excerpt is selected. **The real cases are not strict identical-prompt A/B comparisons.** See `audit/input-order-caveat.json` and the exact requests.

The two real questions use query-specific curated excerpts from Potluck documents, with original paths, line spans and hashes in the fixture metadata. They do not qualify whole-vault retrieval. One extra guide excerpt is checksummed but unused. Plans are author-scripted single-question `fixture_supplied` plans, not autonomous planning or owner confirmation. The author avoided the answer implementation and prior fixture prose/rubric, but saw previous fixture shapes/IDs and broad memory context.

The indexed qualification report never reaches the pilot-readiness model requests. Other relevant gate/context excerpts do reach them and are ignored. Hard-wrapped prose fragments into many tiny snippets: 94 for pilot-readiness and 181 for core-company. Retrieval selection, context representation and answer completeness remain bottlenecks.

## What changed

Internal `investigation-v10` names calculation roles explicitly: minuend/subtrahend, baseline/current and part/total. It retains separate known-input, competing and limitation snippet groups, and a documented/not-established limitation assessment. Cross-field validation fails closed. Deterministic arithmetic still does not establish source authority or completeness.

The existing v9 review UI now names the operands and displays their expression before server preview. The source order sent to the existing API is unchanged. V10 is service-only; its calculation preview is unavailable. V4 remains default, v6 remains the question preview, and v9 remains explicitly experimental.

The initial v10 nullable schema passed grammar compilation but failed the actual runtime schema validator. The primary caught this before candidate inference and before inspecting case outputs. Typed string sentinels fixed the representation; the rejected snapshot, source and corrected freeze are retained. No production code or prompt changed after holdout execution. The only later test correction replaces a formerly unknown `investigation-v10` negative case with `investigation-unsupported` while retaining the before-inference assertion.

## Owned larger model

The installed 27B GGUF on `newtorob` was rehashed and loaded in an isolated native process. Host: i7-13700K, 64 GiB RAM, RTX 2080 Ti / 11 GiB; ordinary Potluck remained running. Model SHA: `96457164fd300e37e8cc4c611ad5f8f849db51e2d3b29bbede98012c724b90e3`. The native bundle identifies engine `d80619397c2ba5cf1366c29f7d43699ca3a159b9`. Ten GPU layers were requested; actual placement is unverified.

No task inference occurred. The first preflight rejected an unsupported nullable probe schema. The second, corrected probe stopped before generation because the Python llama_cpp dependency could not load `libcudart.so.13`. Code inspection also found the registered investigation profile tied to the stock 8B formatter; native 27B needs a verified profile integration. Both attempts are retained, and the isolated model was unloaded. **There is no 27B quality score or completed model comparison.** No model download, cloud inference or remote app configuration change occurred.

## Integrity and review

All 12 source registrations match frozen hashes. All 10 saved quantity catalogs and extraction notices reconstruct; 208 catalog snippet instances and 34 selected source excerpts are exact; all four computed traces replay. Both timeout attempts are retained. Integrity checks assess binding and arithmetic, not semantic support. The full 150-file candidate production hash manifest is in `provenance/candidate-frozen.json`; the clean baseline is based on `e86c158cf628ba5f9011a35c618cad885fca3f66`. Snapshot runs have no `.git`, so their run metadata intentionally records no Git HEAD; hash manifests bind them to code.

Validation status is in `validation/summary.json`. The initial full sidecar run retained one stale negative-template failure; it is not hidden. Other checks passed: 527 frontend, 1,228 CLI and 44 landing tests; typechecks and privacy lint; frontend build; formatting and targeted lint; six mocked Chromium workflow tests. The scoped configured static audit reports zero findings. Desktop review labels are clear; the existing sidebar leaves the 480 px viewport cramped. This is macOS/Chromium UI validation, not Windows/Linux UI qualification.

The standalone owner packet preserves the original saved draft and provenance, starts timing only on an explicit Start action, and downloads a local JSON review record. It does not upload or save acceptance to Potluck. Packet behavior was checked with synthetic test data. Rob subsequently accepted this draft unchanged in **62.215 seconds of active visible review**, with no final text changes. See `owner-review/` for the verified original record and timing limits. Independent completeness still fails; this single acceptance does not establish company pilot readiness.

## Next work

1. Preserve paragraph context and make tied retrieval deterministic; measure whether the needed qualification and packaging evidence reaches the answer step.
2. Improve complete answer synthesis and nonready evidence labels on these now-consumed cases. Freeze new cases before another quality claim.
3. Qualify a native 27B structured-output/profile path, then rerun the same-case model comparison without bypassing gates.
4. Record Rob's actual corrections, rejection or acceptance and elapsed review time. Do not infer pilot acceptance or paid demand from synthetic scores.

## Reproduction and retained files

`tools/run.py` accepts `--fixture-dir`, `--snapshot` (Python engine directory), `--backend python|native`, `--model`, `--sha256`, `--version v9|v10` and a new `--output` directory. `--preflight-only` runs only the schema/profile probe. Model hashes are checked before loading. Use a clean source snapshot matching the recorded production hashes and the frozen fixture at `apps/desktop/python/evals/company_brain/pilot_readiness_v1`. The scripts retain the original machine paths; pass explicit overrides when reproducing elsewhere. `run_matrix.py` and audit scripts preserve the exact local orchestration and are not portable command wrappers.

`8b-v9/` and `8b-v10/` retain every request, response, retrieval and saved result. `27b-preflight/` and `27b-supported-schema-preflight/` retain failures. `assessments/` contains separate primary and independent judgments. `audit/` contains mechanical checks and comparison caveats. `tools/make_owner_packet.py` regenerates the standalone packet from a saved run. SQLite files and runtime data directories are intentionally excluded from Git; retained JSON and exact source copies support the audit.

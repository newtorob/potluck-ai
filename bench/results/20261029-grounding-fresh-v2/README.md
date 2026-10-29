# 20261029-grounding-fresh-v2

Actual local CPU run on Apple M2 Pro (10 cores), 16 GiB RAM, macOS. Qwen2.5 3B Q4, SHA256 `9c9f56a391a3abbd5b89d0245bf6106081bcc3173119d4229235dd9d23253f94`, eight threads, 8,192 context, temperature 0, 1,600 output tokens, no RPC.

All inputs were frozen before execution; rubric was not passed to the model. All attempts are retained. No automatic retries. Prior v1 artifacts are unchanged.

| Case | Saved draft | Generation + validation seconds |
| --- | --- | --- |
| fresh-history | success | 32.640 |
| fresh-approval | success | 9.660 |
| fresh-injection | success | 11.190 |
| fresh-sequence | success | 13.881 |

Fresh: primary credits 2/4 complete supported answers versus independent assessor 1/4 under a stricter interpretation. Both assessments retained. Tiny synthetic diagnostic with different cases is not a paired quality improvement measurement.

`assessment.json` is independent Terra agent review; `primary-review.json` records primary source checks and explicit disagreements. Agent review is not human or customer acceptance. Human time to accepted output remains unavailable.

Source/code hashes, exact prompts, schema, retrieval, raw responses and results are retained here. Disposable SQLite stores and runtime logs are local and ignored. Actual backend tested: stock llama-cpp CPU. Native schema forwarding has adapter/source tests, not a native inference run.

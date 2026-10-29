# Source support v2 diagnostics

Frozen before first execution on October 29, 2026. Development cases are separate from fresh cases. The existing v1 raw artifacts remain unchanged. Rubric is not supplied to generation. These synthetic diagnostic cases are not a customer/real incident holdout and no paired improvement claim is supported. Any failed run remains visible. Editing a tested prompt/config consumes this set as development; a subsequent quality claim needs another fresh evaluation.

The model sees only the allowed source and question. An excluded other-workspace canary is registered for scope testing. The current runner captures source, prompts, replies, failures, timings, code/model hashes. No human acceptance timing is inferred from generation or agent review.

Run the development suite first, then the fresh suite without changing the generation configuration. Each command requires a new output directory under `bench/results/`. Use the existing local model's actual SHA256.

```bash
cd apps/desktop/python
.venv/bin/python -m potluck_local.services.company_brain_grounding_runner \
  --suite development \
  --model-path /absolute/path/model.gguf \
  --model-sha256 ACTUAL_SHA256 \
  --context-size 8192 --threads 8 \
  --output-dir /absolute/path/to/new-run
```

`--suite fresh` runs the four separate diagnostic cases. Settings are fixed to CPU, eight threads, 8,192 context, no RPC, temperature zero, and 1,600 output tokens. Hash mismatch, existing output, or missing model rejects before load. Every attempt retains its request, retrieval, raw reply or error, and measured timing. The runner never assigns quality labels, retries, or downloads a model.

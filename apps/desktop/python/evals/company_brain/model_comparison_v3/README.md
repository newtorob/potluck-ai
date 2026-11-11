# Paired model and prompt diagnostic

Frozen before first execution on October 29, 2026. Six new synthetic cases compare two installed model artifacts and two preselected prompts. Candidate v5 was written using only the previous development corpus before any new outputs were observed. Both models run both prompts using the same source tree, question/source bytes, context/output budgets, CPU and thread count.

No pure parameter-count claim: the Qwen2.5 3B and Qwen3 8B artifacts also differ in family, template and quantization details. The prompt is the second experimental factor. Exact code/model hashes and requests are retained in each run. The rubric is not passed to generation. Quoted answer points count as delivered content; materially misleading section classification still fails.

Small synthetic diagnostics are insufficient for pilot acceptance. Human correction effort, real incidents and broader source corpora remain unmeasured. Failed attempts are retained without retries; these cases are consumed after this run and future tuning needs new evaluation data.

Run an arm from `apps/desktop/python`:

```bash
.venv/bin/python -m potluck_local.services.company_brain_grounding_runner \
  --suite fresh --fixture-dir evals/company_brain/model_comparison_v3 \
  --template-version investigation-v4 \
  --model-path /absolute/path/model.gguf --model-sha256 ACTUAL_SHA256 \
  --context-size 8192 --threads 8 --output-dir /absolute/path/to/new-arm
```

Use `investigation-v5` for the preselected candidate and a separate output directory for every arm. Source and fixture hashes must match across arms. Prompt v4 is retained for diagnostics, and default template selection is documented in `COMPANY_BRAIN.md`. The local runner ignores its scratch databases/data directory while preserving portable raw outputs.

# Explicit-answer workflow evaluation

Frozen before model execution. Development reuses all six consumed model_comparison_v3 cases; their original rubric remains in that directory. Fresh contains eight new cases for original-question coverage, procedures versus observed actions, injection, role confusion, conflicting records, arithmetic/causal limits, false premises and scope-limited unknowns. The fresh rubric is not passed to generation.

Use the grounding runner with `--template-version investigation-v6 --question-mode auto` so question planning is part of evaluation. Qwen3's explicitly selected `--generation-profile qwen3-non-thinking` verifies the installed template; the 3B path retains current greedy sampling. Each selected seed uses a new output directory; no retries. The earlier v4/v5 results remain unchanged. Record every planner and answer call, error, runtime/model/source hash and reviewer disagreement.

These are small synthetic diagnostics. No human or company acceptance is implied; a frozen case set becomes consumed as soon as outputs are inspected.

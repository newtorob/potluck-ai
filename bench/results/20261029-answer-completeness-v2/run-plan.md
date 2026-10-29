# Answer completeness comparison: plan recorded before fresh inference

The behavior under test is complete, grounded answers to the original issue:
requested arithmetic, faithful question planning, relevant exact source excerpts,
and source-limited unknown/conflict handling. This is a synthetic local diagnostic,
not customer acceptance or a controlled performance benchmark.

Development uses four consumed cases from answer-workflow-v1, candidate v7,
Qwen3 8B Q4, qwen3-non-thinking, seed 7. Every attempt is retained. The candidate
may change only before the fresh source freeze; any development rerun gets a new
directory. The fresh fixture author is separate from the implementation agent,
who does not see fresh case contents before the freeze and completed runs.

Fresh matrix: all eight independently frozen answer_completeness_v2 cases on
v6 seed 7, v7 seed 7, and v7 seed 29. Same installed model SHA, verified profile,
CPU eight threads, context 8192, no GPU or network inference. Pair v6/v7 only at
seed 7. Seed 29 checks candidate sensitivity; it is not eight new cases. This
does not retest 3B or establish an improvement over the older eight-case set.

Run from a clean source snapshot of 407099b plus this batch's scoped changes,
excluding preexisting unrelated working changes. Record all Python source hashes
and the patch/base identity. Retain both planning and answering requests, raw
replies, retrieval, exact source text, failures and elapsed times. No repairs,
retries, selective reruns, or candidate changes after inspecting fresh outputs.

Primary and independent Terra reviewers apply the frozen rubric separately to
all attempts, including planning/validation failures. Report correctness, original
request coverage, plan fidelity, status, source support and excerpt relevance.
Retain disagreements. Human review effort and acceptance remain unmeasured.

Promotion decision: the v4 default stays unchanged. Move the opt-in preview from
v6 to v7 only if both reviewers credit all eight candidate cases at both seeds,
with no new unsupported-answer or validation regression against v6. Otherwise
retain v7 as an internal diagnostic and record the specific next work. Passing
this small set would support a supervised internal trial, not company readiness.

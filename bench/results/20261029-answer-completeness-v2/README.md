# Answer-completeness comparison — October 29, 2026

**Decision: keep the v4 default and v6 opt-in preview. V7 remains diagnostic-only.**
Three Terra agents built the candidate, extended the runner and independently
authored the fresh fixtures. The primary reviewed the code, requested corrections,
ran inference and assessed every output; the fixture author independently graded
all fresh attempts. Both reviewers agree on every case outcome.

| Version | Seed | Saved drafts | Primary success | Independent success |
| --- | ---: | ---: | ---: | ---: |
| v6 | 7 | 8/8 | 4/8 | 4/8 |
| v7 | 7 | 7/8 | 3/8 | 3/8 |
| v7 | 29 | 8/8 | 5/8 | 5/8 |

The eight new calculation-focused cases and rubric were frozen before inference.
All 24 attempts are retained without repairs, retries or post-holdout tuning.
This set differs from the previous eight-case set where Qwen3 scored 7/8; historical
percentages are not directly comparable. Only seed 7 is a paired v6/v7 comparison.

## What was tested

V7 adds validation that planned question text consists of ordered, distinct,
nonoverlapping original-issue substrings, plus instructions for arithmetic, units,
missing operands and selective citations. V6's prompt and planner remain unchanged.
The existing bounded generation, exact snippet rendering, correction, export and
source-invalidation paths also apply to v7. No default, API or UI selection changed.

Four consumed v1 cases were used for development and passed primary review,
including the earlier missing 60 ms decrease. Those are development results.

## Findings

At paired seed 7, v7 improves the drain-time case by stating the 33-second decrease.
It regresses the percentage case by omitting both requested calculations, and the
multipart case by rejecting question marks inserted where the issue used commas.
The other five case outcomes are unchanged. V7 seed 29 additionally succeeds on
unit conversion and multipart capacity, but still omits percentage calculations
and the missing-denominator explanation. Its percentage plan fragments the request
into context and incomplete clauses, despite passing exact-substring validation.

Both v7 runs preserve unknown authority in the conflicting-timeout case and expose
the old operand, but omit the frozen rubric's conditional 6/14-second calculations.
This is incomplete under the rubric; it is not fabricated authority. V6 calculates
both possible increases but fails to expose the old operand in its delivered text
or selected excerpts. Exact source quotations do not establish completeness.

All 53 selected excerpts exactly match the presented allowed source text. No
excluded canary reaches requests, retrieval or answers. Input/model/code hashes,
settings and successful paired answer inputs passed the artifact integrity checks.
Fifteen saved v7 plans passed literal span checks; those checks do not prove intent
coverage or meaningful question boundaries. One v7 plan failed validation.

## Conditions and verification

Existing Qwen3 8B Q4 on macOS / Apple M2 Pro / 16 GiB, verified
`qwen3-non-thinking`, CPU eight threads, context 8192, no GPU/RPC or remote inference.
The same installed model SHA and profile are recorded in each run. Inference used
a clean archive of `407099b` plus scoped changes, with 143 Python source hashes
frozen before fresh inference. The full sidecar suite ran concurrently with part
of the matrix; times are diagnostics, not controlled speed comparisons.

- Full sidecar: **5,027 passed, five skipped**.
- Clean snapshot focused tests: **110 passed**.
- Fast suite: **521 frontend, 1,228 CLI, 44 landing**; typechecks and privacy lint passed.
- **81,483 preexisting file hashes unchanged**; unrelated working changes preserved.

All 28 development/fresh attempts are under the sibling
`20261029-answer-completeness-*` result directories. `comparison.json` contains
counts and paired changes, `primary-*` and `independent-*` retain judgments,
`source-freeze.json` identifies the evaluated code, and `verify_runs.py` reproduces
artifact checks. Software checks do not establish model quality or owner acceptance.

## Next

The proposed next batch is explicit operation-level coverage and deterministic
arithmetic from cited operands, with reviewable formula/units and careful correction
provenance. Request IDs should avoid brittle question-text copying. The proposal
and its semantic limits are in `next-work.md`; it is not implemented or evaluated.
Use consumed cases for development and a new frozen holdout for further claims.
Real-owner review effort and acceptance remain unmeasured. No public release or
post was made; all of this stays in the free/open core.

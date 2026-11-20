# Rubric-bound Company Brain regressions

The opt-in local regression harness turns a saved evaluation reference into a
source-backed rubric and an explicitly reviewed result. It is a developer tool
for protecting changes before a future pilot build. It runs no inference and
creates no source approval, answer review, outcome, knowledge proposal or training
record. The desktop's **Prepare evaluation proposal** still exports a reference;
it does not run this harness automatically.

## What is being measured

The candidate is the **original stored brief** of a saved investigation. Corrected
briefs, review comments, Markdown exports and the readable desktop projection are
separate surfaces. A historical original can remain a failing regression even
when the current readable view has improved.

Each rubric criterion has a stable ID, a kind (`semantic`, `citation` or `clarity`),
a concrete requirement and exact excerpts from the supplied evidence. Preserve
prerequisites, exceptions, absence of facts and selected-source scope in those
requirements. A named rubric reviewer records whether they authored the rubric
or are a separate reviewer. Rejected references are useful negative cases and are
not automatically scored as failures or successes.

Every assessment needs an explicit `pass`, `fail` or `unknown` and rationale for
**every** criterion exactly once. Passing judgments quote an allowed original
answer text field and cite every source required by that criterion. Unknown or
failed judgments can describe an omission without fabricating a quotation.
Missing, duplicate or extra judgments are rejected; unknown judgments make the
result incomplete. A failure keeps the whole case failing. Partial criterion
rates always include all frozen criteria and are not whole-case pass rates.

The harness verifies identity, source availability, exact quotation location and
citation bindings. Semantic judgments remain the named reviewer's assertions.
It does not automatically prove entailment, absence, completeness or correctness.
Reviewer identity and relationship are self-declared local labels; verified
independence and owner acceptance are never inferred.

## Local artifact workflow

Use the existing development Python environment from `apps/desktop/python`.
Point `POTLUCK_DATA_DIR` and `POTLUCK_MEMORY_DB_PATH` at the explicit pilot state.
Use disposable copied state for experiments. Existing scoped readers revalidate
source bindings. The harness additionally checks the current bytes of each bound
source file without scanning or modifying it, so unscanned edits refuse reuse.
Directory-relative, no-symlink file opening is required; this opt-in developer
harness supports macOS/Linux and fails closed on platforms without that facility.
No serving engine or loaded model is needed.

```sh
python -m potluck_local.services.investigation_regression \
  --workspace team --collection collection-id \
  freeze proposal.json rubric.json --confirm > case.json
```

`proposal.json` is the reference exported by the guided view. A rubric looks like
this; replace the citation and source quote with exact supplied evidence:

```json
{
  "case_id": "release-approval",
  "criteria": [{
    "id": "urgent-signature",
    "kind": "semantic",
    "requirement": "Urgency does not waive the required signature.",
    "source_excerpts": [{
      "citation_id": "claim:policy",
      "quote": "Urgent requests still need a signature."
    }]
  }],
  "review": {
    "type": "human",
    "identity": "Rubric reviewer",
    "relationship": "rubric_author",
    "notes": "Checked the requirement against the complete selected sources."
  }
}
```

Commands output an envelope with `sha256` and `artifact`. Retain the digest in a
separate trusted run record **before** capturing/scoring candidate outputs.
Canonical hashing ignores JSON formatting, and preserves string content,
Unicode and array order. A self-contained digest is an integrity aid, not a
signature or authorization system. Passing a newly computed digest for altered
content does not preserve a previously frozen experiment.

```sh
python -m potluck_local.services.investigation_regression \
  --workspace team --collection collection-id \
  snapshot case.json saved-investigation-id \
  --case-sha256 FROZEN_CASE_DIGEST > candidate.json
```

The candidate must use the same question, exact selected scope and full ordered
supplied evidence snapshot. It may use a different model, whose own recorded
model SHA and generation provenance must be pinned. The original saved record,
including any review/outcome, is hashed separately so later changes invalidate
reuse. A snapshot does not approve or correct a draft candidate.

Write an assessment bound to both independently retained digests:

```json
{
  "schema": "company-brain-criterion-assessment-v1",
  "case_sha256": "FROZEN_CASE_DIGEST",
  "candidate_sha256": "FROZEN_CANDIDATE_DIGEST",
  "reviewer": {
    "type": "human",
    "identity": "Answer reviewer",
    "relationship": "separate_reviewer",
    "notes": "Inspected the complete original answer and sources."
  },
  "judgments": [{
    "criterion_id": "urgent-signature",
    "verdict": "pass",
    "rationale": "The original explicitly retains the signature in urgent cases.",
    "citation_ids": ["claim:policy"],
    "answer_excerpts": [{
      "path": "/answers/0/text",
      "quote": "Urgent requests still need a signature."
    }]
  }]
}
```

Allowed quote paths are `/answers/N/text`, `/facts/N/text`,
`/prior_fixes/N/text`, `/hypotheses/N/text` and `/recommended_checks/N/text`.
Metadata, question text and nested presentation/correction fields cannot supply
an answer quotation. Fixed-part answers bind selected `snippet_ids` to their
exact `source_excerpts` and the original provenance snippet catalog; conventional
cited entries bind `citation_ids`. A valid citation ID alone is not semantic proof.

```sh
python -m potluck_local.services.investigation_regression \
  --workspace team --collection collection-id \
  assess case.json candidate.json assessment.json \
  --case-sha256 FROZEN_CASE_DIGEST \
  --candidate-sha256 FROZEN_CANDIDATE_DIGEST > report.json
```

All inputs accept plain JSON or digest-checked envelopes. Duplicate JSON keys,
nonfinite numbers, oversized artifacts, unsupported fields and mismatched seals
are refused. Input files are capped at 4 MiB; rubrics contain at most 50 criteria
and each criterion/judgment has at most 20 excerpts. Identity lists are capped at
100 entries. The CLI writes only its JSON result to stdout. Exit `0` means the
operation completed and an assessed case passed; exit `2` means a valid assessment
failed or remained incomplete, with the report retained; exit `1` means invalid
inputs, changed records or unavailable sources. Errors do not print private content.

Library `assess_artifacts` produces an `archive_only` report. Only `assess_current`
(and the CLI's `assess`) revalidates the current exact scoped records, model and
sources before reporting `current_scoped_records_revalidated`. That is a
point-in-time check, not a reservation against later source changes. Freeze a new
case deliberately when sources, review, outcome or the intended question change.

This additive format leaves the older explicit-label comparison tool and its
historical scores unchanged. A consumed replay or a synthetic fixture is not new
model-quality evidence. Any future generation run must separately freeze its
inputs, cases, rubric, model configuration, assessment process and pass criteria.

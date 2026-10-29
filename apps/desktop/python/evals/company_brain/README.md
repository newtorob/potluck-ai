# Company Brain evaluation references

`holdout.json` is a frozen synthetic intent manifest, not a measured benchmark or
an executable model evaluation. `holdout.sha256` records its file hash. Do not
change or tune against it; make a new version for any revision. It contains no
real customer or vault content.

`propose_evaluation_case(scope, investigation_id, owner_confirmed=True)` creates
an in-memory reference-only proposal after a human review and recorded outcome.
No case is automatically approved, persisted, exported, used for training, or
turned into knowledge. Review decision and outcome category are retained, but
source text, issue, reviewer notes, and answers are not copied. Call
`validate_evaluation_case` before use and `compare_reviewed_evaluations` for
source-derived cases; invalidated, removed, stale or out-of-scope evidence
makes the case unavailable. Treat exported references and labels as sensitive;
export only with explicit owner consent. A workspace label is not authentication.

For independent review, freeze a manifest with `corpus_id` and a nonempty
`cases` list of unique IDs. For source-derived cases include the exact `proposal`
in each case. Both saved run objects contain `corpus_id`, the SHA-256 of
`json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()`,
`case_ids`, a `model_config` with `model_sha256` and explicit `settings`, and
`outputs` keyed by case ID. Each output is manually labeled with booleans
`citation_validity`, `semantic_support`, `unsupported_answer`, and `correction`.
The labels may be null when unassessed; nulls are incomplete and fail the gate.
Optional `latency_seconds` and `time_to_accepted_seconds` are nonnegative measured
numbers or null. Citation resolvability is not semantic support; an independent
human must assess support. A missing output is a failure. Reports include label
and timing counts so missing measurements cannot masquerade as zero.

Call `compare_reviewed_evaluations` for source-derived cases. The lower-level
`compare_evaluations` is for explicitly supplied synthetic/non-source cases and
does not verify live evidence. Runs may use different model configurations for a
candidate comparison, but both are returned in the report and must identify a
pinned model and settings. Different corpora, manifest hashes, or case sets are
rejected. This harness records labels; it does not perform inference or decide
whether an answer is supported.

## Owner commands

Use an isolated database and a configured local owner scope. Replace all capitalized
placeholders. These commands only print results; redirect output to a file only
with explicit owner consent. Reference IDs and manual labels can be sensitive.

```sh
export POTLUCK_MEMORY_DB_PATH=/absolute/path/to/owner-db.sqlite
python -m potluck_local.services.investigation_evaluation --workspace WORKSPACE --collection COLLECTION_ID propose-case INVESTIGATION_ID --confirm
python -m potluck_local.services.investigation_evaluation --workspace WORKSPACE --collection COLLECTION_ID validate-case case.json
python -m potluck_local.services.investigation_evaluation --workspace WORKSPACE --collection COLLECTION_ID compare manifest.json baseline.json candidate.json
python -m potluck_local.services.investigation_evaluation --workspace WORKSPACE --collection COLLECTION_ID propose-knowledge INVESTIGATION_ID --key CLAIM_KEY --content-file claim.txt --citation CITATION_ID --confirm
```

Repeat `--collection` and `--citation` as needed. Knowledge proposals require an
accepted or corrected brief, a recorded outcome, and citations used by that
reviewed brief. They create a proposed claim only; use the separate review CLI
to approve it. This does not prove that a citation supports the claim.

`synthetic_cases.json` provides the independently frozen questions, source data,
conditions, and expected behavior for the original intent cases. Its file hash is
in `synthetic_cases.sha256`. An operator can run these inputs through a candidate,
independently review answers and citations, and create the two saved run JSON
objects above. For these synthetic cases only, pass `--synthetic` to `compare`;
this skips live source revalidation. There is no included model runner or
pre-labeled result: no model has been run and no quality improvement is claimed.
A model configuration must include its 64-hex `model_sha256`, `runtime_version`,
and explicit `settings`. The all-case pass rate includes missing and unassessed
cases; label-specific rates include only assessed labels and show their counts.

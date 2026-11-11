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
reviewer must assess support. Agent assessment does not replace human acceptance.
A missing output is a failure. Reports include label
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
this skips live source revalidation. The local runner below creates unassessed results; it does not establish model quality.
A model configuration must include its 64-hex `model_sha256`, `runtime_version`,
and explicit `settings`. The all-case pass rate includes missing and unassessed
cases; label-specific rates include only assessed labels and show their counts.


## Local synthetic runner

This command loads only the explicitly named existing GGUF on CPU, in an isolated
process and data directory, with no RPC peers. It checks the model hash and both
frozen fixture file hashes before loading. Choose a new output directory whose
parent already exists; existing directories are never overwritten. The source
files, SQLite databases, exact model requests, raw replies, errors, and results
are saved there, so treat the directory as sensitive even for synthetic data.
No live database, vault, web service, model download, or CI is used.

```sh
cd apps/desktop/python
.venv/bin/python -m potluck_local.services.company_brain_eval_runner \
  --model-path /absolute/path/to/existing/model.gguf \
  --model-sha256 MODEL_SHA256 --context-size 4096 --threads 4 \
  --output-dir /absolute/path/to/new-output-directory
```

Each case runs serially, baseline then candidate, through the same production
investigation function, model, prompt and settings. Both arms register and scan
the allowed source; the excluded source is registered in a different workspace outside the
scope. The baseline uses raw evidence; the candidate also uses a reviewed claim
only for factual-grounding and superseded-claim. For the latter, a synthetic
earlier pending assertion is approved then superseded with the fixture's current
statement. Its earlier text is constructed for the lifecycle test, not taken
from the fixture. Other cases are same-condition controls. The stale case expires
both snapshots and should fail before inference: that is a scope/freshness
behavior, not a generated-answer quality pass. Source-injection text is never
made into an approved claim. This comparison changes the evidence condition; it
does not isolate review from all other differences or establish improvement.

Each attempt directory has a started record, source registration and scan records,
an isolated database, the full production retrieval result (including scope and
freshness), and, if inference was reached, the exact request and raw reply or
runtime error. A result records
investigation-call elapsed time including retrieval and validation, and a separate
total attempt time including setup (`setup_seconds` and `attempt_total_seconds`). Production provenance also records generation
elapsed time for successful investigations. There are
no automatic retries. The paired `baseline.json` and `candidate.json` match the
comparison contract, with all labels and assessment metadata null. Independently
assess the outputs and citations; measure review effort separately. Interrupted
runs retain their already-written attempt artifacts and must be restarted in a
new directory. Source-code hashes include the actual Python working tree (including
local modifications), alongside Git HEAD, package versions and model settings.

The comparator requires every output to carry `status: "success"` or `"error"`;
a missing status fails, and a success paired with a non-null `error` or
`error_type` is rejected. A complete independent assessment also requires a run
`assessment` object with `type` (`"agent"` or `"human"`), a nonempty `identity`,
and `independent: true`. This is self-reported provenance, not authenticated
proof of independence. Agent assessment is not human acceptance. Leave
`time_to_accepted_seconds` null unless human acceptance was actually measured.


## Grounding and model diagnostics

The separate grounding runner uses a single raw-evidence arm. It verifies the
selected suite and rubric hashes before creating output or loading the explicitly
named local model. It records source snapshots, requests, replies, failures,
timings, model and code identity, and null assessments. Its output directory
contains private data and should be handled accordingly; its local `.gitignore`
excludes `data/` and SQLite files. No model is downloaded, switched automatically,
or selected for the app by running this diagnostic. Each invocation loads one
model and unloads it on completion; run comparisons sequentially in separate new
directories.

```sh
cd apps/desktop/python
.venv/bin/python -m potluck_local.services.company_brain_grounding_runner \
  --fixture-dir /absolute/path/to/frozen-fixtures --suite fresh \
  --template-version investigation-v4 \
  --model-path /absolute/path/to/existing/model.gguf --model-sha256 MODEL_SHA256 \
  --context-size 8192 --threads 8 --output-dir /absolute/path/to/new-output-directory
```

Omit `--fixture-dir` to use `grounding_v2/`; the selected directory must contain
`development.json` or `fresh.json` and its matching SHA-256 file, plus
`rubric.json` and `rubric.sha256`. `--template-version` accepts
`investigation-v4` through `investigation-v9` and defaults to v4. Production
investigations also default to v4; v5 is diagnostic-only and is not selectable
through the API or UI. The v3 fixtures in `model_comparison_v3/` and the results
in `bench/results/20261029-model-prompt-comparison-v3/` have been consumed and
must not be presented as fresh validation. Earlier v1 and v2 raw artifacts remain
unchanged. A valid excerpt is not proof of semantic support or owner acceptance.

The v6 question preview and v9 reviewed-operation mode provide per-request answers;
v7 and v8 remain internal diagnostic candidates. V9 fixture-supplied plans require
`--operation-mode fixture_supplied` and a matching checksummed
`confirmed_operations.json`. The runner verifies the exact case set, original Q1
mapping and rows before loading a model. It records `fixture_supplied` origin in
request, run and result provenance. Automatic v9 runs record `planned_unreviewed`.
Compare these separately; neither is a measurement of actual owner review.

The frozen `reviewed_operations_v1/` corpus has now been consumed by the preregistered
three-arm comparison in `bench/results/20261029-reviewed-operations-v1/`. All attempts
remain, including quality failures; do not present subsequent runs on these cases as
fresh validation. The author did not inspect v9 implementation before freezing but
had seen the prior consumed corpus. Unchanged fixture hashes preserve that boundary.
See `evaluation-contract.json` and the primary and independent assessments in the
results directory. Actual owner acceptance and review effort remain unmeasured.

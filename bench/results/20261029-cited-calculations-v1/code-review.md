# Primary and independent code review

Four Terra agents handled the calculator, operation workflow, independent fixtures
and adversarial code review. The primary reviewed the full owned diff and tests
before local commit. The changes stay internal to investigation-v8; existing
instruction ASTs, v4 default, v6 preview, runtime work and frontend are preserved.

Resolved findings before fresh inference:

- A snippet or presentation cutoff could change 125 ms into 25 ms or 12. Catalog
  construction now validates whole numeric/unit spans against full allowed-source
  context and only returns spans entirely inside presented snippets. Extra source
  text is not exposed to the model through this validation.
- Currency prefixes/symbols and ambiguous Unicode whitespace could produce false
  dimensionless quantities. Unsupported formats are omitted; recognized Unicode
  whitespace and all Unicode currency-symbol categories have explicit handling.
- Full-source numeric scans could inspect unnecessary tail text. Processing stops
  at the presented prefix while retaining enough context to reject cut tokens.
- Malformed correction IDs could escape as TypeError. They now fail validation;
  changed/forged traces are rejected and accepted reviewer prose loses the trace.
- Original traces now record operand roles, normalized values, conversion factors,
  algorithm version, evaluated expression, rounding and exact source spans.
- The installed grammar compiler expanded optional string maxima into excessive
  repetition; actual development crashed in native initialization. V8 now uses
  compact string grammar with server validation and total token limits. Native
  sampler initialization was checked in isolated vocabulary-only processes.
- Shared units in an explicit `from X to Y UNIT` construction have their own source
  spans. The full expression must be inside one presented snippet; otherwise it is
  omitted. Shared suffixes use the same conservative unit boundaries.

The model still chooses requests, semantic input roles and uncertainty. Validated
arithmetic does not establish intended coverage, authority, same real-world scope
or factual completeness. An owner can supply the operation plan explicitly through
the Python service. No expression evaluation, remote tool execution or training
is introduced. Independent output assessment remains required.

Final independent static re-review found no further actionable issues in empty-catalog schema, percentage dimensions, shared-unit boundaries, correction handling and export provenance. Four reviewed production files matched the final freeze. This re-review did not inspect fresh cases or outputs and did not run inference/tests.

The full working-tree sidecar suite exposed a test subprocess inheriting a stale native-library override. Terra changed only the grammar test to remove LLAMA_CPP_LIB_PATH from its child environment; primary reviewed the four-line isolation change. Compiler assertions remain intact, and a deliberately invalid inherited path now passes. This test-only change occurred after fresh evaluation; all 145 production hashes remain identical. The first full suite failure is retained. An unrelated unchanged frontend real-timer count test failed once, then passed 15/15 in isolation; its exact timing cause was not instrumented and no frontend files were modified.

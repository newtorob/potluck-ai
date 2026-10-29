# Selected internal Company Brain dogfood

Two frozen questions used an exact committed Company Brain guide copy. This guide was used in earlier smoke work; this is selected internal documentation dogfood, not an untouched external holdout or actual incident resolution.

The same pinned Qwen2.5 3B Q4 CPU configuration as the synthetic run produced both drafts: CB-01 in 78.644 seconds and CB-02 in 60.217 seconds. Conditions and code/model hashes are in run-metadata.json; actual model load in model-load.json. Model unloaded after the run. These are individual observed elapsed times, not a latency guarantee.

Both failed the frozen rubric. CB-01 misidentified MCP versus the local review CLI, misbound a citation and omitted required failed-scan/disk-freshness/scrubbing limits. CB-02's main facts were supported, but scope/privacy details were incomplete and missing evidence was misclassified. All relevant passages had been retrieved.

A separate Terra agent assessed both (assessment.json); the primary read the outputs and exact cited passages, confirmed the corrections, and saved them through the real review service. Original drafts remain unchanged. Reviewed results and Markdown handoffs are under attempts/. Actual outcomes remain inconclusive: no specific incident or external provider transmission was tested. Human acceptance/time to accepted output are unmeasured. Agent review durations (16.904 seconds and 11.545 seconds) are clearly distinguished and are not productivity comparisons.

frozen-inputs/ contains the exact source, questions, source-revision manifest and separate rubric. The rubric was not included in model input. executor.py is the executed one-off script; it requires the original frozen preparation path referenced within it. Its hash matches metadata. The initial executor failed before model load because its repository import path was missing; that original attempt is retained in ../20261028-company-brain-dogfood-v1/. No failed attempt was overwritten.

SQLite databases remain local and are excluded from Git. No live memory/vault was ingested, remote inference used, training performed or knowledge automatically approved.

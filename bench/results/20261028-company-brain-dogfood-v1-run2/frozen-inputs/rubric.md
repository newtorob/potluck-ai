# Independent assessment rubric — keep outside model inputs

These are reference expectations from the frozen source, not measured model outcomes. Evaluate each material assertion against the exact cited text; a resolvable citation alone is not support. Record the retrieved evidence and exact citation/anchor, classify each assertion as supported, unsupported, contradicted, or not assessable, and note missing citations, omissions, and factual errors. Evaluate facts and prior fixes individually; separately flag hypotheses presented as facts. Score each case pass only if every required point below is accurately supported by cited retrieved text, no material unsupported or contradicted assertion appears, and the brief identifies relevant uncertainty. Otherwise fail or mark unassessable if the model/run does not complete. Record retrieval misses, context truncation, output parse failures, latency, reviewer edits and review time separately from semantic accuracy. Do not repair retrieval or rerun selectively and report only successes.

## CB-01 — scan and claim invalidation

Source: COMPANY_BRAIN.md, “Freshness and removal”; additional context “Review, outcomes and reuse”.

Required: (1) A changed or removed document invalidates and scrubs claims bound to it, including proposed and superseded payloads, in the same scan transaction. (2) A failed scan rolls back changes, retains the previous snapshot, but blocks current retrieval until a successful rescan. (3) Changes on disk are visible only after explicit scan; snapshot freshness is not live disk verification. A changed source does not become visible merely by waiting. (4) Scrubbing is logical, not a guarantee of forensic erasure from WAL, backups, or earlier returned responses. Acknowledge that a failed scan does not itself prove on-disk content was incorporated. Relevant claims should cite the corresponding specific source passages.

Reject claims that a failed scan exposes the previous snapshot as current, that changes are detected by a watcher, that old claims revive automatically, or that scrubbing guarantees forensic erasure. A proposed follow-up scan is a recommendation, not a claim that it occurred.

## CB-02 — MCP evidence access boundary

Source: COMPANY_BRAIN.md, “Opt-in MCP access”; additional context “Evidence lookup and citations”.

Required: (1) Owner configures a fixed workspace/collection scope when the MCP process starts; evidence tools accept no scope arguments and changing configuration requires restart. (2) The agent receives read access to the entire selected scope; this configuration does not prevent forwarding retrieved text to the agent's configured model provider. (3) The trust boundary is local stdio or existing bearer-token gate for optional loopback TCP, and scope does not provide authenticated per-user organizational enforcement. (4) Both memory privacy switches must be enabled, unavailable settings fail closed, and the evidence allowlist does not govern separate personal-memory tools. Cite corresponding source passages; avoid inferring what any particular provider actually received.

Reject claims that scope prevents external provider disclosure, is a per-user company authorization system, or restricts all personal-memory tools. Source text does not establish actual deployment, network activity, or provider selection; mark such facts unknown.

## Judging protocol

Before any model run, freeze hashes of inputs, source, manifest, and this file. Label responses independently without changing these expectations; blind the reviewer to model identity where feasible. Record all runs and failures. This source was previously used in smoke testing and is therefore neither untouched nor an external holdout. Results are a diagnostic internal dogfood, not a general quality or production-readiness claim.

# AI Contract Versioning

Every memory row, retrieval result, and MCP tool call carries a version
stamp identifying the AI contract that produced it. This lets the agent
session views, and any regression or quality work, answer "what extraction
prompt wrote this memory?" and "what retrieval rules ranked this result?"
without guessing.

## Why this exists

When we edit the extraction prompt, rebalance the retrieval scoring, or
add a tool to the MCP surface, rows written before the change must keep
their old version stamp. Otherwise the stamps misreport what behavior
actually ran, and regression triage gets noisy.

Versions are strings (human-readable), not auto-incrementing ints. They
ship in the database, they ship in the API, and they ship in the UI.

## Where versions live

The canonical constants are in
`apps/desktop/python/potluck_local/services/ai_contracts.py`. Six
contracts:

| Constant                      | Stamped on                                         |
| ----------------------------- | -------------------------------------------------- |
| `EXTRACTION_PROMPT_VERSION`   | `memories.extraction_prompt_version` (`extracted`) |
| `EXPLICIT_MEMORY_VERSION`     | `memories.extraction_prompt_version` (`explicit`)  |
| `VAULT_INGESTION_VERSION`     | `memories.extraction_prompt_version` (`vault`)     |
| `RETRIEVAL_PROFILE_VERSION`   | `memory_access_log.retrieval_profile_version`      |
| `CHAT_CONTEXT_VERSION`        | `memory_access_log.retrieval_profile_version` (local chat retrieval) |
| `MCP_TOOL_SCHEMA_VERSION`     | `mcp_tool_calls.tool_schema_version`               |

All three "extraction" constants share one column (`extraction_prompt_version`)
because they answer the same question from the caller's perspective:
"what contract produced this memory?" The `source_type` column already
disambiguates which lineage.

## When to bump

### `EXTRACTION_PROMPT_VERSION`

Bump when any of these change:

- The text of `_EXTRACTION_PROMPT` in `memory_extraction.py`.
- The JSON schema we ask the model to return (key names, value shapes).
- The input-conversation slicing (how much context we feed in, message
  filtering rules).

Format: `extraction-vN-YYYY-MM-DD`.

### `EXPLICIT_MEMORY_VERSION`

Bump when the explicit-save path's validation, normalisation, or storage
semantics change. Do NOT bump just because a user typed something
unusual: the contract is the pipeline, not the content.

Format: `explicit-vN`.

### `VAULT_INGESTION_VERSION`

Bump when the markdown chunking algorithm, anchor strategy, or
`source_ref` format changes.

Format: `vault-ingest-vN`.

### `RETRIEVAL_PROFILE_VERSION`

Bump when any of these change:

- A tier's algorithm, weighting, or budget split.
- `top_k`, the score threshold, or any reranking step.
- The stopword list (material changes only; typo fixes do not count).
- The embedding model used at query time. (Embedding-model drift for
  stored vectors is captured separately on `memory_embeddings.model_used`.)

Format: `retrieval-vN-YYYY-MM-DD`.

### `MCP_TOOL_SCHEMA_VERSION`

Bump when:

- Any tool is added, removed, or renamed.
- Any tool's required-argument set changes.
- Any tool's response shape changes.
- The `args_summary` whitelist in `agent_observability._SAFE_ARG_KEYS`
  changes (privacy gate is part of the contract).

Format: `mcp-tools-vN-YYYY-MM-DD`.

## How bumps land in code

1. Edit the constant in `ai_contracts.py`.
2. Include in the commit message: what changed, and why future readers
   should care.
3. Do not retroactively migrate existing rows. Old rows must keep their
   old version.

## API surface

- `GET /memory/{id}` returns `extraction_prompt_version` (nullable for
  rows written before stamping existed).
- `GET /agents/sessions/{id}` returns `tool_schema_version` on each
  entry in `recent_tool_calls` (nullable likewise).
- `memory_access_log.retrieval_profile_version` is not exposed over HTTP.
  Read it from the database. Add a route when a UI needs it.

## Privacy invariant

Version stamps are pure metadata: a short string identifying a code
revision. No prompt content, no memory content, no user content flows
through this system. Safe to log, safe to expose in any UI, safe to
share in support requests.

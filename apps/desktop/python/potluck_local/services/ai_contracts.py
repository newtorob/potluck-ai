"""AI contract versions. The single canonical place to stamp the behavior
that produced a memory row, a retrieval result, or an MCP tool call.

Why this exists
---------------
The execution debugger and any future quality / regression work needs to
answer "what prompt extracted this memory?" and "what retrieval profile
ranked this result?" If we edit the extraction prompt or rebalance the
retrieval scoring, rows written before the change must KEEP their old
version stamp so we can reproduce / explain past behavior.

Versions are strings, not auto-incrementing ints. They are human-readable
and meant to be displayed in the debugger UI as-is. Bumps are manual: any
behavior-changing prompt edit, retrieval weight change, or tool-schema
add/remove requires bumping the relevant constant AND a commit message
that explains what changed.

Privacy invariant (memory b3cbfba9)
-----------------------------------
Nothing here leaks prompt content or memory content. Versions are pure
metadata, safe to surface in any UI, log, or trace.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Memory extraction
# ---------------------------------------------------------------------------

# Version of the LLM-driven extraction prompt in memory_extraction.py.
# Stamped on memories where source_type='extracted' (the model decided
# what was worth remembering from a conversation).
#
# Bump when:
#   - the prompt text in _EXTRACTION_PROMPT changes
#   - the JSON schema we ask the model to return changes
#   - the input-conversation slicing (how much context we pass in) changes
EXTRACTION_PROMPT_VERSION = "extraction-v2-2026-05-27"

# Version stamped on memories saved via the explicit user-typed path
# (memory_save MCP tool, /memory POST endpoint). No LLM is involved here,
# so this is effectively "the contract for user-provided memories" — it
# bumps if we change validation, normalisation, or storage semantics
# (NOT if the user happens to type something different).
EXPLICIT_MEMORY_VERSION = "explicit-v1"

# Version stamped on memories produced by the vault-ingestion pipeline
# (services/vault_ingestion.py). Files chunked by anchor, each chunk
# becomes one 'reference' memory. No LLM extraction happens here. Bump
# when the chunking algorithm, anchor strategy, or source_ref format
# changes.
VAULT_INGESTION_VERSION = "vault-ingest-v1"


# ---------------------------------------------------------------------------
# Memory retrieval
# ---------------------------------------------------------------------------

# Version of the retrieval profile in memory_retrieval.py — the algorithm
# AND its parameters as a single unit. Same algorithm with different top_k
# or threshold = different version.
#
# v1 is the current two-tier hybrid:
#   tier 1 — always-relevant: top N//2 preference + instruction memories
#            above min_confidence, ordered by (confidence DESC,
#            last_accessed_at DESC).
#   tier 2 — keyword-match: rest of budget filled from non-preference /
#            non-instruction categories matching keywords extracted from
#            the last 3 user messages (stopword-filtered LIKE).
# A separate semantic_search() path uses cosine ranking against stored
# embeddings; that path stamps the same RETRIEVAL_PROFILE_VERSION because
# it's the same conceptual contract from the caller's perspective. If we
# split them into distinct callable profiles we'll add a second constant.
#
# Bump when:
#   - any tier's algorithm, weighting, or budget split changes
#   - top_k, the score threshold, or any reranking step changes
#   - the stopword list materially changes
#   - we swap the embedding model used at query time (embedding-model
#     drift for stored vectors is already captured separately on
#     memory_embeddings.model_used)
RETRIEVAL_PROFILE_VERSION = "retrieval-v1-2026-05-20"


# ---------------------------------------------------------------------------
# MCP tool schemas
# ---------------------------------------------------------------------------

# Version of the tool-schema surface exposed to MCP clients (the tool
# names, argument shapes, and return types declared in mcp/tools/*.py).
# Stamped on every mcp_tool_calls row so the debugger can show "this
# call was made against tool-schema-v3" and a follow-up regression can
# be tied back to the schema change.
#
# Bump when:
#   - any tool is added, removed, or renamed
#   - any tool's required-argument set changes
#   - any tool's response shape changes
#   - the args_summary whitelist (privacy gate) changes
MCP_TOOL_SCHEMA_VERSION = "mcp-tools-v1-2026-05-20"

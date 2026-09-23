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
#
# v3 (2026-07-17 security audit, MEDIUM): added a rule restricting category
# "instruction" to directives the user typed themselves — never pasted,
# quoted, or copied text — to reduce prompt-injection-via-paste turning into
# a stored "instruction" memory. See RETRIEVAL_PROFILE_VERSION v3 for the
# paired retrieval-side mitigation (auto-extracted instructions no longer
# get always-inject status).
EXTRACTION_PROMPT_VERSION = "extraction-v4-redacted-2026-09-15"

# Version stamped on memories saved via the explicit user-typed path
# (memory_save MCP tool, /memory POST endpoint). No LLM is involved here,
# so this is effectively "the contract for user-provided memories" — it
# bumps if we change validation, normalisation, or storage semantics
# (NOT if the user happens to type something different).
EXPLICIT_MEMORY_VERSION = "explicit-v2-redacted-2026-09-15"

# Version stamped on memories produced by the vault-ingestion pipeline
# (services/vault_ingestion.py). Files chunked by anchor, each chunk
# becomes one 'reference' memory. No LLM extraction happens here. Bump
# when the chunking algorithm, anchor strategy, or source_ref format
# changes.
VAULT_INGESTION_VERSION = "vault-ingest-v2-redacted-2026-09-15"


# ---------------------------------------------------------------------------
# Memory retrieval
# ---------------------------------------------------------------------------

# Version of the retrieval profile in memory_retrieval.py — the algorithm
# AND its parameters as a single unit. Same algorithm with different top_k
# or threshold = different version.
#
# v1 was a two-tier hybrid with keyword-only Tier 2 (retrieval-v1-2026-05-20).
#
# v2 (retrieval-v2-rrf-2026-05-28) adds Reciprocal Rank Fusion to Tier 2:
#   tier 1 — unchanged: top N//2 preference + instruction memories above
#            min_confidence, ordered by (confidence DESC, last_accessed_at DESC).
#   tier 2 — RRF-fused keyword+semantic: keyword LIKE query and
#            semantic_search() (cosine similarity via sentence-transformers
#            all-MiniLM-L6-v2) run in parallel via asyncio.gather; results
#            are fused with RRF (k=60, 1-based ranks), deduped by memory ID,
#            sorted by fused score, top N fills the remaining budget.
#            Graceful degradation: if semantic_search returns [] (no embedder
#            or empty embedding store), RRF reduces to keyword-only ranking —
#            identical to v1 behavior.
#
# v3 (2026-07-17 security audit, MEDIUM): Tier 1 no longer always-injects
#   category='instruction' memories whose source_type='extracted' (i.e.
#   auto-extracted from raw conversation text, which may contain pasted/
#   untrusted content). Tier 1 now injects: all 'preference' memories, plus
#   'instruction' memories only when source_type != 'extracted' (explicit
#   user-confirmed instructions via memory_save). Auto-extracted instructions
#   are still stored and still retrievable through Tier 2 (keyword+semantic,
#   relevance-ranked) like any other memory; pinning (the existing `pinned`
#   opt-in) is the only path back to always-inject status. Preference-tier
#   behavior and Tier 2/RRF are unchanged.
#
# Bump when:
#   - any tier's algorithm, weighting, or budget split changes
#   - top_k, the score threshold, or any reranking step changes
#   - the stopword list materially changes
#   - we swap the embedding model used at query time (embedding-model
#     drift for stored vectors is already captured separately on
#     memory_embeddings.model_used)
RETRIEVAL_PROFILE_VERSION = "retrieval-v3-2026-07-17"

# Local chat opts into relevance-first retrieval: explicit pins, then all
# categories ranked against the latest user turn, with a semantic relevance
# floor. MCP keeps its existing ambient instruction contract.
CHAT_CONTEXT_VERSION = "chat-context-v1-relevance-2026-09-15"


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

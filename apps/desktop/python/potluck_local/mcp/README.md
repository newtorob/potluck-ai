# Potluck MCP Server

A local MCP server that exposes Potluck's persistent memory and task-state layer to external coding agents (Claude Code, Cursor, Zed, Windsurf, Continue).

This binary does **not** call the LLM. It is a thin layer over the shared SQLite database at `~/.potluck/data/memory.db`. The FastAPI sidecar and the MCP server are separate processes that share state through that file.

## Running in development

```sh
# stdio (for Claude Code and other CLI agents)
python -m potluck_local.mcp --stdio

# SSE over local TCP (for GUI agents that prefer long-lived connections).
# Requires POTLUCK_MCP_TOKEN (or POTLUCK_SIDECAR_TOKEN) to be set -- the
# TCP/SSE listener has no other caller authentication, so the process
# refuses to start without a token. Every request must send
# `Authorization: Bearer <token>`.
POTLUCK_MCP_TOKEN=some-secret python -m potluck_local.mcp --tcp 8322
```

## Claude Code config

Add to `~/.claude.json` (or your project-level `.mcp.json`):

```json
{
  "mcpServers": {
    "potluck": {
      "command": "/Applications/Potluck AI.app/Contents/Resources/python/potluck-mcp/potluck-mcp",
      "args": ["--stdio"]
    }
  }
}
```

## Tools (13 total)

### Memory (5 tools)

| Tool | Description |
|------|-------------|
| `memory_search` | Ranked retrieval for a natural-language query. See "How search ranks results" below. |
| `memory_save` | Explicitly persist a fact, preference, decision, project note, or instruction. |
| `memory_list` | Browse active memories, optionally filtered by category or project. `since` (an ISO-8601 timestamp) returns only memories updated after that time, and `include_all` ignores the project filter. |
| `memory_delete` | Permanently delete a memory and its embedding by ID. The row is removed, not flagged inactive. |
| `memory_pin` | Pin or unpin a memory by ID. A pinned memory is always included in retrieval, whatever the query. |

Memory categories: `preference`, `fact`, `decision`, `project`, `person`, `instruction`, `reference` (ingested external content)

`memory_search`, `memory_save` and `memory_list` take an optional `project`. With no project they work on global memories. With a project name, search and list return that project's memories plus the global ones, and save stores the memory under that project.

### Task State (6 tools)

| Tool | Description |
|------|-------------|
| `task_create` | Create a new agent task with objective, constraints, and pending steps. |
| `task_get` | Fetch a task by ID. |
| `task_list` | List tasks; optional status filter (`active`, `paused`, `completed`, `abandoned`) and project filter. |
| `task_update` | Partial update of any mutable task field. |
| `task_complete_step` | Move a step from `pending_steps` to `completed_steps`. |
| `task_add_error` | Append an error record (with optional resolution note) to `errors_encountered`. |

### Session metadata (2 tools)

| Tool | Description |
|------|-------------|
| `session_set_model` | Report which model is driving the agent session, so the desktop app can show it. |
| `session_register_mcps` | Report the names of the other MCP servers the agent has configured. Metadata only: Potluck never contacts them. |

These two write to the agent-observability record for the current MCP session. They do not touch memory content.

### How search ranks results

`memory_search` builds its result list in this order, up to `max_results` (default 20):

1. Pinned memories in scope, always.
2. Unless a `category` is given, up to three of the highest-confidence preferences and explicitly saved instructions. Instructions that were extracted automatically from conversation do not get this slot.
3. The remaining slots, filled by reciprocal rank fusion (k = 60) of two rankings: a keyword ranking (memories that match words from the query, ordered by confidence) and a semantic ranking (cosine similarity between embeddings of the query and the stored memories).

Only memories with confidence at or above `min_confidence` (default 0.5) and inside the project scope are considered. If the embedding model cannot load in the MCP process, the semantic ranking is empty and the keyword ranking alone fills the slots.

## Shared SQLite path

Default: `~/.potluck/data/memory.db`

Override with the `POTLUCK_MEMORY_DB_PATH` environment variable.

Both the FastAPI sidecar and this MCP server open the same file. SQLite's WAL mode handles concurrent reads/writes without coordination overhead.

## Privacy

Memory tools respect the same privacy gate as the HTTP router. Both `privacy.memory_enabled` and `memory.enabled` in `~/.potluck/config.json` must be true. When either is false, `memory_search` and `memory_list` return empty results, and `memory_save`, `memory_delete` and `memory_pin` return an error saying memory is disabled. Task and session tools are unaffected by the memory privacy toggle.

## No language model

The MCP server never loads a language model. The packaged `potluck-mcp` binary excludes `llama_cpp`, `torch` and `sentence_transformers` (see `potluck-mcp.spec`), so it cannot load the embedding model either, and its searches use the keyword ranking only. In a development environment that has `sentence-transformers` installed, searches also use semantic similarity over stored embeddings. Extraction (converting conversation text into memories) happens inside the FastAPI chat pipeline, not here.

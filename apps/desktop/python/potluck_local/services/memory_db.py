"""SQLite memory database — initialisation, schema, and path helpers.

All other services import get_memory_db_path() and call init_memory_db()
on startup; they must never create the schema themselves.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import aiosqlite

from potluck_local.utils.time import now_iso as _now_iso

logger = logging.getLogger(__name__)

DB_PATH_ENV = "POTLUCK_MEMORY_DB_PATH"

_CREATE_MEMORIES = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.7,
    source_type TEXT NOT NULL,
    source_conversation_id TEXT,
    created_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
)
"""

_CREATE_MEMORY_EMBEDDINGS = """
CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id TEXT PRIMARY KEY REFERENCES memories(id),
    embedding BLOB NOT NULL,
    model_used TEXT NOT NULL
)
"""

_CREATE_TASK_STATES = """
CREATE TABLE IF NOT EXISTS task_states (
    id TEXT PRIMARY KEY,
    task_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    objective TEXT NOT NULL,
    completed_steps TEXT NOT NULL DEFAULT '[]',
    pending_steps TEXT NOT NULL DEFAULT '[]',
    known_constraints TEXT NOT NULL DEFAULT '[]',
    context_summary TEXT,
    files_touched TEXT NOT NULL DEFAULT '[]',
    errors_encountered TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    conversation_id TEXT
)
"""

_CREATE_MEMORY_ACCESS_LOG = """
CREATE TABLE IF NOT EXISTS memory_access_log (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memories(id),
    conversation_id TEXT NOT NULL,
    was_useful INTEGER,
    accessed_at TEXT NOT NULL
)
"""

_CREATE_VAULT_FILES = """
CREATE TABLE IF NOT EXISTS vault_files (
    file_path TEXT PRIMARY KEY,
    content_sha TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    last_ingested_at TEXT NOT NULL
)
"""

# Chat conversations + messages — local-first history. Lives in the same
# SQLite as memories/tasks because the chat surface is conceptually
# adjacent (extracted memories come from these same conversations).
# Cross-device sync is v0.5+ work; for now everything stays on the user's
# machine, in line with the manifesto's local-first promise.
_CREATE_CONVERSATIONS = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    project TEXT,
    network_mode INTEGER NOT NULL DEFAULT 0,
    network_model_slug TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'complete',
    served_by TEXT,
    output_tokens INTEGER,
    duration_ms INTEGER,
    finish_reason TEXT,
    created_at TEXT NOT NULL
)
"""

_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)",
    "CREATE INDEX IF NOT EXISTS idx_memories_active ON memories(is_active, confidence DESC)",
    "CREATE INDEX IF NOT EXISTS idx_memories_accessed ON memories(last_accessed_at)",
    "CREATE INDEX IF NOT EXISTS idx_task_states_status ON task_states(status)",
    "CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_task_states_project ON task_states(project, status)",
    "CREATE INDEX IF NOT EXISTS idx_memories_source_type ON memories(source_type, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_memories_source_ref ON memories(source_ref)",
    "CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories(updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_messages_convo ON messages(conversation_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations(project, updated_at DESC)",
]


def get_memory_db_path() -> Path:
    """Return the memory DB path, honouring POTLUCK_MEMORY_DB_PATH if set."""
    env_override = os.environ.get(DB_PATH_ENV)
    if env_override:
        return Path(env_override)
    from potluck_local.utils.paths import memory_db_path
    return memory_db_path()


# Explicit allowlist of (table, column, sql_type) triples that
# _add_column_if_missing is permitted to create.  All values are hardcoded
# string literals — nothing here comes from external input.  If a caller
# passes a combination not in this set the function raises immediately so
# the mistake is caught at development time rather than silently allowing
# arbitrary DDL.
_ALLOWED_SCHEMA_ADDITIONS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("memories", "project", "TEXT"),
        ("task_states", "project", "TEXT"),
        ("memories", "source_ref", "TEXT"),
        ("mcp_sessions", "friendly_name", "TEXT"),
        ("memories", "extraction_prompt_version", "TEXT"),
        ("memory_access_log", "retrieval_profile_version", "TEXT"),
        ("mcp_tool_calls", "tool_schema_version", "TEXT"),
        ("mcp_tool_calls", "memory_ids_touched", "TEXT"),
        # updated_at enables incremental sync, agent delta queries, and
        # export diffing. ISO-8601 TEXT matches the rest of the schema.
        ("memories", "updated_at", "TEXT"),
        # pinned: standing/ambient memories that are ALWAYS injected into
        # retrieval regardless of query (the explicit "pinned instructions"
        # mechanism the always_budget heuristic only approximated).
        ("memories", "pinned", "INTEGER NOT NULL DEFAULT 0"),
    }
)


def _add_column_if_missing(
    conn: aiosqlite.Connection,
    table: str,
    column: str,
    sql_type: str,
) -> Coroutine[Any, Any, None]:
    """Return an awaitable that adds `column` to `table` only when absent.

    Uses PRAGMA table_info to check — cleaner than catching OperationalError
    on duplicate-column, which masks real errors.

    Only (table, column, sql_type) combinations listed in
    _ALLOWED_SCHEMA_ADDITIONS are permitted.  This prevents the f-string
    DDL interpolation from becoming a latent injection vector if a future
    caller ever passes a non-literal value.
    """
    if (table, column, sql_type) not in _ALLOWED_SCHEMA_ADDITIONS:
        raise ValueError(
            f"_add_column_if_missing: ({table!r}, {column!r}, {sql_type!r}) "
            "is not in the allowlist — add it to _ALLOWED_SCHEMA_ADDITIONS "
            "with a hardcoded literal to proceed."
        )

    # We return a coroutine so callers can await it.
    async def _run() -> None:
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        existing = {row[1] for row in rows}  # index 1 = column name
        if column not in existing:
            await conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
            )

    return _run()


async def init_memory_db(db_path: Path | None = None) -> None:
    """Create the DB file and all tables/indices if missing. Idempotent."""
    path = db_path or get_memory_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(path) as db:
        # WAL mode: allows concurrent readers + one writer without blocking.
        # Both the FastAPI sidecar and the MCP server process open this file
        # independently; without WAL, simultaneous writes produce "database is
        # locked" errors.  WAL is stored in the file header so this is a
        # one-time flip — subsequent calls are no-ops.
        await db.execute("PRAGMA journal_mode=WAL")
        # NORMAL sync is the correct companion for WAL: safe (no data loss on
        # crash), and ~3x faster than FULL because WAL checkpoints are the
        # real durability boundary.
        await db.execute("PRAGMA synchronous=NORMAL")
        # Return gracefully on cross-process lock contention instead of
        # immediately raising "database is locked".
        await db.execute("PRAGMA busy_timeout=5000")

        # Verify WAL actually took effect — SQLite ignores the PRAGMA if it
        # can't switch (e.g. in-memory DB or read-only filesystem).
        cursor = await db.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        actual_mode = row[0] if row else "unknown"
        if actual_mode != "wal":
            logger.warning("memory_db_wal_not_enabled actual_mode=%s path=%s", actual_mode, path)
        else:
            logger.info("memory_db_wal_enabled path=%s", path)

        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(_CREATE_MEMORIES)
        await db.execute(_CREATE_MEMORY_EMBEDDINGS)
        await db.execute(_CREATE_TASK_STATES)
        await db.execute(_CREATE_MEMORY_ACCESS_LOG)
        await db.execute(_CREATE_MCP_SESSIONS)
        await db.execute(_CREATE_MCP_TOOL_CALLS)
        await db.execute(_CREATE_VAULT_FILES)
        await db.execute(_CREATE_CONVERSATIONS)
        await db.execute(_CREATE_MESSAGES)

        # Schema migrations first — indexes below may reference added columns.
        await _add_column_if_missing(db, "memories", "project", "TEXT")
        await _add_column_if_missing(db, "task_states", "project", "TEXT")
        # source_ref carries the originating identifier for ingested content,
        # e.g. "vault:/path/to/notes.md#heading-anchor" for vault chunks.
        await _add_column_if_missing(db, "memories", "source_ref", "TEXT")
        # friendly_name is an adjective+animal nickname assigned at session
        # create, used to disambiguate multiple instances of the same client
        # in the AgentsView (e.g. two Claude Code processes running side by
        # side). Persisted so the same session keeps the same nickname across
        # AgentsView refreshes; never reused after a session is deleted.
        await _add_column_if_missing(db, "mcp_sessions", "friendly_name", "TEXT")

        # updated_at: records every write mutation (insert, update, soft-delete,
        # dedup-bump). Enables incremental sync ("what changed since X"),
        # export diffing, and agent delta queries.  ISO-8601 TEXT to match
        # the rest of the schema. Backfill sets updated_at = created_at for
        # all existing rows where the column was just added (NULLs only).
        await _add_column_if_missing(db, "memories", "updated_at", "TEXT")
        await db.execute(
            "UPDATE memories SET updated_at = created_at WHERE updated_at IS NULL"
        )

        # AI contract versions (services/ai_contracts.py). Stamped at write
        # time so the debugger can answer "what prompt extracted this memory
        # / ranked this retrieval / shaped this tool call?" even after the
        # underlying contract is bumped. NULL on rows older than 2026-05-20
        # (pre-versioning era); the debugger renders that honestly rather
        # than backfilling and lying about provenance.
        await _add_column_if_missing(db, "memories", "extraction_prompt_version", "TEXT")
        await _add_column_if_missing(db, "memory_access_log", "retrieval_profile_version", "TEXT")
        await _add_column_if_missing(db, "mcp_tool_calls", "tool_schema_version", "TEXT")

        # Memory-IDs each tool call read or wrote, JSON-encoded array of
        # strings. Populated by AgentObserver.log_tool_call when callers know
        # the affected ids (memory_save / memory_search / memory_delete /
        # memory_list). NULL when not applicable or pre-debugger rows. The
        # debugger UI joins this with `memories` so a tool call expands to
        # show the previews of the memories it touched.
        await _add_column_if_missing(db, "mcp_tool_calls", "memory_ids_touched", "TEXT")

        # pinned: 1 = always-inject this memory into retrieval regardless of
        # query (ambient/standing context). Defaults 0 for all existing rows.
        await _add_column_if_missing(db, "memories", "pinned", "INTEGER NOT NULL DEFAULT 0")

        for stmt in _INDICES:
            await db.execute(stmt)

        for stmt in _OBSERVABILITY_INDICES:
            await db.execute(stmt)

        await db.commit()

    logger.info("memory_db_ready path=%s journal_mode=wal", path)


async def reset_memory_db(db_path: Path | None = None) -> None:
    """Delete the DB file and re-initialise. Destructive — for tests/wipe-all."""
    path = db_path or get_memory_db_path()
    if path.exists():
        path.unlink()
        logger.info("memory_db_deleted path=%s", path)
    await init_memory_db(path)


_CREATE_MCP_SESSIONS = """
CREATE TABLE IF NOT EXISTS mcp_sessions (
  id TEXT PRIMARY KEY,
  client_name TEXT,
  client_version TEXT,
  transport TEXT NOT NULL,
  pid INTEGER,
  project TEXT,
  model TEXT,
  model_source TEXT,
  peer_mcps TEXT NOT NULL DEFAULT '[]',
  started_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  disconnected_at TEXT
)
"""

_CREATE_MCP_TOOL_CALLS = """
CREATE TABLE IF NOT EXISTS mcp_tool_calls (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES mcp_sessions(id),
  tool_name TEXT NOT NULL,
  args_summary TEXT,
  result_count INTEGER,
  duration_ms INTEGER,
  error TEXT,
  called_at TEXT NOT NULL
)
"""

_OBSERVABILITY_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_mcp_sessions_active ON mcp_sessions(disconnected_at, last_seen_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mcp_sessions_project ON mcp_sessions(project)",
    "CREATE INDEX IF NOT EXISTS idx_mcp_tool_calls_session ON mcp_tool_calls(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_mcp_tool_calls_called_at ON mcp_tool_calls(called_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mcp_tool_calls_tool_name ON mcp_tool_calls(tool_name)",
]


async def list_sessions(
    db_path: Path | None = None,
    include_disconnected: bool = False,
    limit: int = 100,
) -> list[dict]:
    """Return sessions with tool_call_count joined; ordered by last_seen_at desc."""
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        where = "" if include_disconnected else "WHERE s.disconnected_at IS NULL"
        cursor = await db.execute(
            f"""
            SELECT
                s.id, s.client_name, s.client_version, s.transport, s.pid,
                s.project, s.model, s.model_source, s.peer_mcps,
                s.started_at, s.last_seen_at, s.disconnected_at,
                s.friendly_name,
                COUNT(t.id) AS tool_call_count
            FROM mcp_sessions s
            LEFT JOIN mcp_tool_calls t ON t.session_id = s.id
            {where}
            GROUP BY s.id
            ORDER BY s.last_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_session(
    session_id: str,
    db_path: Path | None = None,
) -> dict | None:
    """Return a single session row with tool_call_count, or None."""
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                s.id, s.client_name, s.client_version, s.transport, s.pid,
                s.project, s.model, s.model_source, s.peer_mcps,
                s.started_at, s.last_seen_at, s.disconnected_at,
                s.friendly_name,
                COUNT(t.id) AS tool_call_count
            FROM mcp_sessions s
            LEFT JOIN mcp_tool_calls t ON t.session_id = s.id
            WHERE s.id = ?
            GROUP BY s.id
            """,
            (session_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def list_recent_tool_calls(
    *,
    db_path: Path | None = None,
    limit: int = 100,
    client: str | None = None,
    project: str | None = None,
    tool_name: str | None = None,
) -> list[dict]:
    """Return recent tool calls with optional filters; ordered called_at desc."""
    path = db_path or get_memory_db_path()
    conditions: list[str] = []
    params: list[object] = []
    if client is not None:
        conditions.append("s.client_name = ?")
        params.append(client)
    if project is not None:
        conditions.append("s.project = ?")
        params.append(project)
    if tool_name is not None:
        conditions.append("t.tool_name = ?")
        params.append(tool_name)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT
                t.id, t.session_id, t.tool_name, t.args_summary,
                t.result_count, t.duration_ms, t.error, t.called_at,
                t.tool_schema_version, t.memory_ids_touched
            FROM mcp_tool_calls t
            JOIN mcp_sessions s ON s.id = t.session_id
            {where}
            ORDER BY t.called_at DESC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def session_tool_calls(
    session_id: str,
    limit: int = 200,
    db_path: Path | None = None,
) -> list[dict]:
    """Return tool calls for a single session; ordered called_at desc.

    Includes tool_schema_version so the debugger can render which contract
    version each call ran under (NULL for pre-2026-05-20 rows).
    """
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, session_id, tool_name, args_summary,
                   result_count, duration_ms, error, called_at,
                   tool_schema_version, memory_ids_touched
            FROM mcp_tool_calls
            WHERE session_id = ?
            ORDER BY called_at DESC
            LIMIT ?
            """,
            (session_id, limit),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def delete_session(
    session_id: str,
    db_path: Path | None = None,
) -> bool:
    """Delete one mcp_sessions row + all its mcp_tool_calls.

    Returns True if a row was deleted, False if the id wasn't found.
    Tool calls are deleted first because the FK on mcp_tool_calls.session_id
    is non-cascading (and PRAGMA foreign_keys=ON would otherwise reject the
    parent delete).
    """
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            "DELETE FROM mcp_tool_calls WHERE session_id = ?", (session_id,)
        )
        cursor = await db.execute(
            "DELETE FROM mcp_sessions WHERE id = ?", (session_id,)
        )
        deleted = cursor.rowcount > 0
        await db.commit()
    return deleted


async def delete_disconnected_sessions(
    db_path: Path | None = None,
) -> int:
    """Delete every mcp_sessions row whose disconnected_at is set, plus all
    of their tool_calls. Returns the number of session rows removed.

    Does not touch active sessions (disconnected_at IS NULL); the user may
    have agents that are momentarily silent but still meant to be there.
    """
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            """
            DELETE FROM mcp_tool_calls
            WHERE session_id IN (
                SELECT id FROM mcp_sessions WHERE disconnected_at IS NOT NULL
            )
            """
        )
        cursor = await db.execute(
            "DELETE FROM mcp_sessions WHERE disconnected_at IS NOT NULL"
        )
        removed = cursor.rowcount
        await db.commit()
    return removed


async def save_embedding(
    memory_id: str,
    vector: "np.ndarray",
    model_name: str,
    db_path: Path | None = None,
) -> None:
    """Persist a float32 embedding vector for a memory row.

    Uses INSERT OR REPLACE so re-embedding with a new model version overwrites
    the old row cleanly.
    """
    import numpy as np  # local import — numpy is a dep but not needed at module load

    path = db_path or get_memory_db_path()
    blob = np.array(vector, dtype=np.float32).tobytes()
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            """INSERT OR REPLACE INTO memory_embeddings (memory_id, embedding, model_used)
               VALUES (?, ?, ?)""",
            (memory_id, blob, model_name),
        )
        await db.commit()


async def get_embedding(
    memory_id: str,
    db_path: Path | None = None,
) -> "tuple[np.ndarray, str] | None":
    """Return (vector, model_name) for a memory, or None if not yet embedded."""
    import numpy as np

    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT embedding, model_used FROM memory_embeddings WHERE memory_id = ?",
            (memory_id,),
        )
        row = await cursor.fetchone()

    if row is None:
        return None
    vec = np.frombuffer(row["embedding"], dtype=np.float32).reshape(-1)
    return vec, row["model_used"]


def project_clause(project: str | None, alias: str = "") -> "tuple[str, list]":
    """Return (SQL fragment, params) for project-scoped filtering.

    project given -> rows where project = X OR project IS NULL.
    project None  -> only global rows (project IS NULL).

    `alias` prefixes the column for JOIN queries, e.g. alias="m." emits
    "(m.project = ? OR m.project IS NULL)".
    """
    col = f"{alias}project"
    if project is not None:
        return f"({col} = ? OR {col} IS NULL)", [project]
    return f"{col} IS NULL", []


async def load_all_embeddings(
    project: str | None = None,
    model: str | None = None,
    db_path: Path | None = None,
) -> "list[tuple[str, np.ndarray]]":
    """Return [(memory_id, vector)] for all active memories that have embeddings.

    project filter: when project is given, loads embeddings for memories where
    project = X OR project IS NULL (same scoping as retrieval).
    When project is None, loads embeddings for global memories only (project IS NULL).

    model filter: when `model` is given, only embeddings produced by that model
    are returned. This is required — without it, a table containing vectors from
    two embedding models (after a model switch / partial backfill) would be
    stacked into one matrix with inconsistent dimensions and crash retrieval.
    """
    import numpy as np

    path = db_path or get_memory_db_path()

    proj_clause, proj_params = project_clause(project, alias="m.")

    model_clause = ""
    if model is not None:
        model_clause = " AND e.model_used = ?"
        proj_params = proj_params + [model]

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""SELECT e.memory_id, e.embedding
                FROM memory_embeddings e
                JOIN memories m ON m.id = e.memory_id
                WHERE m.is_active = 1
                  AND {proj_clause}{model_clause}""",
            proj_params,
        )
        rows = await cursor.fetchall()

    result: list[tuple[str, np.ndarray]] = []
    for row in rows:
        vec = np.frombuffer(row["embedding"], dtype=np.float32).reshape(-1)
        result.append((row["memory_id"], vec))
    return result


async def memories_missing_embeddings(
    model: str | None = None,
    db_path: Path | None = None,
) -> list[str]:
    """Return IDs of active memories that need (re)embedding.

    Without `model`: memories that have no embedding row at all.
    With `model`: also includes memories whose existing embedding was produced
    by a *different* model — i.e. stale rows after an embedding-model switch.
    save_embedding uses INSERT OR REPLACE, so re-embedding these overwrites the
    stale row in place. This is what makes a model switch self-healing.
    """
    path = db_path or get_memory_db_path()
    if model is not None:
        where = "m.is_active = 1 AND (e.memory_id IS NULL OR e.model_used != ?)"
        params: list = [model]
    else:
        where = "m.is_active = 1 AND e.memory_id IS NULL"
        params = []
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            f"""SELECT m.id FROM memories m
               LEFT JOIN memory_embeddings e ON e.memory_id = m.id
               WHERE {where}""",
            params,
        )
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def list_projects(db_path: Path | None = None) -> list[dict]:
    """Return [{project, memory_count, task_count}] for all named projects.

    NULL and empty-string project values are excluded — only surfaced named
    projects are returned. Used by GET /memory/projects.
    """
    path = db_path or get_memory_db_path()

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT project, COUNT(*) AS memory_count
            FROM memories
            WHERE project IS NOT NULL AND project != '' AND is_active = 1
            GROUP BY project
            """
        )
        memory_rows = {row["project"]: row["memory_count"] for row in await cursor.fetchall()}

        cursor = await db.execute(
            """
            SELECT project, COUNT(*) AS task_count
            FROM task_states
            WHERE project IS NOT NULL AND project != ''
            GROUP BY project
            """
        )
        task_rows = {row["project"]: row["task_count"] for row in await cursor.fetchall()}

    all_projects = sorted(set(memory_rows) | set(task_rows))
    return [
        {
            "project": p,
            "memory_count": memory_rows.get(p, 0),
            "task_count": task_rows.get(p, 0),
        }
        for p in all_projects
    ]


# ---------------------------------------------------------------------------
# Chat history — conversations + messages
# ---------------------------------------------------------------------------


async def list_conversations(
    project: str | None = None,
    include_all: bool = False,
    limit: int = 200,
    db_path: Path | None = None,
) -> list[dict]:
    """Return conversations ordered by most-recently-updated.

    Project filtering matches the rest of the app (see memory router):
    - include_all=True   → ignore project, return everything
    - project=None       → only conversations with project IS NULL
    - project='foo'      → conversations where project='foo' OR project IS NULL
    """
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        if include_all:
            where = ""
            params: tuple = (limit,)
        elif project is None:
            where = "WHERE project IS NULL"
            params = (limit,)
        else:
            where = "WHERE project = ? OR project IS NULL"
            params = (project, limit)

        cursor = await db.execute(
            f"""
            SELECT c.id, c.title, c.project, c.network_mode, c.network_model_slug,
                   c.created_at, c.updated_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count
            FROM conversations c
            {where}
            ORDER BY c.updated_at DESC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_conversation(
    conversation_id: str,
    db_path: Path | None = None,
) -> dict | None:
    """Return a conversation with its messages, ordered by created_at."""
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        c_cursor = await db.execute(
            """
            SELECT id, title, project, network_mode, network_model_slug,
                   created_at, updated_at
            FROM conversations
            WHERE id = ?
            """,
            (conversation_id,),
        )
        c_row = await c_cursor.fetchone()
        if c_row is None:
            return None

        m_cursor = await db.execute(
            """
            SELECT id, conversation_id, role, content, status,
                   served_by, output_tokens, duration_ms, finish_reason, created_at
            FROM messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC
            """,
            (conversation_id,),
        )
        m_rows = await m_cursor.fetchall()

    convo = dict(c_row)
    convo["messages"] = [dict(r) for r in m_rows]
    return convo


async def create_conversation(
    conversation_id: str,
    title: str,
    project: str | None,
    network_mode: bool,
    network_model_slug: str | None,
    db_path: Path | None = None,
) -> dict:
    """Insert a new conversation row. Caller-supplied id (UUID) so the
    frontend can optimistically use it before the round-trip completes."""
    now = _now_iso()
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            """
            INSERT INTO conversations
                (id, title, project, network_mode, network_model_slug, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (conversation_id, title, project, 1 if network_mode else 0,
             network_model_slug, now, now),
        )
        await db.commit()
    return {
        "id": conversation_id,
        "title": title,
        "project": project,
        "network_mode": 1 if network_mode else 0,
        "network_model_slug": network_model_slug,
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
    }


async def update_conversation(
    conversation_id: str,
    title: str | None = None,
    network_mode: bool | None = None,
    network_model_slug: str | None = None,
    project: str | None = None,
    project_set: bool = False,  # distinguish "unchanged" from "set to NULL"
    db_path: Path | None = None,
) -> bool:
    """Patch a conversation. Returns True if the row was found."""
    sets: list[str] = []
    params: list = []
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if network_mode is not None:
        sets.append("network_mode = ?")
        params.append(1 if network_mode else 0)
    if network_model_slug is not None:
        sets.append("network_model_slug = ?")
        params.append(network_model_slug)
    if project_set:
        sets.append("project = ?")
        params.append(project)

    if not sets:
        return True  # noop is idempotent-success

    sets.append("updated_at = ?")
    params.append(_now_iso())
    params.append(conversation_id)

    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            f"UPDATE conversations SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        found = cursor.rowcount > 0
        await db.commit()
    return found


async def delete_conversation(
    conversation_id: str,
    db_path: Path | None = None,
) -> bool:
    """Delete a conversation + cascade its messages.

    PRAGMA foreign_keys=ON is set so the ON DELETE CASCADE on
    messages.conversation_id fires. Without that pragma the cascade
    silently doesn't run and orphan messages accumulate.
    """
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        cursor = await db.execute(
            "DELETE FROM conversations WHERE id = ?", (conversation_id,)
        )
        deleted = cursor.rowcount > 0
        await db.commit()
    return deleted


async def append_message(
    message_id: str,
    conversation_id: str,
    role: str,
    content: str,
    status: str = "complete",
    served_by: str | None = None,
    output_tokens: int | None = None,
    duration_ms: int | None = None,
    finish_reason: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Append a message + bump the conversation's updated_at."""
    now = _now_iso()
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            """
            INSERT INTO messages
                (id, conversation_id, role, content, status, served_by,
                 output_tokens, duration_ms, finish_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, conversation_id, role, content, status,
             served_by, output_tokens, duration_ms, finish_reason, now),
        )
        await db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        await db.commit()
    return {
        "id": message_id,
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "status": status,
        "served_by": served_by,
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "finish_reason": finish_reason,
        "created_at": now,
    }


async def update_message(
    message_id: str,
    content: str | None = None,
    status: str | None = None,
    served_by: str | None = None,
    output_tokens: int | None = None,
    duration_ms: int | None = None,
    finish_reason: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """Patch an existing message. Used to finalize an assistant message
    after streaming completes (status transitions from 'streaming' to
    'complete', and meta fields land if the response came from the
    contributor network)."""
    sets: list[str] = []
    params: list = []
    if content is not None:
        sets.append("content = ?")
        params.append(content)
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if served_by is not None:
        sets.append("served_by = ?")
        params.append(served_by)
    if output_tokens is not None:
        sets.append("output_tokens = ?")
        params.append(output_tokens)
    if duration_ms is not None:
        sets.append("duration_ms = ?")
        params.append(duration_ms)
    if finish_reason is not None:
        sets.append("finish_reason = ?")
        params.append(finish_reason)

    if not sets:
        return True

    params.append(message_id)
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            f"UPDATE messages SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        found = cursor.rowcount > 0
        await db.commit()
    return found


async def clear_conversation_messages(
    conversation_id: str,
    db_path: Path | None = None,
) -> int:
    """Drop all messages in a conversation. Returns count deleted."""
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            "DELETE FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        )
        removed = cursor.rowcount
        await db.commit()
    return removed

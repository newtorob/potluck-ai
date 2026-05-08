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

_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)",
    "CREATE INDEX IF NOT EXISTS idx_memories_active ON memories(is_active, confidence DESC)",
    "CREATE INDEX IF NOT EXISTS idx_memories_accessed ON memories(last_accessed_at)",
    "CREATE INDEX IF NOT EXISTS idx_task_states_status ON task_states(status)",
    "CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_task_states_project ON task_states(project, status)",
    "CREATE INDEX IF NOT EXISTS idx_memories_source_type ON memories(source_type, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_memories_source_ref ON memories(source_ref)",
]


def get_memory_db_path() -> Path:
    """Return the memory DB path, honouring POTLUCK_MEMORY_DB_PATH if set."""
    env_override = os.environ.get(DB_PATH_ENV)
    if env_override:
        return Path(env_override)
    from potluck_local.utils.paths import memory_db_path
    return memory_db_path()


def _add_column_if_missing(
    conn: aiosqlite.Connection,
    table: str,
    column: str,
    sql_type: str,
) -> Coroutine[Any, Any, None]:
    """Return an awaitable that adds `column` to `table` only when absent.

    Uses PRAGMA table_info to check — cleaner than catching OperationalError
    on duplicate-column, which masks real errors.
    """
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
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(_CREATE_MEMORIES)
        await db.execute(_CREATE_MEMORY_EMBEDDINGS)
        await db.execute(_CREATE_TASK_STATES)
        await db.execute(_CREATE_MEMORY_ACCESS_LOG)
        await db.execute(_CREATE_MCP_SESSIONS)
        await db.execute(_CREATE_MCP_TOOL_CALLS)
        await db.execute(_CREATE_VAULT_FILES)

        # Schema migrations first — indexes below may reference added columns.
        await _add_column_if_missing(db, "memories", "project", "TEXT")
        await _add_column_if_missing(db, "task_states", "project", "TEXT")
        # source_ref carries the originating identifier for ingested content,
        # e.g. "vault:/path/to/notes.md#heading-anchor" for vault chunks.
        await _add_column_if_missing(db, "memories", "source_ref", "TEXT")

        for stmt in _INDICES:
            await db.execute(stmt)

        for stmt in _OBSERVABILITY_INDICES:
            await db.execute(stmt)

        await db.commit()

    logger.info("memory_db_ready path=%s", path)


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
                t.result_count, t.duration_ms, t.error, t.called_at
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
    """Return tool calls for a single session; ordered called_at desc."""
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, session_id, tool_name, args_summary,
                   result_count, duration_ms, error, called_at
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


async def load_all_embeddings(
    project: str | None = None,
    db_path: Path | None = None,
) -> "list[tuple[str, np.ndarray]]":
    """Return [(memory_id, vector)] for all active memories that have embeddings.

    project filter: when project is given, loads embeddings for memories where
    project = X OR project IS NULL (same scoping as retrieval).
    When project is None, loads embeddings for global memories only (project IS NULL).
    """
    import numpy as np

    path = db_path or get_memory_db_path()

    if project is not None:
        proj_clause = "(m.project = ? OR m.project IS NULL)"
        proj_params: list = [project]
    else:
        proj_clause = "m.project IS NULL"
        proj_params = []

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""SELECT e.memory_id, e.embedding
                FROM memory_embeddings e
                JOIN memories m ON m.id = e.memory_id
                WHERE m.is_active = 1
                  AND {proj_clause}""",
            proj_params,
        )
        rows = await cursor.fetchall()

    result: list[tuple[str, np.ndarray]] = []
    for row in rows:
        vec = np.frombuffer(row["embedding"], dtype=np.float32).reshape(-1)
        result.append((row["memory_id"], vec))
    return result


async def memories_missing_embeddings(
    db_path: Path | None = None,
) -> list[str]:
    """Return IDs of active memories that have no entry in memory_embeddings."""
    path = db_path or get_memory_db_path()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            """SELECT m.id FROM memories m
               LEFT JOIN memory_embeddings e ON e.memory_id = m.id
               WHERE m.is_active = 1 AND e.memory_id IS NULL"""
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

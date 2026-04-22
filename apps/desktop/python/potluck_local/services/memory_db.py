"""SQLite memory database — initialisation, schema, and path helpers.

All other services import get_memory_db_path() and call init_memory_db()
on startup; they must never create the schema themselves.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

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

_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)",
    "CREATE INDEX IF NOT EXISTS idx_memories_active ON memories(is_active, confidence DESC)",
    "CREATE INDEX IF NOT EXISTS idx_memories_accessed ON memories(last_accessed_at)",
    "CREATE INDEX IF NOT EXISTS idx_task_states_status ON task_states(status)",
]


def get_memory_db_path() -> Path:
    """Return the memory DB path, honouring POTLUCK_MEMORY_DB_PATH if set."""
    env_override = os.environ.get(DB_PATH_ENV)
    if env_override:
        return Path(env_override)
    from potluck_local.utils.paths import memory_db_path
    return memory_db_path()


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
        for stmt in _INDICES:
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

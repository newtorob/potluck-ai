"""POST /memory/wipe must clear memories without touching other data.

reset_memory_db() deletes the entire SQLite file backing memory.db - including
task_states (tasks), conversations + messages (chat history), vault_files
(vault), and mcp_sessions/mcp_tool_calls (agent observability). A memory wipe
must not take those with it.

wipe_memory_tables() deletes only memories, memory_embeddings, and
memory_access_log, in one transaction, leaving every other table intact.
/memory/wipe calls that instead of reset_memory_db().

Isolated via POTLUCK_MEMORY_DB_PATH so this test can never touch a real
~/.potluck/data/memory.db.
"""
from __future__ import annotations

import asyncio
import uuid

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app
from potluck_local.services.memory_db import init_memory_db, wipe_memory_tables
from potluck_local.utils.time import now_iso


def _run(coro):
    return asyncio.run(coro)


async def _seed(db_path) -> None:
    """One row in every table a memory wipe must not touch, plus rows in the
    tables it must clear."""
    now = now_iso()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")

        # In-scope: memory data.
        mem_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO memories
               (id, content, category, confidence, source_type,
                created_at, last_accessed_at, updated_at, access_count, is_active)
               VALUES (?, 'a private memory', 'fact', 0.9, 'explicit', ?, ?, ?, 0, 1)""",
            (mem_id, now, now, now),
        )
        await db.execute(
            "INSERT INTO memory_embeddings (memory_id, embedding, model_used) VALUES (?, ?, 'test-model')",
            (mem_id, b"\x00\x01\x02\x03"),
        )
        await db.execute(
            """INSERT INTO memory_access_log (id, memory_id, conversation_id, was_useful, accessed_at)
               VALUES (?, ?, 'conv-x', 1, ?)""",
            (str(uuid.uuid4()), mem_id, now),
        )

        # Out-of-scope: a task.
        await db.execute(
            """INSERT INTO task_states
               (id, task_name, status, objective, created_at, updated_at)
               VALUES ('task-1', 'ship the fix', 'active', 'close the P1s', ?, ?)""",
            (now, now),
        )

        # Out-of-scope: a chat conversation + message.
        await db.execute(
            """INSERT INTO conversations (id, title, network_mode, created_at, updated_at)
               VALUES ('convo-1', 'a real conversation', 0, ?, ?)""",
            (now, now),
        )
        await db.execute(
            """INSERT INTO messages
               (id, conversation_id, role, content, created_at)
               VALUES ('msg-1', 'convo-1', 'user', 'hello', ?)""",
            (now,),
        )

        # Out-of-scope: vault state.
        await db.execute(
            """INSERT INTO vault_files (file_path, content_sha, chunk_count, last_ingested_at)
               VALUES ('/notes.md', 'deadbeef', 3, ?)""",
            (now,),
        )

        # Out-of-scope: agent observability.
        await db.execute(
            """INSERT INTO mcp_sessions
               (id, transport, started_at, last_seen_at)
               VALUES ('sess-1', 'stdio', ?, ?)""",
            (now, now),
        )

        await db.commit()


async def _counts(db_path) -> dict[str, int]:
    tables = [
        "memories",
        "memory_embeddings",
        "memory_access_log",
        "task_states",
        "conversations",
        "messages",
        "vault_files",
        "mcp_sessions",
    ]
    out: dict[str, int] = {}
    async with aiosqlite.connect(db_path) as db:
        for t in tables:
            cursor = await db.execute(f"SELECT COUNT(*) FROM {t}")
            row = await cursor.fetchone()
            out[t] = row[0]
    return out


@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(db_path))
    _run(init_memory_db(db_path))
    _run(_seed(db_path))
    before = _run(_counts(db_path))
    assert before == {
        "memories": 1,
        "memory_embeddings": 1,
        "memory_access_log": 1,
        "task_states": 1,
        "conversations": 1,
        "messages": 1,
        "vault_files": 1,
        "mcp_sessions": 1,
    }
    return db_path


# ---------------------------------------------------------------------------
# Service-level: wipe_memory_tables() directly.
# ---------------------------------------------------------------------------


def test_wipe_memory_tables_clears_only_memory_data(seeded_db):
    removed = _run(wipe_memory_tables(seeded_db))
    assert removed == 1

    after = _run(_counts(seeded_db))
    assert after["memories"] == 0
    assert after["memory_embeddings"] == 0
    assert after["memory_access_log"] == 0
    # Every other surface survives untouched.
    assert after["task_states"] == 1
    assert after["conversations"] == 1
    assert after["messages"] == 1
    assert after["vault_files"] == 1
    assert after["mcp_sessions"] == 1


# ---------------------------------------------------------------------------
# Route-level: POST /memory/wipe.
# ---------------------------------------------------------------------------


def test_memory_wipe_route_preserves_tasks_and_chat_history(seeded_db):
    app = create_app()
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        resp = client.post("/memory/wipe", json={"confirm": True})
        assert resp.status_code == 200
        assert resp.json() == {"status": "wiped"}

    after = _run(_counts(seeded_db))
    assert after["memories"] == 0
    # The task and the conversation must both still be there.
    assert after["task_states"] == 1
    assert after["conversations"] == 1
    assert after["messages"] == 1
    assert after["vault_files"] == 1
    assert after["mcp_sessions"] == 1


def test_memory_wipe_route_requires_confirm(seeded_db):
    app = create_app()
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        resp = client.post("/memory/wipe", json={"confirm": False})
        assert resp.status_code == 400

    # Nothing was touched.
    after = _run(_counts(seeded_db))
    assert after["memories"] == 1

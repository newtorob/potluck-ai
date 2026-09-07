"""Chat history survives a sidecar restart.

The frontend chatStore persists every conversation + message through the
/chat/conversations endpoints, which write to the shared memory_db SQLite file.
The launch worry was "chats are lost on restart"; these lock that they are NOT.

Each memory_db call opens its own connection to the on-disk file, so reading
after writing already goes through disk. To make the restart semantics explicit,
the read phase runs on a fresh event loop and re-runs the idempotent schema init
(CREATE TABLE IF NOT EXISTS) -- exactly what a cold sidecar start does.

These tests write chat content directly through memory_db, which since the
2026-09-06 privacy fix refuses to write with privacy.chat_history_enabled off
(the shipped default). The chat_history_on autouse fixture below turns the
switch on for this module, which is the state these tests were always about:
a user who opted into saving chat history expects it to survive a restart.
See tests/test_chat_history_privacy_gate.py for the switch-off behaviour.
"""
from __future__ import annotations

import asyncio

import pytest

from potluck_local.services import settings_manager as sm
from potluck_local.services.memory_db import (
    append_message,
    create_conversation,
    delete_conversation,
    get_conversation,
    init_memory_db,
    list_conversations,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def chat_history_on(tmp_path, monkeypatch):
    """Point settings at an isolated config.json with chat history enabled."""
    data_dir = tmp_path / "data_dir"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(
        '{"privacy": {"chat_history_enabled": true}}', encoding="utf-8"
    )
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(data_dir))
    monkeypatch.setattr(sm, "_settings_cache", {})


def test_conversation_and_messages_survive_restart(tmp_path):
    db = tmp_path / "memory.db"

    async def write_phase():
        await init_memory_db(db_path=db)
        await create_conversation("c1", "First chat", None, False, None, db_path=db)
        await append_message("m1", "c1", "user", "hello", db_path=db)
        await append_message(
            "m2", "c1", "assistant", "hi there",
            status="complete", served_by="local", output_tokens=3, db_path=db,
        )

    _run(write_phase())

    async def after_restart():
        # Cold start: fresh loop, re-run schema init, read via fresh connections.
        await init_memory_db(db_path=db)
        convo = await get_conversation("c1", db_path=db)
        listing = await list_conversations(include_all=True, db_path=db)
        return convo, listing

    convo, listing = _run(after_restart())

    assert convo is not None
    assert convo["title"] == "First chat"
    msgs = convo["messages"]
    assert [m["content"] for m in msgs] == ["hello", "hi there"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["served_by"] == "local"
    assert msgs[1]["output_tokens"] == 3

    # The list summary reflects the persisted state too.
    assert len(listing) == 1
    assert listing[0]["id"] == "c1"
    assert listing[0]["message_count"] == 2


def test_delete_conversation_cascades_and_is_gone_after_restart(tmp_path):
    db = tmp_path / "memory.db"

    async def scenario():
        await init_memory_db(db_path=db)
        await create_conversation("c1", "x", None, False, None, db_path=db)
        await append_message("m1", "c1", "user", "hello", db_path=db)
        existed = await delete_conversation("c1", db_path=db)
        # Fresh reads (post-delete) confirm it and its messages are gone.
        gone = await get_conversation("c1", db_path=db)
        listing = await list_conversations(include_all=True, db_path=db)
        return existed, gone, listing

    existed, gone, listing = _run(scenario())
    assert existed is True
    assert gone is None
    assert listing == []


def test_create_conversation_is_idempotent(tmp_path):
    """Re-POST create with the same id must not 500 (UNIQUE) — optimistic
    clients retry create before append."""
    db = tmp_path / "memory.db"

    async def scenario():
        await init_memory_db(db_path=db)
        first = await create_conversation("c1", "First", None, False, None, db_path=db)
        second = await create_conversation("c1", "Ignored title", None, False, None, db_path=db)
        return first, second

    first, second = _run(scenario())
    assert first["id"] == "c1"
    assert second["id"] == "c1"
    # Existing row wins — title is not overwritten by the duplicate create.
    assert second["title"] == "First"

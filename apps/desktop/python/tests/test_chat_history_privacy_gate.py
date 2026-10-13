"""Chat history writes must honor the privacy.chat_history_enabled switch.

`PrivacySettings.chat_history_enabled` defaults to False. Every content-writing
chat endpoint (POST /chat/conversations, PATCH /chat/conversations/{id},
POST /chat/conversations/{id}/messages, PATCH /chat/messages/{id}) must not
persist the user's prompts and derived titles into memory.db while it is off.
Otherwise the default install would write chat content to disk that the user
had never opted into (POSTing with no config.json at all, the shipped
default, must leave `conversations` and `messages` empty).

There are two gates. The router refuses the four write verbs with 403 and a
stable `chat_history_disabled` code. memory_db refuses the same writes at the
persistence boundary with ChatHistoryDisabledError, fails closed if settings
cannot be read, and raises before any SQL runs so a write is never silently
dropped.

Reads and deletes stay open with the flag off: the user must always be able to
see and erase what is already stored, and turning the switch off never deletes
anything.

Isolated via POTLUCK_MEMORY_DB_PATH + POTLUCK_DATA_DIR so this test can never
touch a real ~/.potluck.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app
from potluck_local.services import settings_manager as sm
from potluck_local.services.memory_db import (
    ChatHistoryDisabledError,
    append_message,
    create_conversation,
    init_memory_db,
    update_conversation,
    update_message,
)

# TestClient client= tuple that satisfies the peer_access_middleware loopback
# check. Without it every request 403s for an unrelated reason, which would
# make a status-only assertion pass against a broken gate - so the tests below
# assert on the detail string too.
LOCAL = ("127.0.0.1", 50000)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    data_dir = tmp_path / "data_dir"
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(db_path))
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(data_dir))
    # No config.json is written here on purpose: that is the shipped default,
    # and the shipped default is chat_history_enabled=False.
    monkeypatch.setattr(sm, "_settings_cache", {})
    return db_path


@pytest.fixture()
def client(isolated_env):
    app = create_app()
    with TestClient(app, client=LOCAL) as c:
        yield c


def _set_chat_history(enabled: bool) -> None:
    """Write config.json under POTLUCK_DATA_DIR and drop the mtime cache."""
    cfg = sm._default_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps({"privacy": {"chat_history_enabled": enabled}}),
        encoding="utf-8",
    )
    sm._settings_cache.clear()


def _count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _seed_rows(db_path: Path) -> None:
    """Insert a conversation + message with raw SQL, bypassing both gates.

    Stands in for history written while the switch was on, so the read/delete
    tests below have something real to see and erase.
    """
    now = "2026-09-06T00:00:00Z"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO conversations (id, title, network_mode, created_at, updated_at)
               VALUES ('seeded', 'kept from before', 0, ?, ?)""",
            (now, now),
        )
        conn.execute(
            """INSERT INTO messages (id, conversation_id, role, content, created_at)
               VALUES ('seeded-msg', 'seeded', 'user', 'still readable', ?)""",
            (now,),
        )


# ---------------------------------------------------------------------------
# (a) Flag off: the four write verbs are refused and nothing lands on disk.
# ---------------------------------------------------------------------------


def test_write_endpoints_are_refused_when_chat_history_disabled(client, isolated_env):
    responses = {
        "create_conversation": client.post(
            "/chat/conversations",
            json={"id": "c1", "title": "what is my mortgage rate"},
        ),
        "rename_conversation": client.patch(
            "/chat/conversations/c1",
            json={"title": "renamed by the client"},
        ),
        "append_message": client.post(
            "/chat/conversations/c1/messages",
            json={"id": "m1", "role": "user", "content": "hello"},
        ),
        "patch_message": client.patch(
            "/chat/messages/m1",
            json={"content": "finalized assistant text"},
        ),
    }

    for name, resp in responses.items():
        assert resp.status_code == 403, f"{name} returned {resp.status_code}"
        detail = str(resp.json()["detail"])
        assert detail.startswith("chat_history_disabled"), f"{name}: {detail!r}"

    assert _count(isolated_env, "conversations") == 0
    assert _count(isolated_env, "messages") == 0


# ---------------------------------------------------------------------------
# (b) Flag on: every write path works.
# ---------------------------------------------------------------------------


def test_write_endpoints_work_when_chat_history_enabled(client, isolated_env):
    _set_chat_history(True)

    created = client.post(
        "/chat/conversations",
        json={"id": "c1", "title": "First chat"},
    )
    assert created.status_code == 201
    assert created.json()["id"] == "c1"

    renamed = client.patch("/chat/conversations/c1", json={"title": "Renamed"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Renamed"

    appended = client.post(
        "/chat/conversations/c1/messages",
        json={"id": "m1", "role": "user", "content": "hello"},
    )
    assert appended.status_code == 201

    patched = client.patch("/chat/messages/m1", json={"content": "hello again"})
    assert patched.status_code == 200
    assert patched.json()["updated"] is True

    assert _count(isolated_env, "conversations") == 1
    assert _count(isolated_env, "messages") == 1
    with sqlite3.connect(isolated_env) as conn:
        assert conn.execute("SELECT content FROM messages WHERE id = 'm1'").fetchone()[0] == (
            "hello again"
        )


# ---------------------------------------------------------------------------
# (c) Flag off: reads and deletes stay available for what is already stored.
# ---------------------------------------------------------------------------


def test_reads_and_deletes_still_work_when_chat_history_disabled(client, isolated_env):
    _seed_rows(isolated_env)
    _set_chat_history(False)

    listing = client.get("/chat/conversations?include_all=true")
    assert listing.status_code == 200
    assert [c["id"] for c in listing.json()] == ["seeded"]

    one = client.get("/chat/conversations/seeded")
    assert one.status_code == 200
    assert one.json()["title"] == "kept from before"
    assert [m["content"] for m in one.json()["messages"]] == ["still readable"]

    cleared = client.delete("/chat/conversations/seeded/messages")
    assert cleared.status_code == 200
    assert cleared.json()["removed"] == 1
    assert _count(isolated_env, "messages") == 0

    deleted = client.delete("/chat/conversations/seeded")
    assert deleted.status_code == 204
    assert _count(isolated_env, "conversations") == 0


def test_turning_the_switch_off_deletes_nothing(client, isolated_env):
    _set_chat_history(True)
    assert client.post(
        "/chat/conversations", json={"id": "c1", "title": "kept"}
    ).status_code == 201
    assert client.post(
        "/chat/conversations/c1/messages",
        json={"id": "m1", "role": "user", "content": "kept too"},
    ).status_code == 201

    _set_chat_history(False)

    assert _count(isolated_env, "conversations") == 1
    assert _count(isolated_env, "messages") == 1
    assert client.get("/chat/conversations/c1").json()["title"] == "kept"


# ---------------------------------------------------------------------------
# (d) Defense in depth: memory_db refuses the write itself, not just the route.
# ---------------------------------------------------------------------------


def test_memory_db_create_conversation_raises_when_disabled(isolated_env):
    _run(init_memory_db(db_path=isolated_env))

    with pytest.raises(ChatHistoryDisabledError):
        _run(create_conversation("c1", "a title", None, False, None, db_path=isolated_env))

    assert _count(isolated_env, "conversations") == 0


def test_memory_db_append_message_raises_when_disabled(isolated_env):
    _run(init_memory_db(db_path=isolated_env))
    _seed_rows(isolated_env)

    with pytest.raises(ChatHistoryDisabledError):
        _run(append_message("m2", "seeded", "user", "secret", db_path=isolated_env))

    assert _count(isolated_env, "messages") == 1  # only the seeded row


def test_memory_db_updates_are_gated_only_on_content_columns(isolated_env):
    """title/content are content; status and network_mode are not."""
    _run(init_memory_db(db_path=isolated_env))
    _seed_rows(isolated_env)

    with pytest.raises(ChatHistoryDisabledError):
        _run(update_conversation("seeded", title="new title", db_path=isolated_env))
    with pytest.raises(ChatHistoryDisabledError):
        _run(update_message("seeded-msg", content="new content", db_path=isolated_env))

    # Non-content patches still go through with the switch off.
    assert _run(update_conversation("seeded", network_mode=True, db_path=isolated_env)) is True
    assert _run(update_message("seeded-msg", status="complete", db_path=isolated_env)) is True

    with sqlite3.connect(isolated_env) as conn:
        assert conn.execute(
            "SELECT title FROM conversations WHERE id = 'seeded'"
        ).fetchone()[0] == "kept from before"
        assert conn.execute(
            "SELECT content FROM messages WHERE id = 'seeded-msg'"
        ).fetchone()[0] == "still readable"


# ---------------------------------------------------------------------------
# (e) Fail closed: an unreadable settings file must not mean "write anyway".
# ---------------------------------------------------------------------------


def test_memory_db_gate_fails_closed_when_settings_raise(isolated_env, monkeypatch):
    _run(init_memory_db(db_path=isolated_env))
    _set_chat_history(True)

    def _boom(*args, **kwargs):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(sm, "get_settings", _boom)

    with pytest.raises(ChatHistoryDisabledError):
        _run(create_conversation("c1", "a title", None, False, None, db_path=isolated_env))

    assert _count(isolated_env, "conversations") == 0


# ---------------------------------------------------------------------------
# A ChatHistoryDisabledError escaping memory_db must surface as 403, not 500.
# ---------------------------------------------------------------------------


def test_escaping_memory_db_error_becomes_403(client, isolated_env, monkeypatch):
    """Router gate on, persistence gate off - the HTTP answer stays the same."""
    from potluck_local.routers import chat as chat_router

    _set_chat_history(True)
    monkeypatch.setattr(chat_router, "_chat_history_enabled", lambda: True)

    def _boom(*args, **kwargs):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(sm, "get_settings", _boom)

    resp = client.post("/chat/conversations", json={"id": "c1", "title": "leaky"})
    assert resp.status_code == 403
    assert str(resp.json()["detail"]).startswith("chat_history_disabled")
    assert _count(isolated_env, "conversations") == 0

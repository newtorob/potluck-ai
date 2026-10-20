"""Saved prompts are chat content: gated by origin, removed with the history and the switches."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app
from potluck_local.services import memory_db, prompt_cache as pc
from potluck_local.services import settings_manager as sm

SHA_A, SHA_B, SIG = "a" * 64, "b" * 64, "sig0"
LOCAL = ("127.0.0.1", 50000)


def toks(n, start=0):
    return [(start + i) % 997 for i in range(n)]


def filled(root):
    """Entries for two models; neither has to be loaded for the purge to remove it."""
    s = pc.PromptCacheStore(root, block=8, min_tokens=8)
    s.save(SHA_A, SIG, toks(32), b"a-state")
    s.save(SHA_B, SIG, toks(32, 300), b"b-state")
    assert s.stats()["entries"] == 2
    s.close()


def count(root):
    s = pc.PromptCacheStore(root, block=8, min_tokens=8)
    try:
        return s.stats()["entries"], len(list(root.rglob("*.entry")))
    finally:
        s.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(tmp_path / "data" / "memory.db"))
    monkeypatch.setattr(pc, "FREE_SPACE_FLOOR", 0)
    monkeypatch.setattr(sm, "_settings_cache", {})
    return tmp_path


def settings(history: bool, cache: bool = True):
    s = sm.PotluckSettings()
    s.privacy.chat_history_enabled = history
    s.runtime.prompt_cache = cache
    return s


# ---- where a request enters -------------------------------------------------------------

def test_the_middleware_marks_local_and_peer_paths(env):
    app = create_app()
    seen = {}

    @app.get("/v1/_origin_probe")
    def local_probe():
        seen["local"] = pc.request_is_local.get()
        return {}

    @app.get("/peer/_origin_probe")
    def peer_probe():
        seen["peer"] = pc.request_is_local.get()
        return {}

    with TestClient(app, client=LOCAL) as client:
        client.get("/v1/_origin_probe")
        client.get("/peer/_origin_probe")
    assert seen == {"local": True, "peer": False}


# ---- settings --------------------------------------------------------------------------

def test_turning_chat_history_off_purges_every_model(env):
    root = pc.cache_root(env)
    filled(root)
    cfg = env / "config.json"
    sm.save_settings(settings(history=True), cfg)  # nothing to compare with yet
    sm.save_settings(settings(history=True), cfg)
    assert count(root) == (2, 2)
    sm.save_settings(settings(history=False), cfg)
    assert count(root) == (0, 0)


def test_turning_the_cache_switch_off_purges_and_unrelated_saves_do_not(env):
    root = pc.cache_root(env)
    filled(root)
    cfg = env / "config.json"
    sm.save_settings(settings(history=True), cfg)
    sm.save_settings(settings(history=True, cache=True), cfg)
    assert count(root)[0] == 2
    sm.save_settings(settings(history=True, cache=False), cfg)
    assert count(root) == (0, 0)


def test_history_that_was_already_off_keeps_nothing_to_purge_and_does_not_fail(env):
    cfg = env / "config.json"
    sm.save_settings(settings(history=False), cfg)
    sm.save_settings(settings(history=False), cfg)
    assert not pc.cache_root(env).exists()


def test_a_purge_stops_an_attached_cache_from_writing_back(env):
    root = pc.cache_root(env)

    class Model:
        n_tokens = 0
        def n_ctx(self): return 4096

    cache = pc.PromptDiskCache.__new__(pc.PromptDiskCache)
    cache.store = pc.PromptCacheStore(root, block=8, min_tokens=8)
    import queue
    cache._queue = queue.Queue(maxsize=2)
    cache.disabled = False
    pc._LIVE.add(cache)
    filled_store = cache.store
    filled_store.save(SHA_A, SIG, toks(32), b"x")
    pc.purge_all(root, "test")
    assert cache.disabled and count(root) == (0, 0)


# ---- clearing chat history ---------------------------------------------------------------

def seed_conversation(db_path):
    asyncio.run(memory_db.init_memory_db(db_path))
    now = "2026-09-06T00:00:00Z"
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO conversations (id, title, network_mode, created_at, updated_at) VALUES ('c1','t',0,?,?)", (now, now))
        conn.execute("INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES ('m1','c1','user','hi',?)", (now,))


def test_deleting_a_conversation_purges_the_saved_prompts(env):
    root = pc.cache_root(env)
    filled(root)
    db_path = env / "data" / "memory.db"
    seed_conversation(db_path)
    assert asyncio.run(memory_db.delete_conversation("c1")) is True
    assert count(root) == (0, 0)


def test_clearing_a_conversations_messages_purges_the_saved_prompts(env):
    root = pc.cache_root(env)
    filled(root)
    seed_conversation(env / "data" / "memory.db")
    assert asyncio.run(memory_db.clear_conversation_messages("c1")) == 1
    assert count(root) == (0, 0)


def test_deleting_something_that_does_not_exist_keeps_the_cache(env):
    root = pc.cache_root(env)
    filled(root)
    asyncio.run(memory_db.init_memory_db(env / "data" / "memory.db"))
    assert asyncio.run(memory_db.delete_conversation("nope")) is False
    assert count(root) == (2, 2)

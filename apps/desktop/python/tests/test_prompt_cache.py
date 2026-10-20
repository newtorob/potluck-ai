"""Disk prompt cache: keying, longest prefix restore, eviction, expiry, model isolation."""
from __future__ import annotations

import shutil
import sqlite3
import types

import numpy as np
import pytest

from potluck_local.services import prompt_cache as pc

SHA_A = "a" * 64
SHA_B = "b" * 64
SIG = "sig0"
BLOCK = 8


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def toks(n, start=0):
    return [(start + i) % 997 for i in range(n)]


def store(tmp_path, clock=None, **kw):
    kw.setdefault("block", BLOCK)
    kw.setdefault("min_tokens", BLOCK)
    kw.setdefault("max_bytes", 10_000_000)
    return pc.PromptCacheStore(tmp_path / "prompt-cache", clock=clock or Clock(), **kw)


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch):
    monkeypatch.setattr(pc, "FREE_SPACE_FLOOR", 0)


# ---- keying -----------------------------------------------------------------

def test_chain_hash_covers_the_whole_prefix():
    base = toks(32)
    changed = list(base)
    changed[3] += 1
    a, b = pc.chain_hashes(SHA_A, SIG, base, BLOCK), pc.chain_hashes(SHA_A, SIG, changed, BLOCK)
    assert len(a) == 4
    # A change inside block 0 changes every later hash, because each hash chains the one before.
    assert all(x != y for x, y in zip(a, b))
    longer = pc.chain_hashes(SHA_A, SIG, base + toks(5), BLOCK)
    assert longer == a  # a partial trailing block adds no hash


def test_chain_hash_depends_on_model_and_engine_signature():
    t = toks(16)
    assert pc.chain_hashes(SHA_A, SIG, t, BLOCK) != pc.chain_hashes(SHA_B, SIG, t, BLOCK)
    assert pc.chain_hashes(SHA_A, SIG, t, BLOCK) != pc.chain_hashes(SHA_A, "other", t, BLOCK)
    assert pc.entry_id(SHA_A, SIG, t) != pc.entry_id(SHA_B, SIG, t)


# ---- save, lookup, read -------------------------------------------------------

def test_roundtrip_returns_tokens_and_state(tmp_path):
    s = store(tmp_path)
    t = toks(40)
    eid = s.save(SHA_A, SIG, t, b"state-bytes")
    hit = s.lookup(SHA_A, SIG, t + toks(10, 500))
    assert hit and hit.entry_id == eid and hit.n_tokens == 40 and hit.matched_tokens == 40
    tokens, state = s.read(hit)
    assert tokens == t and state == b"state-bytes"
    assert oct(hit.path.stat().st_mode & 0o777) == "0o600"


def test_lookup_picks_the_longest_shared_prefix(tmp_path):
    s = store(tmp_path)
    short, long_ = toks(16), toks(16) + toks(24, 300)
    s.save(SHA_A, SIG, toks(16) + toks(8, 700), b"x")  # shares 16 tokens
    s.save(SHA_A, SIG, long_, b"y")  # shares 40
    hit = s.lookup(SHA_A, SIG, long_ + [1, 2, 3])
    assert hit.matched_tokens == 40 and hit.n_tokens == 40
    assert s.lookup(SHA_A, SIG, short + toks(4, 900)).matched_tokens == 16


def test_partial_match_restores_a_longer_saved_entry(tmp_path):
    s = store(tmp_path)
    saved = toks(40)
    s.save(SHA_A, SIG, saved, b"z")
    diverging = saved[:27] + [9999] * 20  # shares 3 whole blocks (24 tokens)
    hit = s.lookup(SHA_A, SIG, diverging)
    assert hit.n_tokens == 40 and hit.matched_tokens == 24


def test_short_prompts_and_unrelated_prompts_miss(tmp_path):
    s = store(tmp_path, min_tokens=16)
    assert s.save(SHA_A, SIG, toks(10), b"x") is None
    s.save(SHA_A, SIG, toks(32), b"x")
    assert s.lookup(SHA_A, SIG, toks(32, 400)) is None
    assert s.lookup(SHA_A, SIG, toks(5)) is None


def test_saving_the_same_tokens_twice_keeps_one_entry(tmp_path):
    s = store(tmp_path)
    assert s.save(SHA_A, SIG, toks(24), b"x") == s.save(SHA_A, SIG, toks(24), b"x")
    assert s.stats()["entries"] == 1


def test_a_longer_turn_replaces_the_earlier_prefix_entries(tmp_path):
    s = store(tmp_path)
    s.save(SHA_A, SIG, toks(24), b"turn1")
    s.save(SHA_A, SIG, toks(48), b"turn2")
    s.save(SHA_A, SIG, toks(24) + toks(8, 800), b"fork")  # diverges, not a prefix of turn 2
    assert s.stats()["entries"] == 2
    hit = s.lookup(SHA_A, SIG, toks(60))
    assert hit.n_tokens == 48


# ---- model isolation ------------------------------------------------------------

def test_an_entry_for_one_model_is_invisible_to_another(tmp_path):
    s = store(tmp_path)
    t = toks(32)
    s.save(SHA_A, SIG, t, b"model-a")
    assert s.lookup(SHA_B, SIG, t) is None
    assert s.lookup(SHA_A, "different-engine", t) is None


def test_a_file_copied_into_another_models_directory_is_refused(tmp_path):
    s = store(tmp_path)
    t = toks(32)
    eid = s.save(SHA_A, SIG, t, b"model-a")
    (s.root / SHA_B).mkdir()
    shutil.copy(s.root / SHA_A / f"{eid}.entry", s.root / SHA_B / f"{eid}.entry")
    # Also forge the index so that B's chain points at the copied file.
    db = sqlite3.connect(s.root / "index.sqlite")
    db.execute("INSERT INTO entries SELECT ?, ?, signature, n_tokens, size_bytes, created, last_used FROM entries", (eid + "b", SHA_B))
    for h in pc.chain_hashes(SHA_B, SIG, t, BLOCK):
        db.execute("INSERT INTO blocks VALUES (?, ?, 32)", (h, eid + "b"))
    db.commit()
    db.close()
    (s.root / SHA_B / f"{eid}b.entry").write_bytes((s.root / SHA_B / f"{eid}.entry").read_bytes())
    assert s.lookup(SHA_B, SIG, t) is None  # header names model A
    assert s.lookup(SHA_A, SIG, t) is not None


def test_truncated_and_foreign_files_are_misses_and_deleted(tmp_path):
    s = store(tmp_path)
    t = toks(32)
    eid = s.save(SHA_A, SIG, t, b"0123456789" * 10)
    path = s.root / SHA_A / f"{eid}.entry"
    path.write_bytes(path.read_bytes()[:-5])
    assert s.lookup(SHA_A, SIG, t) is None
    assert not path.exists() and s.stats()["entries"] == 0
    eid = s.save(SHA_A, SIG, t, b"abc")
    (s.root / SHA_A / f"{eid}.entry").write_bytes(b"not an entry")
    assert s.lookup(SHA_A, SIG, t) is None


# ---- expiry and eviction -------------------------------------------------------

def test_entries_expire_after_the_ttl_since_last_use(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock, ttl_seconds=3 * 86400)
    s.save(SHA_A, SIG, toks(32), b"old")
    s.save(SHA_A, SIG, toks(32, 400), b"used")
    clock.now += 2 * 86400
    hit = s.lookup(SHA_A, SIG, toks(32, 400))
    s.read(hit)  # use refreshes the clock for this entry only
    clock.now += 2 * 86400  # 4 days since the first save, 2 since the use
    s.sweep()
    assert s.lookup(SHA_A, SIG, toks(32)) is None
    assert s.lookup(SHA_A, SIG, toks(32, 400)) is not None
    assert len(list((s.root / SHA_A).glob("*.entry"))) == 1


def test_least_recently_used_entries_go_first_when_over_the_cap(tmp_path):
    clock = Clock()
    payload = b"x" * 1000
    s = store(tmp_path, clock, max_bytes=4500)
    ids = []
    for k in range(3):
        clock.now += 10
        ids.append(s.save(SHA_A, SIG, toks(32, 100 * k), payload))
    assert s.stats()["entries"] == 3
    clock.now += 10
    s.read(s.lookup(SHA_A, SIG, toks(32, 0)))  # entry 0 becomes the most recent
    clock.now += 10
    s.save(SHA_A, SIG, toks(32, 900), payload)
    left = {p.stem for p in (s.root / SHA_A).glob("*.entry")}
    assert ids[0] in left and ids[1] not in left
    assert s.stats()["bytes"] <= 4500


def test_an_entry_larger_than_the_cap_is_not_stored(tmp_path):
    s = store(tmp_path, max_bytes=500)
    assert s.save(SHA_A, SIG, toks(32), b"x" * 1000) is None
    assert s.stats()["entries"] == 0


def test_sweep_removes_orphan_files_and_dangling_rows(tmp_path):
    s = store(tmp_path)
    eid = s.save(SHA_A, SIG, toks(32), b"x")
    (s.root / SHA_A / "feedface.entry").write_bytes(b"orphan")
    (s.root / SHA_A / f"{eid}.entry").unlink()
    s.sweep()
    assert s.stats()["entries"] == 0 and not list((s.root / SHA_A).glob("*.entry"))


# ---- eligibility --------------------------------------------------------------------

def test_only_trimmable_architectures_are_eligible():
    assert pc.eligibility({"general.architecture": "qwen2", "qwen2.block_count": "28"}) is None
    assert "not verified" in pc.eligibility({"general.architecture": "qwen3next"})
    assert "sliding window" in pc.eligibility({"general.architecture": "llama", "llama.attention.sliding_window": "4096"})
    assert "recurrent" in pc.eligibility({"general.architecture": "llama", "llama.ssm.state_size": "16"})
    assert pc.eligibility({"general.architecture": "gemma", "gemma.attention.sliding_window": "0"}) is None
    assert pc.eligibility(None)


# ---- adapter -----------------------------------------------------------------------

class FakeModel:
    def __init__(self, n_ctx=4096):
        self.input_ids = np.zeros(n_ctx, dtype=np.intc)
        self.n_tokens = 0
        self._n_ctx = n_ctx
        self.loaded = []
        self.cleared = 0

    def n_ctx(self):
        return self._n_ctx

    def reset(self):
        self.n_tokens = 0
        self.cleared += 1

    _ctx = types.SimpleNamespace(kv_cache_clear=lambda: None)


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "engine_signature", lambda model: SIG)

    def fake_load(model, tokens, state):
        if state == b"corrupt":
            raise RuntimeError("Failed to set llama state data")
        model.loaded.append(state)
        model.input_ids[: len(tokens)] = tokens
        model.n_tokens = len(tokens)

    monkeypatch.setattr(pc, "_load_into", fake_load)
    model = FakeModel()
    cache = pc.PromptDiskCache(store(tmp_path), model, SHA_A)
    token = pc.request_is_local.set(True)  # a request that began on this machine
    yield cache, model
    pc.request_is_local.reset(token)
    cache.close()


def saved_state(tokens, state=b"kv"):
    return types.SimpleNamespace(n_tokens=len(tokens), input_ids=np.asarray(tokens, dtype=np.intc), llama_state=state)


def test_adapter_saves_after_a_turn_and_restores_into_a_new_model(adapter, tmp_path, monkeypatch):
    cache, model = adapter
    turn = toks(64)
    cache[turn + [5]] = saved_state(turn)  # llama-cpp-python's key is one token longer than the state
    cache.flush()
    fresh = FakeModel()
    reloaded = pc.PromptDiskCache(cache.store, fresh, SHA_A)
    restored = reloaded[turn + toks(10, 700)]
    assert fresh.n_tokens == 64 and fresh.loaded == [b"kv"]
    assert restored.input_ids.tolist() == turn
    assert reloaded.last_restore["matched_tokens"] == 64
    reloaded.close()


def test_adapter_does_not_restore_what_the_live_state_already_covers(adapter):
    cache, model = adapter
    turn = toks(64)
    cache[turn] = saved_state(turn)
    cache.flush()
    model.input_ids[:64] = turn
    model.n_tokens = 64
    with pytest.raises(KeyError):
        cache[turn + [1, 2]]
    assert model.loaded == []


def test_adapter_other_models_state_is_never_loaded(adapter):
    cache, model = adapter
    turn = toks(64)
    cache[turn] = saved_state(turn)
    cache.flush()
    other = FakeModel()
    pc.PromptDiskCache(cache.store, other, SHA_B).close()
    with pytest.raises(KeyError):
        pc.PromptDiskCache(cache.store, other, SHA_B)[turn]
    assert other.loaded == []


def test_adapter_a_state_that_fails_to_load_is_a_miss_and_is_removed(adapter):
    cache, model = adapter
    turn = toks(64)
    cache[turn] = saved_state(turn, b"corrupt")
    cache.flush()
    with pytest.raises(KeyError):
        cache[turn]
    assert cache.store.stats()["entries"] == 0 and model.cleared == 1


def test_adapter_close_saves_a_live_state_that_was_never_stored(adapter, monkeypatch):
    cache, model = adapter
    monkeypatch.setattr(pc, "_state_bytes", lambda m: b"live")
    turn = toks(80)
    model.input_ids[:80] = turn
    model.n_tokens = 80
    cache.close()
    hit = cache.store.lookup(SHA_A, SIG, turn)
    assert hit and cache.store.read(hit)[1] == b"live"


# ---- native slot files ----------------------------------------------------------------

class FakeServer:
    """Just enough of llama-server's /slots to move files around."""

    def __init__(self, slot_dir, slot_tokens):
        self.dir, self.slot_tokens, self.calls = slot_dir, slot_tokens, []

    def __call__(self, path, body):
        self.calls.append(path)
        name = body["filename"]
        if "action=save" in path:
            (self.dir / name).write_bytes(b"SLOTFILE-" + str(len(self.slot_tokens)).encode())
            return {"n_saved": len(self.slot_tokens)}
        assert (self.dir / name).read_bytes().startswith(b"SLOTFILE-")
        return {"n_restored": 64}


def test_native_slot_roundtrip_and_cleanup(tmp_path):
    s = store(tmp_path)
    slot_dir = tmp_path / "slots"
    turn = toks(64)
    server = FakeServer(slot_dir, turn)
    cache = pc.NativeSlotCache(s, slot_dir, SHA_A, SIG, server)
    assert cache.save(turn)
    assert not list(slot_dir.iterdir())  # the server's file moved into the store
    got = cache.restore(turn + toks(9, 600))
    assert got["n_restored"] == 64 and got["matched_tokens"] == 64
    assert not list(slot_dir.iterdir())


def test_native_slot_save_refuses_when_the_slot_holds_other_tokens(tmp_path):
    s = store(tmp_path)
    slot_dir = tmp_path / "slots"
    cache = pc.NativeSlotCache(s, slot_dir, SHA_A, SIG, FakeServer(slot_dir, toks(63)))
    assert cache.save(toks(64)) is None and s.stats()["entries"] == 0


def test_native_slot_for_another_model_is_a_miss(tmp_path):
    s = store(tmp_path)
    slot_dir = tmp_path / "slots"
    turn = toks(64)
    pc.NativeSlotCache(s, slot_dir, SHA_A, SIG, FakeServer(slot_dir, turn)).save(turn)
    other = FakeServer(slot_dir, turn)
    assert pc.NativeSlotCache(s, slot_dir, SHA_B, SIG, other).restore(turn) is None
    assert other.calls == []


# ---- origin: only requests that began on this machine ---------------------------------

def test_mark_request_trusts_loopback_but_never_the_peer_surface():
    for host, path, expected in [("127.0.0.1", "/chat/stream", True), ("::1", "/v1/chat/completions", True),
                                 ("127.0.0.1", "/peer/chat/completions", False), ("127.0.0.1", "/peer", False),
                                 ("100.64.0.9", "/peer/chat/completions", False), ("192.168.1.5", "/v1/x", False)]:
        pc.mark_request(host in ("127.0.0.1", "::1"), path)
        assert pc.request_is_local.get() is expected, (host, path)
    pc.request_is_local.set(False)


def test_the_default_is_not_local():
    import contextvars

    assert contextvars.Context().run(pc.request_is_local.get) is False


def test_worker_threads_get_the_requests_origin_only_through_in_request_context():
    import concurrent.futures

    seen = []
    pc.request_is_local.set(True)
    try:
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pool.submit(lambda: seen.append(pc.request_is_local.get())).result()  # a bare executor drops it
            pool.submit(pc.in_request_context(lambda: seen.append(pc.request_is_local.get()))).result()
    finally:
        pc.request_is_local.set(False)
    assert seen == [False, True]


def test_a_peer_request_neither_restores_nor_saves(adapter):
    cache, model = adapter
    turn = toks(64)
    cache[turn] = saved_state(turn)  # a local turn is saved
    cache.flush()
    assert cache.store.stats()["entries"] == 1
    pc.request_is_local.set(False)  # now a request served for another machine
    with pytest.raises(KeyError):
        cache[turn + [1]]  # no restore, so no timing signal about the local user's prompt
    assert turn not in cache and model.loaded == []
    other = toks(64, 500)
    cache[other] = saved_state(other)
    cache.flush()
    assert cache.store.stats()["entries"] == 1
    assert cache.store.lookup(SHA_A, SIG, other) is None


def test_the_live_state_of_a_peer_request_is_not_saved_on_close(adapter, monkeypatch):
    cache, model = adapter
    monkeypatch.setattr(pc, "_state_bytes", lambda m: b"live")
    pc.request_is_local.set(False)
    with pytest.raises(KeyError):
        cache[toks(80)]
    model.input_ids[:80] = toks(80)  # the peer's prompt is what the model now holds
    model.n_tokens = 80
    cache.close()
    assert cache.store.stats()["entries"] == 0


def test_a_disabled_cache_does_nothing(adapter):
    cache, model = adapter
    turn = toks(64)
    cache[turn] = saved_state(turn)
    cache.flush()
    cache.disable()
    with pytest.raises(KeyError):
        cache[turn]
    cache[toks(64, 9)] = saved_state(toks(64, 9))
    cache.flush()
    assert cache.store.stats()["entries"] == 1

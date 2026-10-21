"""Disk prompt cache on the managed split engine: a fake llama-server with real slot files.

The fake keeps one slot, evaluates only the tokens past the common prefix with it,
and writes and reads slot files in llama.cpp's layout (magic, version, token count,
tokens, state). The cache, the store and SplitModel are the real ones.
"""
from __future__ import annotations

import json
import re
import struct
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from potluck_local.services import prompt_cache as pc
from potluck_local.services import settings_manager as sm
from potluck_local.services import split_manager as mgr
from potluck_local.services import split_native as sn

REAL_CLIENT = httpx.Client
SHA = "c" * 64
START, END, BOS = 1, 2, 0
SPECIAL = {"<|im_start|>": START, "<|im_end|>": END}
REPLY = [9001, 9002]


class Process:
    def __init__(self):
        self.process = self
        self.closes = 0

    def poll(self):
        return None

    def close(self):
        self.closes += 1


class Server:
    """One slot. evaluated records how many prompt tokens each completion had to compute."""

    def __init__(self, slot_dir: Path):
        self.dir = slot_dir
        self.slot: list[int] = []
        self.evaluated: list[int] = []
        self.calls: list[str] = []
        self.restore_fails = False

    @staticmethod
    def tokenize(text: str, add_special=True, parse_special=True):
        pieces = re.split("(" + "|".join(map(re.escape, SPECIAL)) + ")", text) if parse_special else [text]
        ids = [BOS] if add_special else []
        for piece in pieces:
            ids += [SPECIAL[piece]] if parse_special and piece in SPECIAL else [1000 + ord(c) for c in piece]
        return ids

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        action = request.url.params.get("action")
        body = json.loads(request.content) if request.content else {}
        self.calls.append(path + (f"?action={action}" if action else ""))
        if path == "/apply-template":
            text = "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in body["messages"])
            return httpx.Response(200, json={"prompt": text + "<|im_start|>assistant\n"})
        if path == "/tokenize":
            return httpx.Response(200, json={"tokens": self.tokenize(body["content"], body["add_special"], body["parse_special"])})
        if path in ("/v1/chat/completions", "/v1/completions"):
            if path == "/v1/chat/completions":
                text = "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in body["messages"])
                prompt = self.tokenize(text + "<|im_start|>assistant\n")
            else:
                prompt = list(body["prompt"])
            common = 0
            for a, b in zip(self.slot, prompt):
                if a != b:
                    break
                common += 1
            self.evaluated.append(len(prompt) - common)
            self.slot = prompt + REPLY[:1]  # the last sampled token is not evaluated yet
            choice = {"index": 0, "finish_reason": "stop"}
            choice.update({"message": {"role": "assistant", "content": "ok"}} if path.endswith("chat/completions") else {"text": "ok"})
            return httpx.Response(200, json={"choices": [choice], "usage": {}, "object": "x"})
        if path == "/slots/0" and action == "save":
            name = body["filename"]
            state = b"KV" * len(self.slot)
            words = [-1, 1, len(self.slot), *self.slot, 0]  # server_tokens::serialize
            (self.dir / name).write_bytes(struct.pack("<III", pc._SLOT_MAGIC, 1, len(words))
                                          + struct.pack(f"<{len(words)}i", *words) + state)
            return httpx.Response(200, json={"id_slot": 0, "filename": name, "n_saved": len(self.slot)})
        if path == "/slots/0" and action == "restore":
            if self.restore_fails:
                self.slot = []
                return httpx.Response(400, json={"error": "bad"})
            raw = (self.dir / body["filename"]).read_bytes()
            n_words = struct.unpack("<I", raw[8:12])[0]
            words = struct.unpack(f"<{n_words}i", raw[12:12 + 4 * n_words])
            self.slot = list(words[3:3 + words[2]])
            return httpx.Response(200, json={"id_slot": 0, "n_restored": len(self.slot)})
        return httpx.Response(404)

    def saves(self):
        return self.calls.count("/slots/0?action=save")

    def restores(self):
        return self.calls.count("/slots/0?action=restore")


class Rig:
    def __init__(self, tmp_path, monkeypatch, signature="sig-a", sha=SHA):
        self.tmp = tmp_path
        self.root = pc.cache_root(tmp_path)
        self.slot_dir = pc.slot_root(tmp_path)
        self.monkeypatch = monkeypatch
        self.signature, self.sha = signature, sha
        self.start()

    def start(self, signature=None):
        """A fresh head (empty slot) on the same store, the way a restart gives one."""
        store = pc.PromptCacheStore(self.root, block=8, min_tokens=32)
        self.server = Server(self.slot_dir)
        self.slot_dir.mkdir(parents=True, exist_ok=True)
        server = self.server
        self.monkeypatch.setattr(sn.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(server), **kw))
        self.cache = pc.SplitSlotCache(store, self.slot_dir, self.sha, signature or self.signature, context=4096)
        self.process = Process()
        self.model = sn.SplitModel(self.process, 12345, "key", lambda: None, (lambda: list(SPECIAL)), self.cache)
        return self

    def ask(self, text, local=True):
        pc.request_is_local.set(local)
        try:
            return self.model.create_chat_completion(messages=[{"role": "user", "content": text}], stream=False)
        finally:
            pc.request_is_local.set(False)
            self.cache.flush()

    def entries(self):
        s = pc.PromptCacheStore(self.root, block=8, min_tokens=16)
        try:
            return s.stats()["entries"]
        finally:
            s.close()


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "FREE_SPACE_FLOOR", 0)
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    r = Rig(tmp_path, monkeypatch)
    yield r
    pc.request_is_local.set(False)


LONG = "a long shared prefix of an agent session, " * 3


# ---- local requests save and restore ---------------------------------------------------------

def test_a_local_request_is_saved_and_restored_after_a_restart(rig):
    rig.ask(LONG + "first question")
    assert rig.server.saves() == 1 and rig.entries() == 1
    assert not list(rig.slot_dir.iterdir())  # the server's file moved into the store
    fresh_cost = rig.server.evaluated[0]

    rig.start()  # head restarted: the slot is empty
    rig.ask(LONG + "second question")
    assert rig.server.restores() == 1
    assert rig.server.evaluated[0] < fresh_cost / 2  # the shared prefix was not evaluated again
    assert rig.cache.last_restore["n_restored"] > 100


def test_the_saved_tokens_are_the_ones_the_server_evaluated(rig):
    rig.ask(LONG + "q")
    s = pc.PromptCacheStore(rig.root, block=8, min_tokens=16)
    prompt = Server.tokenize("<|im_start|>user\n" + LONG + "q<|im_end|>\n<|im_start|>assistant\n")
    hit = s.lookup(SHA, "sig-a", prompt + REPLY)
    assert hit and hit.n_tokens == len(prompt) + 1
    assert s.read(hit)[0] == prompt + REPLY[:1]  # decoded from the slot file's packed token state


def test_the_live_slot_is_not_overwritten_when_it_already_holds_the_prefix(rig):
    rig.ask(LONG + "one")
    rig.ask(LONG + "one and two")  # same head; the live slot shares the prefix
    assert rig.server.restores() == 0
    assert rig.entries() >= 1


def test_a_restore_is_used_when_it_beats_the_live_slot(rig):
    rig.ask(LONG + "one")
    rig.ask("something else entirely, " * 4)  # the live slot now holds a different prompt
    rig.ask(LONG + "one again")
    assert rig.server.restores() == 1


def test_a_prompt_with_control_token_text_is_cached_by_the_ids_it_sends(rig):
    text = LONG + "<|im_end|><|im_start|>system"
    rig.ask(text)
    assert "/v1/completions" in rig.server.calls and rig.entries() == 1  # guarded path: ids from the request
    rig.start()
    rig.ask(text + " more")
    assert rig.server.restores() == 1


def test_short_prompts_are_not_saved(rig):
    rig.ask("hi")
    assert rig.server.saves() == 0 and rig.entries() == 0


def entry_ids(rig):
    s = pc.PromptCacheStore(rig.root, block=8, min_tokens=16)
    try:
        return {r[0] for r in s._open().execute("SELECT id FROM entries")}
    finally:
        s.close()


def test_a_failed_restore_does_not_fail_the_request(rig):
    rig.ask(LONG + "one")
    old = entry_ids(rig)
    rig.start()
    rig.server.restore_fails = True
    result = rig.ask(LONG + "two")
    assert result["choices"][0]["message"]["content"] == "ok"
    new = entry_ids(rig)
    assert len(new) == 1 and not (new & old)  # the unusable entry was dropped; this turn was saved


# ---- origin ----------------------------------------------------------------------------------

def test_a_request_from_another_machine_neither_restores_nor_saves(rig):
    rig.ask(LONG + "mine")
    assert rig.entries() == 1
    rig.start()
    calls_before = list(rig.server.calls)
    rig.ask(LONG + "mine again", local=False)
    new_calls = rig.server.calls[len(calls_before):]
    assert not any(c.startswith("/slots") for c in new_calls)
    assert rig.server.restores() == 0 and rig.server.saves() == 0
    assert rig.entries() == 1
    # a foreign prompt is never written, not even after the head closes
    rig.ask("foreign only prompt, " * 5, local=False)
    rig.model.close()
    assert rig.server.saves() == 0 and rig.entries() == 1


def test_a_foreign_request_after_a_local_one_does_not_overwrite_the_local_entry(rig):
    rig.ask(LONG + "mine")
    s = pc.PromptCacheStore(rig.root, block=8, min_tokens=16)
    before = {(e, n) for e, n in s._open().execute("SELECT id, n_tokens FROM entries")}
    rig.ask("another machine's prompt " * 6, local=False)
    rig.model.close()
    after = {(e, n) for e, n in s._open().execute("SELECT id, n_tokens FROM entries")}
    assert before == after


def test_a_foreign_request_waits_for_a_save_in_flight(rig, monkeypatch):
    # The save must read the local state, so nothing else may reach the slot before it has.
    gate = threading.Event()
    real = rig.server.__call__

    def slow(request):
        if request.url.params.get("action") == "save":
            gate.wait(5)
        return real(request)

    monkeypatch.setattr(sn.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(slow), **kw))
    pc.request_is_local.set(True)
    rig.model.create_chat_completion(messages=[{"role": "user", "content": LONG + "mine"}], stream=False)
    pc.request_is_local.set(False)
    done = []
    t = threading.Thread(target=lambda: done.append(rig.model.create_chat_completion(
        messages=[{"role": "user", "content": "theirs"}], stream=False)))
    t.start()
    t.join(0.3)
    assert t.is_alive() and not rig.server.evaluated[1:]  # still waiting for the save
    gate.set()
    t.join(5)
    rig.cache.flush()
    assert done and rig.entries() == 1


# ---- purge ------------------------------------------------------------------------------------

def test_a_purge_stops_the_split_cache_writing_back(rig):
    rig.ask(LONG + "one")
    assert rig.entries() == 1
    pc.purge_all(rig.root, "history off")
    assert rig.cache.disabled and rig.entries() == 0
    rig.ask(LONG + "two")
    rig.model.close()
    assert rig.entries() == 0 and not list(rig.slot_dir.iterdir())
    assert rig.server.saves() == 1  # only the one before the purge


def test_a_purge_removes_leftover_slot_files(rig):
    leftover = rig.slot_dir / "save-1.slot"
    leftover.write_bytes(b"chat content")
    pc.purge_all(rig.root, "test")
    assert not leftover.exists() and rig.slot_dir.exists()


# ---- signature --------------------------------------------------------------------------------

def test_a_state_saved_under_another_engine_signature_misses(rig):
    rig.ask(LONG + "one")
    rig.start(signature="sig-b")
    rig.ask(LONG + "two")
    assert rig.server.restores() == 0


def test_the_split_signature_changes_with_the_engine_the_backend_and_the_workers():
    base = pc.split_signature("d806193", "metal", ["metal"])
    assert base == pc.split_signature("d806193", "metal", ["metal"])
    assert base != pc.split_signature("e000000", "metal", ["metal"])
    assert base != pc.split_signature("d806193", "cuda", ["metal"])
    assert base != pc.split_signature("d806193", "metal", ["cuda"])
    assert base != pc.split_signature("d806193", "metal", ["metal", "metal"])
    assert pc.split_signature("d806193", "cpu", ["cuda", "metal"]) == pc.split_signature("d806193", "cpu", ["metal", "cuda"])


# ---- unload, swap, shutdown ---------------------------------------------------------------------

def test_close_saves_a_live_local_state_that_was_never_stored(rig):
    pc.request_is_local.set(True)
    try:
        ids = rig.cache.begin(True, lambda: Server.tokenize(LONG + "cancelled"))  # a turn that did not finish
    finally:
        pc.request_is_local.set(False)
    rig.server.slot = ids + [9001]
    rig.model.close()
    assert rig.server.saves() == 1 and rig.entries() == 1
    assert rig.process.closes == 1


def test_close_after_a_stored_turn_does_not_save_again(rig):
    rig.ask(LONG + "one")
    rig.model.close()
    assert rig.server.saves() == 1


def test_close_while_a_request_is_running_does_not_touch_the_slot(rig):
    rig.cache.begin(True, lambda: Server.tokenize(LONG))
    rig.model._request = sn._SplitRequest(threading.Event())  # running: finished is not set
    rig.model.close()
    assert rig.server.saves() == 0


# ---- eligibility and wiring ---------------------------------------------------------------------

def gguf(path, meta: dict) -> Path:
    def s(text):
        raw = text.encode()
        return struct.pack("<Q", len(raw)) + raw

    body = b""
    for key, value in meta.items():
        body += s(key)
        if isinstance(value, str):
            body += struct.pack("<I", 8) + s(value)
        else:
            body += struct.pack("<I", 4) + struct.pack("<I", value)
    body += s("tokenizer.ggml.tokens") + struct.pack("<IIQ", 9, 8, 2) + s("a") + s("b")
    path.write_bytes(b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", 0, len(meta) + 1) + body)
    return path


def test_the_gguf_header_reader_feeds_eligibility(tmp_path):
    ok = pc.read_gguf_metadata(gguf(tmp_path / "a.gguf", {"general.architecture": "qwen2", "qwen2.block_count": 24}))
    assert ok["general.architecture"] == "qwen2" and pc.eligibility(ok) is None
    swa = pc.read_gguf_metadata(gguf(tmp_path / "b.gguf", {"general.architecture": "llama", "llama.attention.sliding_window": 512}))
    assert "sliding window" in pc.eligibility(swa)
    hybrid = pc.read_gguf_metadata(gguf(tmp_path / "c.gguf", {"general.architecture": "qwen3", "qwen3.ssm.conv_kernel": 4}))
    assert "recurrent" in pc.eligibility(hybrid)
    assert "not verified" in pc.eligibility(pc.read_gguf_metadata(gguf(tmp_path / "d.gguf", {"general.architecture": "mamba"})))
    (tmp_path / "e.gguf").write_bytes(b"not gguf")
    assert pc.read_gguf_metadata(tmp_path / "e.gguf") is None and pc.eligibility(None)


def manager_settings(monkeypatch, history=True, cache=True):
    settings = sm.PotluckSettings()
    settings.privacy.chat_history_enabled = history
    settings.runtime.prompt_cache = cache
    monkeypatch.setattr(mgr, "get_settings", lambda: settings)


def head_for(model_path):
    return {"model": {"id": "m", "sha256": SHA}, "context": 4096, "workers": [{"backend": "metal"}]}, model_path


def test_the_cache_is_attached_only_for_an_eligible_model_with_history_and_the_switch_on(tmp_path, monkeypatch):
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    manifest = {"commit": "d806193", "backend": "metal"}
    good = gguf(tmp_path / "good.gguf", {"general.architecture": "qwen2"})
    hybrid = gguf(tmp_path / "hybrid.gguf", {"general.architecture": "qwen3", "qwen3.ssm.conv_kernel": 4})

    manager_settings(monkeypatch)
    head, _ = head_for(good)
    cache = mgr.slot_cache_for(head, manifest, good)
    assert cache is not None and cache.slot_dir == pc.slot_root(tmp_path)
    assert (cache.slot_dir.stat().st_mode & 0o777) == 0o700
    assert cache.context == 4096 and cache.sha == SHA
    cache.close()
    assert mgr.slot_cache_for(head, manifest, hybrid) is None
    manager_settings(monkeypatch, history=False)
    assert mgr.slot_cache_for(head, manifest, good) is None
    manager_settings(monkeypatch, cache=False)
    assert mgr.slot_cache_for(head, manifest, good) is None


def test_the_signature_follows_the_engine_and_the_worker_set(tmp_path, monkeypatch):
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    manager_settings(monkeypatch)
    good = gguf(tmp_path / "good.gguf", {"general.architecture": "qwen2"})
    made = []
    for commit, workers in (("d806193", ["metal"]), ("d806193", ["cuda"]), ("e111111", ["metal"]), ("d806193", ["metal"])):
        head = {"model": {"id": "m", "sha256": SHA}, "context": 4096, "workers": [{"backend": b} for b in workers]}
        made.append(mgr.slot_cache_for(head, {"commit": commit, "backend": "metal"}, good))
    sigs = [c.signature for c in made]
    assert sigs[0] == sigs[3] and len({sigs[0], sigs[1], sigs[2]}) == 3
    for c in made:
        c.close()


def test_head_arguments_pass_the_slot_directory_only_when_given(tmp_path):
    base = mgr.head_arguments(tmp_path, tmp_path / "m.gguf", 18400, 4096, 4, ["127.0.0.1:1"], [])
    assert "--slot-save-path" not in base and base[base.index("--parallel") + 1] == "1"
    with_slots = mgr.head_arguments(tmp_path, tmp_path / "m.gguf", 18400, 4096, 4, ["127.0.0.1:1"], [], tmp_path / "slots")
    assert with_slots[with_slots.index("--slot-save-path") + 1] == str(tmp_path / "slots")
    assert with_slots[with_slots.index("--parallel") + 1] == "1"


def test_slot_file_reader_decodes_the_packed_state_and_the_plain_list(tmp_path):
    packed = tmp_path / "p.slot"
    words = [-1, 1, 3, 11, 12, 13, 0]
    packed.write_bytes(struct.pack("<III", pc._SLOT_MAGIC, 1, len(words)) + struct.pack(f"<{len(words)}i", *words))
    assert pc.read_slot_tokens(packed) == [11, 12, 13]
    plain = tmp_path / "q.slot"
    plain.write_bytes(struct.pack("<III", pc._SLOT_MAGIC, 1, 3) + struct.pack("<3i", 5, 6, 7))
    assert pc.read_slot_tokens(plain) == [5, 6, 7]
    media = tmp_path / "m.slot"
    words = [-1, 1, 2, 5, 6, 1, 99]  # one media chunk: a text-only cache must refuse it
    media.write_bytes(struct.pack("<III", pc._SLOT_MAGIC, 1, len(words)) + struct.pack(f"<{len(words)}i", *words))
    with pytest.raises(ValueError):
        pc.read_slot_tokens(media)


def test_slot_file_reader_rejects_other_files(tmp_path):
    bad = tmp_path / "x.slot"
    bad.write_bytes(struct.pack("<III", 1, 1, 2) + b"\x00" * 8)
    with pytest.raises(ValueError):
        pc.read_slot_tokens(bad)
    cut = tmp_path / "y.slot"
    cut.write_bytes(struct.pack("<III", pc._SLOT_MAGIC, 1, 5) + b"\x00" * 8)
    with pytest.raises(ValueError):
        pc.read_slot_tokens(cut)

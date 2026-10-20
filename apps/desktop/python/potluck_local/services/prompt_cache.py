"""Disk prompt cache: resume a long prompt after a restart or a model swap.

Entries are keyed by the model file's SHA-256 plus a hash chain over the
prompt's token blocks, the way a production prefix cache is. Block i's hash
covers every token before it, so one hash identifies a whole prefix. A request
restores the saved state that shares the most leading blocks with it, and the
engine's own prefix match then trims the state to the exact common length.

One machine, one directory:

    <data_dir>/prompt-cache/index.sqlite
    <data_dir>/prompt-cache/<model sha256>/<entry id>.entry

Rules this module enforces on its own:
  * An entry for one model never loads into another. The model digest is in
    the hash chain, in the directory name and in the entry header, and the
    header is checked on every read.
  * Entries older than the TTL since their last use are removed, then the
    least recently used entries are removed until the total is under the cap.
  * A file that is truncated, from another format version or from a different
    engine build is treated as a miss and deleted. It is never loaded.
  * Only models whose saved state can be trimmed back to a shorter prefix are
    eligible (see eligibility()). Hybrid, recurrent and sliding window models
    are not: a partial rollback of their state is not available through
    llama-cpp-python 0.3.x, so a restored entry would be useless or wrong.

The in-process adapter is PromptDiskCache, a llama-cpp-python cache object
(Llama.set_cache). Nothing here imports llama_cpp or torch at module import.
"""
from __future__ import annotations

import array
import hashlib
import json
import logging
import os
import queue
import shutil
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
MAGIC = b"PLPC1\x00\x00\x00"
BLOCK_TOKENS = 256
# Below this a prefill is quicker than reading the state back.
MIN_TOKENS = 512
# Measured entry sizes at ~8.2K tokens: 237 MB for qwen-2.5-1.5b (28.7 KB/token) and
# 101 MB for qwen-2.5-0.5b (12.3 KB/token). By the KV formula (layers * 2 * kv_heads *
# head_dim * 2 bytes) an 8B model with 8 KV heads costs about 147 KB/token, 1.2 GB per 8K.
# 8 GiB holds about 33 small-model sessions, or 6 sessions of an 8B model at 8K tokens,
# which covers the handful of agent sessions one person keeps open. Larger is the user's
# call (runtime.prompt_cache_max_gib); the free-space floor below protects the disk.
DEFAULT_MAX_BYTES = 8 * 1024 ** 3
DEFAULT_TTL_SECONDS = 3 * 24 * 3600
# Free space kept untouched on the volume when deciding whether to write.
FREE_SPACE_FLOOR = 2 * 1024 ** 3

# Architectures whose KV state can be trimmed to any shorter prefix. Anything
# else (recurrent, hybrid, sliding window) is left out until it is verified.
TRIMMABLE_ARCHITECTURES = frozenset({
    "llama", "qwen2", "qwen3", "qwen3moe", "gemma", "granite", "starcoder2", "phi3",
})
_STATEFUL_KEY_PARTS = (".ssm.", ".rwkv.", ".wkv.", ".conv_kernel", ".linear_attention",
                       ".full_attention_interval", ".shortconv", ".recurrent")


def eligibility(metadata: dict | None) -> str | None:
    """None when a model's prompt state can be cached, otherwise the reason."""
    if not metadata:
        return "model metadata unavailable"
    arch = str(metadata.get("general.architecture", ""))
    if arch not in TRIMMABLE_ARCHITECTURES:
        return f"architecture {arch or 'unknown'} is not verified for state restore"
    for key, value in metadata.items():
        if any(part in key for part in _STATEFUL_KEY_PARTS):
            return "model keeps recurrent state"
        if key.endswith(".attention.sliding_window") or key.endswith(".attention.sliding_window_pattern"):
            try:
                if int(value) > 0:
                    return "model uses sliding window attention"
            except (TypeError, ValueError):
                return "model uses sliding window attention"
    return None


def cache_root(data_dir: Path) -> Path:
    return Path(data_dir) / "prompt-cache"


def chain_hashes(model_sha256: str, signature: str, tokens, block: int = BLOCK_TOKENS) -> list[bytes]:
    """Hash after each complete block of tokens. Entry k covers (k+1)*block tokens."""
    seed = hashlib.sha256(f"potluck-prompt-cache/{FORMAT_VERSION}/{model_sha256}/{signature}".encode()).digest()
    out = []
    prev = seed
    usable = len(tokens) // block * block
    for start in range(0, usable, block):
        chunk = array.array("i", tokens[start:start + block]).tobytes()
        prev = hashlib.sha256(prev + chunk).digest()
        out.append(prev)
    return out


def entry_id(model_sha256: str, signature: str, tokens) -> str:
    h = hashlib.sha256(f"{model_sha256}/{signature}/".encode())
    h.update(array.array("i", tokens).tobytes())
    return h.hexdigest()[:32]


@dataclass
class Hit:
    entry_id: str
    path: Path
    n_tokens: int
    matched_tokens: int


class PromptCacheStore:
    """The files, the index, eviction and expiry. Thread-safe."""

    def __init__(self, root: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 ttl_seconds: float = DEFAULT_TTL_SECONDS, block: int = BLOCK_TOKENS,
                 min_tokens: int = MIN_TOKENS, clock=time.time):
        self.root = Path(root)
        self.max_bytes = int(max_bytes)
        self.ttl = float(ttl_seconds)
        self.block = block
        self.min_tokens = min_tokens
        self._clock = clock
        self._lock = threading.RLock()
        self._db = None

    # -- index ---------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        if self._db is None:
            self.root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
            db = sqlite3.connect(str(self.root / "index.sqlite"), check_same_thread=False, isolation_level=None)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS entries (id TEXT PRIMARY KEY, model_sha TEXT NOT NULL, "
                       "signature TEXT NOT NULL, n_tokens INTEGER NOT NULL, size_bytes INTEGER NOT NULL, "
                       "created REAL NOT NULL, last_used REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS blocks (chain_hash BLOB NOT NULL, entry_id TEXT NOT NULL, "
                       "n_tokens INTEGER NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS blocks_hash ON blocks(chain_hash)")
            db.execute("CREATE INDEX IF NOT EXISTS blocks_entry ON blocks(entry_id)")
            self._db = db
        return self._db

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def _path(self, model_sha256: str, eid: str) -> Path:
        return self.root / model_sha256 / f"{eid}.entry"

    # -- entry file ----------------------------------------------------

    @staticmethod
    def _encode(header: dict, tokens, state: bytes) -> list[bytes]:
        head = json.dumps(header, separators=(",", ":")).encode()
        return [MAGIC, struct.pack("<I", len(head)), head, array.array("i", tokens).tobytes(), state]

    def _read_header(self, f) -> tuple[dict, int]:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError("bad magic")
        (head_len,) = struct.unpack("<I", f.read(4))
        if head_len > 1 << 20:
            raise ValueError("header too large")
        header = json.loads(f.read(head_len))
        return header, len(MAGIC) + 4 + head_len

    # -- operations ----------------------------------------------------

    def lookup(self, model_sha256: str, signature: str, tokens, *, min_matched: int = 0) -> Hit | None:
        """The saved entry sharing the longest block-aligned prefix with tokens."""
        hashes = chain_hashes(model_sha256, signature, tokens, self.block)
        if not hashes:
            return None
        with self._lock:
            db = self._open()
            marks = ",".join("?" * len(hashes))
            rows = db.execute(
                f"SELECT b.entry_id, MAX(b.n_tokens), e.n_tokens FROM blocks b JOIN entries e ON e.id = b.entry_id "
                f"WHERE b.chain_hash IN ({marks}) AND e.model_sha = ? AND e.signature = ? "
                f"GROUP BY b.entry_id ORDER BY MAX(b.n_tokens) DESC, e.last_used DESC",
                [*hashes, model_sha256, signature]).fetchall()
            for eid, matched, total in rows:
                if matched <= min_matched:
                    break
                path = self._path(model_sha256, eid)
                if self._valid(path, model_sha256, signature, total, tokens, matched):
                    return Hit(eid, path, total, matched)
                self._remove(eid)
        return None

    def _valid(self, path: Path, model_sha256, signature, n_tokens, tokens, matched) -> bool:
        try:
            with open(path, "rb") as f:
                header, offset = self._read_header(f)
                if (header.get("format") != FORMAT_VERSION or header.get("model_sha256") != model_sha256
                        or header.get("signature") != signature or header.get("n_tokens") != n_tokens):
                    return False
                saved = array.array("i")
                saved.frombytes(f.read(n_tokens * 4))
                if len(saved) != n_tokens or saved.tolist()[:matched] != list(tokens[:matched]):
                    return False
                if path.stat().st_size != offset + n_tokens * 4 + header["state_bytes"]:
                    return False
            return True
        except (OSError, ValueError, KeyError, struct.error):
            return False

    def read(self, hit: Hit) -> tuple[list[int], bytes]:
        """Tokens and state of a hit. Marks the entry used."""
        with open(hit.path, "rb") as f:
            header, _ = self._read_header(f)
            saved = array.array("i")
            saved.frombytes(f.read(header["n_tokens"] * 4))
            state = f.read(header["state_bytes"])
        if len(state) != header["state_bytes"]:
            raise ValueError("truncated state")
        with self._lock:
            self._open().execute("UPDATE entries SET last_used = ? WHERE id = ?", (self._clock(), hit.entry_id))
        return saved.tolist(), state

    def export_state(self, hit: Hit, dest: Path) -> list[int]:
        """Copy a hit's state payload to dest (for llama-server to restore). Returns its tokens."""
        with open(hit.path, "rb") as f:
            header, _ = self._read_header(f)
            saved = array.array("i")
            saved.frombytes(f.read(header["n_tokens"] * 4))
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(f, out, 1 << 24)
        if Path(dest).stat().st_size != header["state_bytes"]:
            raise ValueError("truncated state")
        with self._lock:
            self._open().execute("UPDATE entries SET last_used = ? WHERE id = ?", (self._clock(), hit.entry_id))
        return saved.tolist()

    def discard(self, entry_id_: str) -> None:
        with self._lock:
            self._remove(entry_id_)

    def save(self, model_sha256: str, signature: str, tokens, state: "bytes | Path") -> str | None:
        """Store a state covering exactly these tokens. None when not stored.

        state is the bytes, or the path of a file holding them (a llama-server slot
        file); a path is copied in, never read whole into memory.
        """
        n = len(tokens)
        if n < self.min_tokens or n < self.block:
            return None
        state_len = len(state) if isinstance(state, (bytes, bytearray)) else Path(state).stat().st_size
        size = len(MAGIC) + 4 + 200 + n * 4 + state_len
        if size > self.max_bytes:
            return None
        eid = entry_id(model_sha256, signature, tokens)
        path = self._path(model_sha256, eid)
        with self._lock:
            db = self._open()
            self._sweep_locked()
            if db.execute("SELECT 1 FROM entries WHERE id = ?", (eid,)).fetchone() and path.exists():
                db.execute("UPDATE entries SET last_used = ? WHERE id = ?", (self._clock(), eid))
                return eid
            self._make_room_locked(size)
            try:
                free = shutil.disk_usage(self.root).free
            except OSError:
                free = 0
            if free < size + FREE_SPACE_FLOOR:
                logger.info("prompt_cache_skip reason=low_disk free_bytes=%d need_bytes=%d", free, size)
                return None
            header = {"format": FORMAT_VERSION, "model_sha256": model_sha256, "signature": signature,
                      "n_tokens": n, "state_bytes": state_len, "created": self._clock()}
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            tmp = path.with_suffix(".tmp")
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    if isinstance(state, (bytes, bytearray)):
                        for part in self._encode(header, tokens, state):
                            f.write(part)
                    else:
                        for part in self._encode(header, tokens, b""):
                            f.write(part)
                        with open(state, "rb") as src:
                            shutil.copyfileobj(src, f, 1 << 24)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except OSError:
                with _suppress():
                    tmp.unlink()
                logger.warning("prompt_cache_write_failed", exc_info=True)
                return None
            actual = path.stat().st_size
            now = self._clock()
            hashes = chain_hashes(model_sha256, signature, tokens, self.block)
            db.execute("BEGIN")
            db.execute("INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?,?)",
                       (eid, model_sha256, signature, n, actual, now, now))
            db.execute("DELETE FROM blocks WHERE entry_id = ?", (eid,))
            db.executemany("INSERT INTO blocks VALUES (?,?,?)",
                           [(h, eid, (i + 1) * self.block) for i, h in enumerate(hashes)])
            db.execute("COMMIT")
            self._supersede_locked(eid, model_sha256, signature, tokens, hashes)
            self._make_room_locked(0, keep=eid)
        return eid

    # -- eviction ------------------------------------------------------

    def _remove(self, eid: str) -> None:
        db = self._open()
        row = db.execute("SELECT model_sha FROM entries WHERE id = ?", (eid,)).fetchone()
        db.execute("DELETE FROM blocks WHERE entry_id = ?", (eid,))
        db.execute("DELETE FROM entries WHERE id = ?", (eid,))
        if row:
            with _suppress():
                self._path(row[0], eid).unlink()

    def _supersede_locked(self, eid, model_sha256, signature, tokens, hashes) -> None:
        """A saved state that is a strict prefix of this one is no longer needed."""
        if not hashes:
            return
        db = self._open()
        marks = ",".join("?" * len(hashes))
        rows = db.execute(
            f"SELECT DISTINCT e.id, e.n_tokens FROM entries e JOIN blocks b ON b.entry_id = e.id "
            f"WHERE b.chain_hash IN ({marks}) AND e.id != ? AND e.model_sha = ? AND e.signature = ? "
            f"AND e.n_tokens < ?", [*hashes, eid, model_sha256, signature, len(tokens)]).fetchall()
        for other, n_other in rows:
            if self._is_prefix(self._path(model_sha256, other), n_other, tokens):
                self._remove(other)

    def _is_prefix(self, path: Path, n_other: int, tokens) -> bool:
        try:
            with open(path, "rb") as f:
                header, _ = self._read_header(f)
                saved = array.array("i")
                saved.frombytes(f.read(n_other * 4))
            return len(saved) == n_other and saved.tolist() == list(tokens[:n_other])
        except (OSError, ValueError, KeyError, struct.error):
            return False

    def _sweep_locked(self) -> None:
        db = self._open()
        cutoff = self._clock() - self.ttl
        for (eid,) in db.execute("SELECT id FROM entries WHERE last_used < ?", (cutoff,)).fetchall():
            self._remove(eid)

    def _make_room_locked(self, incoming: int, keep: str | None = None) -> None:
        db = self._open()
        total = db.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM entries").fetchone()[0]
        if total + incoming <= self.max_bytes:
            return
        for eid, size in db.execute("SELECT id, size_bytes FROM entries ORDER BY last_used ASC").fetchall():
            if eid == keep:
                continue
            self._remove(eid)
            total -= size
            if total + incoming <= self.max_bytes:
                break

    def sweep(self) -> None:
        """Expire old entries and drop files the index does not know about."""
        with self._lock:
            self._sweep_locked()
            self._make_room_locked(0)
            db = self._open()
            known = {r[0] for r in db.execute("SELECT id FROM entries")}
            for directory in self.root.iterdir() if self.root.exists() else []:
                if directory.is_dir():
                    for f in directory.iterdir():
                        if f.suffix == ".tmp" or (f.suffix == ".entry" and f.stem not in known):
                            with _suppress():
                                f.unlink()
            for eid, sha in db.execute("SELECT id, model_sha FROM entries").fetchall():
                if not self._path(sha, eid).exists():
                    self._remove(eid)

    def stats(self) -> dict:
        with self._lock:
            n, size = self._open().execute("SELECT COUNT(*), COALESCE(SUM(size_bytes),0) FROM entries").fetchone()
        return {"entries": n, "bytes": size, "max_bytes": self.max_bytes, "ttl_seconds": self.ttl}

    def clear(self) -> None:
        with self._lock:
            for (eid,) in self._open().execute("SELECT id FROM entries").fetchall():
                self._remove(eid)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def engine_signature(model) -> str:
    """What must match for a saved state to load into this context."""
    import llama_cpp

    params = getattr(model, "context_params", None)
    parts = {"llama_cpp": getattr(llama_cpp, "__version__", "?"), "n_vocab": int(model.n_vocab())}
    for name in ("type_k", "type_v", "flash_attn", "flash_attn_type", "swa_full", "kv_unified"):
        if hasattr(params, name):
            parts[name] = int(getattr(params, name))
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


class _Restored:
    """What Llama._create_completion reads back from cache[...]: only input_ids."""

    def __init__(self, input_ids):
        self.input_ids = input_ids


class PromptDiskCache:
    """Adapter for Llama.set_cache(). Restores on lookup, saves after a turn.

    llama-cpp-python reads cache[prompt_tokens] before it generates and writes
    cache[prompt + completion] = save_state() after. The lookup loads the saved
    state straight into the model, so a state that fails to load is deleted and
    reported as a miss instead of failing the request.
    """

    capacity_bytes = 0

    def __init__(self, store: PromptCacheStore, model, model_sha256: str):
        self.store, self.model, self.sha = store, model, model_sha256
        self.signature = engine_signature(model)
        self._queue: queue.Queue = queue.Queue(maxsize=2)
        self._writer = threading.Thread(target=self._run, name="prompt-cache-writer", daemon=True)
        self._writer.start()
        self.last_restore: dict | None = None
        self.last_save: dict | None = None

    @property
    def cache_size(self) -> int:
        return self.store.stats()["bytes"]

    # -- restore -------------------------------------------------------

    def __contains__(self, key) -> bool:
        return self.store.lookup(self.sha, self.signature, list(key)) is not None

    def __getitem__(self, key):
        tokens = list(key)
        model = self.model
        live = _common_prefix(model.input_ids[: model.n_tokens], tokens)
        hit = self.store.lookup(self.sha, self.signature, tokens, min_matched=live)
        if hit is None or hit.n_tokens > model.n_ctx():
            raise KeyError("prompt cache miss")
        started = time.monotonic()
        try:
            saved, state = self.store.read(hit)
            _load_into(model, saved, state)
        except Exception:  # noqa: BLE001 - a bad entry must not fail the request
            logger.warning("prompt_cache_restore_failed entry=%s", hit.entry_id, exc_info=True)
            _clear(model)
            self.store.discard(hit.entry_id)
            raise KeyError("prompt cache entry unusable") from None
        self.last_restore = {"entry": hit.entry_id, "saved_tokens": hit.n_tokens, "matched_tokens": hit.matched_tokens,
                             "seconds": round(time.monotonic() - started, 3), "bytes": len(state)}
        logger.info("prompt_cache_restore entry=%s tokens=%d matched=%d seconds=%.3f", hit.entry_id, hit.n_tokens,
                    hit.matched_tokens, self.last_restore["seconds"])
        import numpy as np

        return _Restored(np.asarray(saved, dtype=np.intc))

    # -- save ----------------------------------------------------------

    def __setitem__(self, key, state) -> None:
        # The state's own tokens say what its KV holds; the key can be one longer.
        n = int(state.n_tokens)
        if n < self.store.min_tokens:
            return
        self._queue.put((state.input_ids[:n].tolist(), bytes(state.llama_state)))

    def save_live(self) -> None:
        """Queue the model's current state, for a turn that did not finish normally."""
        model = self.model
        n = int(model.n_tokens)
        if n < self.store.min_tokens:
            return
        tokens = model.input_ids[:n].tolist()
        if self._stored(tokens):
            return
        self._queue.put((tokens, _state_bytes(model)))

    def _stored(self, tokens) -> bool:
        eid = entry_id(self.sha, self.signature, tokens)
        with self.store._lock:
            return bool(self.store._open().execute("SELECT 1 FROM entries WHERE id = ?", (eid,)).fetchone())

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                started = time.monotonic()
                tokens, state = item
                eid = self.store.save(self.sha, self.signature, tokens, state)
                if eid:
                    self.last_save = {"entry": eid, "tokens": len(tokens), "bytes": len(state),
                                      "seconds": round(time.monotonic() - started, 3)}
                    logger.info("prompt_cache_save entry=%s tokens=%d seconds=%.3f", eid, len(tokens),
                                self.last_save["seconds"])
            except Exception:  # noqa: BLE001
                logger.warning("prompt_cache_save_failed", exc_info=True)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 30.0) -> None:
        """Wait for queued saves. Called before the model is released."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    def close(self) -> None:
        """Save the live state if it is not stored, wait for writes, stop the writer."""
        try:
            self.save_live()
        except Exception:  # noqa: BLE001
            logger.warning("prompt_cache_final_save_failed", exc_info=True)
        self.flush()
        with _suppress():
            self._queue.put_nowait(None)


def _common_prefix(a, b) -> int:
    n = 0
    for x, y in zip(a, b):
        if int(x) != int(y):
            break
        n += 1
    return n


def _state_bytes(model) -> bytes:
    import ctypes

    import llama_cpp

    ctx = model._ctx.ctx
    size = int(llama_cpp.llama_state_get_size(ctx))
    buf = (ctypes.c_uint8 * size)()
    written = int(llama_cpp.llama_state_get_data(ctx, buf, size))
    if written > size:
        raise RuntimeError("Failed to copy llama state data")
    return bytes(memoryview(buf)[:written])


def _clear(model) -> None:
    model.reset()
    with _suppress():
        model._ctx.kv_cache_clear()


def _load_into(model, tokens, state: bytes) -> None:
    import ctypes

    import llama_cpp
    import numpy as np

    n = len(tokens)
    buf = (ctypes.c_uint8 * len(state)).from_buffer_copy(state)
    if int(llama_cpp.llama_state_set_data(model._ctx.ctx, buf, len(state))) != len(state):
        raise RuntimeError("Failed to set llama state data")
    model.input_ids[:n] = np.asarray(tokens, dtype=np.intc)
    model.n_tokens = n


class NativeSlotCache:
    """The same store for a managed llama-server (split_native.py), through slot files.

    llama-server is started with --slot-save-path <dir>. A turn is saved with
    POST /slots/0?action=save and moved into the store; a request restores the
    longest saved prefix with POST /slots/0?action=restore. The server keeps its
    own token list in the slot file and matches it against the prompt, so a wrong
    guess here costs a miss and never a wrong answer. Hybrid and sliding window
    models keep their context checkpoints in the slot file since llama.cpp
    b10569+ (#26004), but this has not been run against such a model here, so
    only eligibility() models use it.
    """

    def __init__(self, store: PromptCacheStore, slot_dir: Path, model_sha256: str, signature: str, post):
        # post(path, json) -> dict, raising on a non-200 reply; a thin wrapper over httpx.
        self.store, self.slot_dir, self.sha, self.signature, self._post = store, Path(slot_dir), model_sha256, signature, post
        self.slot_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.slot_dir, 0o700)

    def restore(self, prompt_tokens, *, slot: int = 0) -> dict | None:
        hit = self.store.lookup(self.sha, self.signature, prompt_tokens)
        if hit is None:
            return None
        name = f"restore-{hit.entry_id}.slot"
        path = self.slot_dir / name
        try:
            self.store.export_state(hit, path)
            reply = self._post(f"/slots/{slot}?action=restore", {"filename": name})
        except Exception:  # noqa: BLE001
            logger.warning("prompt_cache_slot_restore_failed entry=%s", hit.entry_id, exc_info=True)
            self.store.discard(hit.entry_id)
            return None
        finally:
            with _suppress():
                path.unlink()
        return {"entry": hit.entry_id, "saved_tokens": hit.n_tokens, "matched_tokens": hit.matched_tokens,
                "n_restored": reply.get("n_restored")}

    def save(self, tokens, *, slot: int = 0) -> str | None:
        if len(tokens) < self.store.min_tokens:
            return None
        name = f"save-{entry_id(self.sha, self.signature, tokens)}.slot"
        path = self.slot_dir / name
        try:
            reply = self._post(f"/slots/{slot}?action=save", {"filename": name})
            if reply.get("n_saved") != len(tokens):
                return None  # the slot holds other tokens than the caller thinks
            return self.store.save(self.sha, self.signature, tokens, path)
        finally:
            with _suppress():
                path.unlink()

"""3-node distributed continuous batching tests.

Extends Phase 2 to a 3-node chain: head -> mid -> tail.  The mid node now relays
op="cb_prefill" and op="cb_step" through its OWN layer-block KV cache, exactly like
the tail does for its layers.

Topology (all in-process over localhost sockets, cpu/fp32):
    head  = layers[0:SPLIT_A]         (drives generation, holds DistributedContinuousBatcher)
    mid   = layers[SPLIT_A:SPLIT_B]   (background thread, serve_mid)
    tail  = layers[SPLIT_B:]          (background thread, serve_tail)

The same correctness invariant as the 2-node tests:
    Each request's output must be token-for-token identical to single-stream decode
    through the SAME 3-node loopback (PipelineParallelEngine.stream_tokens).

Three tests:
  1. CORRECTNESS GATE — 3 concurrent requests, different prompts, different max_new,
     staggered admission.  Each must match the 3-node single-stream oracle.
  2. EVICTION SAFETY — a short request (max_new=3) evicts mid-run; the surviving
     longer request must produce the correct output.
  3. EFFICIENCY — N concurrent requests use fewer chain round-trips than N serial
     single-stream runs, and max_batch > 1 at least once.

All tests are gated on torch + transformers (pytest.importorskip).  cpu/fp32, small
model (Qwen2.5-0.5B-Instruct).  No GPU required.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")

import torch  # noqa: E402

from potluck_local.services import pipeline_parallel as pp  # noqa: E402
from potluck_local.services import pipeline_wire as wire  # noqa: E402
from potluck_local.services.continuous_batch import DistributedContinuousBatcher  # noqa: E402

MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"

# 3-node split of the 24-layer 0.5B model: head=[0:8], mid=[8:16], tail=[16:24].
# Even thirds so no node is trivially empty and both relay layers are exercised.
SPLIT_A = 8   # head/mid boundary
SPLIT_B = 16  # mid/tail boundary


# ---------------------------------------------------------------------------
# Wire key fixture (shared PSK for all three nodes)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _wire_key():
    import os
    saved_key = wire._KEY
    saved_env = os.environ.get("SHARD_PSK")
    os.environ["SHARD_PSK"] = "test-3node-dcb-secret"
    wire.use_key("test-3node-dcb-secret")
    yield
    wire._KEY = saved_key
    if saved_env is None:
        os.environ.pop("SHARD_PSK", None)
    else:
        os.environ["SHARD_PSK"] = saved_env


# ---------------------------------------------------------------------------
# Port helper
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------------------
# Module-level parts cache (avoid reloading the model per test)
# ---------------------------------------------------------------------------

_parts_cache: dict = {}
_parts_lock = threading.Lock()


def _get_parts(role: str):
    with _parts_lock:
        if role not in _parts_cache:
            _parts_cache[role] = pp.load_parts(
                MODEL_ID, SPLIT_A, role,
                device="cpu", dtype=torch.float32, split2=SPLIT_B,
            )
    return _parts_cache[role]


# ---------------------------------------------------------------------------
# 3-node loopback context manager: tail + mid as background threads
# ---------------------------------------------------------------------------

class _LoopbackChain:
    """Spin up a serve_mid + serve_tail pair over localhost.

    Chain: head (caller) -> mid (127.0.0.1:mid_port) -> tail (127.0.0.1:tail_port).
    Both threads are daemon threads and are stopped via threading.Events on exit.
    """

    def __init__(self):
        self.tail_parts = _get_parts("tail")
        self.mid_parts  = _get_parts("mid")
        self.tail_port  = _free_port()
        self.mid_port   = _free_port()

        self._tail_stop  = threading.Event()
        self._tail_ready = threading.Event()
        self._mid_stop   = threading.Event()
        self._mid_ready  = threading.Event()

        self._tail_thread = threading.Thread(
            target=pp.serve_tail,
            args=(self.tail_parts, "cpu"),
            kwargs=dict(port=self.tail_port, timeout=60.0, host="127.0.0.1",
                        stop_event=self._tail_stop, ready_event=self._tail_ready),
            daemon=True,
        )
        self._mid_thread = threading.Thread(
            target=pp.serve_mid,
            args=(self.mid_parts, "cpu"),
            kwargs=dict(port=self.mid_port, peer2="127.0.0.1", port2=self.tail_port,
                        timeout=60.0, host="127.0.0.1",
                        stop_event=self._mid_stop, ready_event=self._mid_ready),
            daemon=True,
        )

    def __enter__(self):
        # Start tail first so the mid can connect to it on startup of its first stream.
        self._tail_thread.start()
        assert self._tail_ready.wait(timeout=30.0), "tail failed to start"
        self._mid_thread.start()
        assert self._mid_ready.wait(timeout=30.0), "mid failed to start"
        return self

    def __exit__(self, *exc):
        self._mid_stop.set()
        self._tail_stop.set()
        self._mid_thread.join(timeout=10.0)
        self._tail_thread.join(timeout=10.0)


# ---------------------------------------------------------------------------
# Engine builder: head block -> mid port
# ---------------------------------------------------------------------------

def _make_engine(mid_port: int) -> pp.PipelineParallelEngine:
    """Build a PipelineParallelEngine using the cached head parts, pointing at the mid."""
    eng = pp.PipelineParallelEngine.__new__(pp.PipelineParallelEngine)
    eng.model_id = MODEL_ID
    eng.split    = SPLIT_A
    eng.split2   = SPLIT_B
    eng.peer     = "127.0.0.1"
    eng.port     = mid_port
    eng.timeout  = 60.0
    eng.device   = "cpu"
    eng.dtype    = torch.float32
    eng.parts    = _get_parts("head")
    eng.n_layers = eng.parts["n_layers"]
    eng.draft    = None
    eng.draft_k  = 4
    from transformers import AutoTokenizer
    eng.tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    return eng


def _single_stream(engine, messages, max_new) -> str:
    return "".join(engine.stream_tokens(messages, max_new=max_new))


def _collect(gen) -> str:
    return "".join(gen)


# ---------------------------------------------------------------------------
# Test 1: CORRECTNESS GATE
# ---------------------------------------------------------------------------

def test_3node_correctness_staggered_admission() -> None:
    """THE GATE.

    3 concurrent requests with different prompts and different max_new, admitted
    with staggered timing, over a 3-node loopback (head -> mid -> tail). Each
    request's output must be token-for-token identical to single-stream decode
    through the SAME 3-node chain.

    Validates:
    - The mid correctly maintains its own per-row cb_cache and relays cb ops.
    - Per-row KV isolation holds across all three nodes.
    - Staggered admission does not change any request's output.
    - Eviction of the short row does not corrupt surviving rows.
    """
    prompts = [
        [{"role": "user", "content": "Reply with: cat"}],
        [{"role": "user", "content": "Reply with: dog"}],
        [{"role": "user", "content": "Reply with: fish"}],
    ]
    max_news = [20, 25, 15]

    with _LoopbackChain() as chain:
        engine = _make_engine(chain.mid_port)

        # Ground truth: single-stream through the same 3-node chain.
        expected = [
            _single_stream(engine, p, m)
            for p, m in zip(prompts, max_news)
        ]

        batcher = DistributedContinuousBatcher(engine)
        results = [None, None, None]

        def run(idx, delay=0.0):
            if delay:
                time.sleep(delay)
            results[idx] = _collect(batcher.submit(prompts[idx], max_new=max_news[idx]))

        t0 = threading.Thread(target=run, args=(0, 0.0))
        t1 = threading.Thread(target=run, args=(1, 0.05))
        t2 = threading.Thread(target=run, args=(2, 0.10))
        t0.start(); t1.start(); t2.start()
        t0.join(timeout=300); t1.join(timeout=300); t2.join(timeout=300)
        batcher.stop()

    for i in range(3):
        assert results[i] is not None, f"request {i} produced no result"
        assert results[i] == expected[i], (
            f"request {i} CORRECTNESS FAIL (3-node):\n"
            f"  distributed: {results[i]!r}\n"
            f"  expected:    {expected[i]!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: EVICTION SAFETY
# ---------------------------------------------------------------------------

def test_3node_eviction_short_does_not_corrupt_long() -> None:
    """A short request (max_new=3) finishes long before the long one (max_new=30).
    After the short row evicts from all three nodes, the long one must still produce
    the single-stream-correct output."""
    short_prompt = [{"role": "user", "content": "Say: yes"}]
    long_prompt  = [{"role": "user", "content": "Count from one to ten slowly."}]

    with _LoopbackChain() as chain:
        engine = _make_engine(chain.mid_port)

        expected_short = _single_stream(engine, short_prompt, 3)
        expected_long  = _single_stream(engine, long_prompt, 30)

        batcher = DistributedContinuousBatcher(engine)
        results = {}

        def run(key, prompt, max_new):
            results[key] = _collect(batcher.submit(prompt, max_new=max_new))

        ts = threading.Thread(target=run, args=("short", short_prompt, 3))
        tl = threading.Thread(target=run, args=("long", long_prompt, 30))
        ts.start(); tl.start()
        ts.join(timeout=300); tl.join(timeout=300)
        batcher.stop()

    assert results["short"] == expected_short, (
        f"short FAIL (3-node):\n"
        f"  distributed: {results['short']!r}\n"
        f"  expected:    {expected_short!r}"
    )
    assert results["long"] == expected_long, (
        f"long FAIL (3-node):\n"
        f"  distributed: {results['long']!r}\n"
        f"  expected:    {expected_long!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: EFFICIENCY
# ---------------------------------------------------------------------------

def test_3node_fewer_round_trips_than_serial() -> None:
    """N concurrent requests must complete in FEWER chain round-trips than N serial
    single-stream runs, and max_batch > 1 must occur at least once.

    Serial bound: N * (1 + max_new) round-trips.
    Batched: roughly 1 + max_new shared decode round-trips (when all requests run
    simultaneously), well below the serial bound when B > 1."""
    N = 3
    max_new = 15
    prompt = [{"role": "user", "content": "Say: hello"}]
    serial_bound = N * (1 + max_new)

    with _LoopbackChain() as chain:
        engine = _make_engine(chain.mid_port)
        batcher = DistributedContinuousBatcher(engine)
        results = [None] * N

        def run(i):
            results[i] = _collect(batcher.submit(prompt, max_new=max_new))

        threads = [threading.Thread(target=run, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=300)
        batcher.stop()
        st = batcher.stats()

    assert st["max_batch_size"] > 1, (
        f"max_batch_size={st['max_batch_size']} — batch never grew above 1; "
        "requests may have serialized"
    )
    assert st["n_round_trips"] < serial_bound, (
        f"n_round_trips={st['n_round_trips']} >= serial_bound={serial_bound}; "
        "distributed batching provided no round-trip reduction over 3-node chain"
    )
    for i, r in enumerate(results):
        assert r is not None and r.strip(), f"request {i} produced empty output"

    print(
        f"\n[3node-efficiency] N={N} max_new={max_new} | "
        f"round_trips={st['n_round_trips']} vs serial_bound={serial_bound} | "
        f"n_steps={st['n_steps']} max_batch={st['max_batch_size']} "
        f"n_generated={st['n_generated']}"
    )

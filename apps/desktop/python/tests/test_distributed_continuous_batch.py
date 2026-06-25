"""Distributed continuous batching tests — Phase 2.

DistributedContinuousBatcher drives a pipeline chain (head + tail) with the same
dynamic admit/evict scheduler as the single-node ContinuousBatcher, but uses one
chain round-trip per decode step rather than a local model forward.

Tested via a 2-node LOOPBACK: the head block (layers[0:SPLIT]) runs in the test
thread via a PipelineParallelEngine, and serve_tail (layers[SPLIT:]) runs in a
background thread over a localhost socket — exactly the loopback harness from
test_pipeline_speculative.py. Both ends share a test PSK.

This loopback validates correctness IN-PROCESS. Full multi-machine validation is
pending the wired fleet.

Three properties are covered:

1. CORRECTNESS GATE (the bar): 3 concurrent requests with DIFFERENT prompts and
   DIFFERENT max_new lengths, with STAGGERED admission. Each request's output must
   be token-for-token identical to that request decoded SINGLE-STREAM through the
   same 2-node chain (PipelineParallelEngine.stream_tokens). Batching + admit/evict
   timing must not change any output.

2. EVICT CORRECTNESS: a short request finishes mid-run; the surviving longer requests
   must produce the correct output. Covered by the staggered mixed-length test
   above and verified explicitly by a short+long pair.

3. EFFICIENCY: N concurrent requests use FEWER chain round-trips than N serial single-
   stream runs (assert via stats). Max batch > 1.

All tests run on cpu/fp32, small model (Qwen2.5-0.5B-Instruct), FAST. No GPU required.
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
SPLIT = 12  # head = layers[0:12]; tail = layers[12:] -- 2-node split of the 0.5B


# ---------------------------------------------------------------------------
# Wire key fixture (shared PSK for both ends of the loopback)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _wire_key():
    import os
    saved_key = wire._KEY
    saved_env = os.environ.get("SHARD_PSK")
    os.environ["SHARD_PSK"] = "test-dcb-swarm-secret"
    wire.use_key("test-dcb-swarm-secret")
    yield
    wire._KEY = saved_key
    if saved_env is None:
        os.environ.pop("SHARD_PSK", None)
    else:
        os.environ["SHARD_PSK"] = saved_env


# ---------------------------------------------------------------------------
# Loopback harness: tail in a background thread, head in the test thread
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _LoopbackTail:
    """Run serve_tail with the Phase 2 cb_prefill/cb_step dispatch on a background
    thread. Both the old greedy paths and the new cb ops hit the SAME serve_tail so
    the Phase 2 tail is backward-compatible."""

    def __init__(self, model_id, split):
        self.parts = pp.load_parts(model_id, split, "tail", device="cpu",
                                   dtype=torch.float32, split2=0)
        self.port = _free_port()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(
            target=pp.serve_tail,
            args=(self.parts, "cpu"),
            kwargs=dict(port=self.port, timeout=60.0, host="127.0.0.1",
                        stop_event=self.stop, ready_event=self.ready),
            daemon=True,
        )

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(timeout=30.0), "tail failed to start listening"
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=10.0)


# Module-level loaded parts to avoid reloading the model per test.
_head_parts = None
_head_parts_lock = threading.Lock()


def _get_head_parts():
    global _head_parts
    with _head_parts_lock:
        if _head_parts is None:
            _head_parts = pp.load_parts(MODEL_ID, SPLIT, "head", device="cpu",
                                        dtype=torch.float32, split2=0)
    return _head_parts


def _make_engine(tail_port):
    """Build a PipelineParallelEngine backed by the shared head parts."""
    eng = pp.PipelineParallelEngine.__new__(pp.PipelineParallelEngine)
    eng.model_id = MODEL_ID
    eng.split = SPLIT
    eng.split2 = 0
    eng.peer = "127.0.0.1"
    eng.port = tail_port
    eng.timeout = 60.0
    eng.device = "cpu"
    eng.dtype = torch.float32
    eng.parts = _get_head_parts()
    from transformers import AutoTokenizer
    eng.tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    eng.n_layers = eng.parts["n_layers"]
    eng.draft = None
    eng.draft_k = 4
    return eng


def _single_stream_text(engine, messages, max_new):
    """Run a single-stream greedy decode over the same 2-node chain and return text."""
    return "".join(engine.stream_tokens(messages, max_new=max_new))


def _collect(gen) -> str:
    return "".join(gen)


# ---------------------------------------------------------------------------
# Test 1: CORRECTNESS GATE
# ---------------------------------------------------------------------------

def test_distributed_correctness_staggered_admission() -> None:
    """THE GATE.

    3 concurrent requests with different prompts and different max_new lengths,
    admitted with staggered timing so req 0 has already decoded a few tokens before
    req 1 and req 2 join. Each request's output must be token-for-token identical to
    what PipelineParallelEngine.stream_tokens produces for the same request run alone.

    This validates:
    - Per-row head KV isolation (left-pad masking does not leak cross-row attention).
    - Per-row tail KV isolation (tail's cb_cache keyed by row_id, not shared).
    - Admission timing does not change any row's output.
    - Eviction of the short row does not corrupt the longer surviving rows.
    """
    prompts = [
        [{"role": "user", "content": "Reply with: cat"}],
        [{"role": "user", "content": "Reply with: dog"}],
        [{"role": "user", "content": "Reply with: fish"}],
    ]
    max_news = [20, 25, 15]

    with _LoopbackTail(MODEL_ID, SPLIT) as tail:
        engine = _make_engine(tail.port)

        # Ground truth: single-stream greedy decode for each request.
        expected = [
            _single_stream_text(engine, p, m)
            for p, m in zip(prompts, max_news)
        ]

        batcher = DistributedContinuousBatcher(engine)
        results = [None, None, None]

        def run(idx, delay=0.0):
            if delay:
                time.sleep(delay)
            gen = batcher.submit(prompts[idx], max_new=max_news[idx])
            results[idx] = _collect(gen)

        # Req 0 starts immediately; req 1 and 2 are staggered.
        t0 = threading.Thread(target=run, args=(0, 0.0))
        t1 = threading.Thread(target=run, args=(1, 0.05))
        t2 = threading.Thread(target=run, args=(2, 0.10))
        t0.start(); t1.start(); t2.start()
        t0.join(timeout=180); t1.join(timeout=180); t2.join(timeout=180)
        batcher.stop()

    for i in range(3):
        assert results[i] is not None, f"request {i} produced no result"
        assert results[i] == expected[i], (
            f"request {i} CORRECTNESS FAIL:\n"
            f"  distributed: {results[i]!r}\n"
            f"  expected:    {expected[i]!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: EVICT CORRECTNESS — explicit short + long pair
# ---------------------------------------------------------------------------

def test_distributed_eviction_short_does_not_corrupt_long() -> None:
    """A very short request (max_new=3) finishes long before the long one (max_new=30).
    After the short row evicts from both head and tail, the long one must still produce
    the single-stream-correct output."""
    short_prompt = [{"role": "user", "content": "Say: yes"}]
    long_prompt  = [{"role": "user", "content": "Count from one to ten slowly."}]

    with _LoopbackTail(MODEL_ID, SPLIT) as tail:
        engine = _make_engine(tail.port)

        expected_short = _single_stream_text(engine, short_prompt, 3)
        expected_long  = _single_stream_text(engine, long_prompt, 30)

        batcher = DistributedContinuousBatcher(engine)
        results = {}

        def run(key, prompt, max_new):
            gen = batcher.submit(prompt, max_new=max_new)
            results[key] = _collect(gen)

        ts = threading.Thread(target=run, args=("short", short_prompt, 3))
        tl = threading.Thread(target=run, args=("long", long_prompt, 30))
        ts.start(); tl.start()
        ts.join(timeout=180); tl.join(timeout=180)
        batcher.stop()

    assert results["short"] == expected_short, (
        f"short FAIL:\n  distributed: {results['short']!r}\n  expected: {expected_short!r}"
    )
    assert results["long"] == expected_long, (
        f"long FAIL:\n  distributed: {results['long']!r}\n  expected: {expected_long!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: EFFICIENCY — fewer round-trips than serial, max_batch > 1
# ---------------------------------------------------------------------------

def test_distributed_fewer_round_trips_than_serial() -> None:
    """N concurrent requests must complete in FEWER chain round-trips than N serial
    single-stream runs, and the batcher must have driven batch > 1 at least once.

    Serial lower bound: each request costs 1 prefill round-trip + max_new decode
    round-trips = 1 + max_new per request; N requests serial = N * (1 + max_new).

    Distributed batched: N prefill round-trips + roughly max_new shared decode
    round-trips (when all requests are active simultaneously) + N prefill round-trips.
    Total << N * (1 + max_new) when B > 1.

    We use identical short prompts (max_new=15) so the oracle is cheap and all
    requests run at nearly the same speed (making batching likely)."""
    N = 3
    max_new = 15
    prompt = [{"role": "user", "content": "Say: hello"}]
    serial_bound = N * (1 + max_new)  # upper bound: serial single-stream for each

    with _LoopbackTail(MODEL_ID, SPLIT) as tail:
        engine = _make_engine(tail.port)
        batcher = DistributedContinuousBatcher(engine)
        results = [None] * N

        def run(i):
            gen = batcher.submit(prompt, max_new=max_new)
            results[i] = _collect(gen)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        batcher.stop()
        st = batcher.stats()

    assert st["max_batch_size"] > 1, (
        f"max_batch_size={st['max_batch_size']} — batch never grew beyond 1; "
        "requests may have serialised"
    )
    assert st["n_round_trips"] < serial_bound, (
        f"n_round_trips={st['n_round_trips']} >= serial_bound={serial_bound}; "
        "distributed batching provided no round-trip reduction"
    )
    for i, r in enumerate(results):
        assert r is not None and r.strip(), f"request {i} produced empty output"

    print(
        f"\n[dcb-efficiency] N={N} max_new={max_new} | "
        f"round_trips={st['n_round_trips']} vs serial_bound={serial_bound} | "
        f"n_steps={st['n_steps']} max_batch={st['max_batch_size']} "
        f"n_generated={st['n_generated']}"
    )

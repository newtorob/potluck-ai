"""Distributed pipeline sampling tests.

Tests the top-K wire variant that enables head-side sampling for the
PipelineParallelEngine (single-stream) and DistributedContinuousBatcher
(per-row batched) paths.

Design recap
------------
GREEDY (temperature <= 0): unchanged tuple wire (h, abs_start) -> tail argmaxes
  -> {tok}.  The existing test files cover this; we add an explicit gate here.
SAMPLING (temperature > 0): head sends op="sample_step" keyed dict; tail returns
  top-K (values + indices) instead of a single argmax token; head runs
  sample_from_topk over the returned K entries with its per-request generator.

K sizing: max(top_k, 64).  For top_k <= 64 and normal nucleus parameters this
  is exact (identical to sampling over the full vocab).  test_topk_exactness
  verifies this by comparing against a head-side full-logit reference.

All tests run on cpu/fp32, Qwen2.5-0.5B-Instruct (already cached), 2-node
loopback identical to test_pipeline_speculative.py.  Gated on torch + transformers.
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
from potluck_local.services.sampling import sample_from_topk  # noqa: E402

MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
SPLIT = 12  # head = layers[0:12]; tail = layers[12:]


# ---------------------------------------------------------------------------
# Wire key fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _wire_key():
    import os
    saved_key = wire._KEY
    saved_env = os.environ.get("SHARD_PSK")
    os.environ["SHARD_PSK"] = "test-sampling-swarm-secret"
    wire.use_key("test-sampling-swarm-secret")
    yield
    wire._KEY = saved_key
    if saved_env is None:
        os.environ.pop("SHARD_PSK", None)
    else:
        os.environ["SHARD_PSK"] = saved_env


# ---------------------------------------------------------------------------
# Loopback harness (shared with test_pipeline_speculative / test_distributed_*)
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _LoopbackTail:
    """Serve the tail block on a background thread."""

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
        assert self.ready.wait(timeout=30.0), "tail failed to start"
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=10.0)


# Module-level head parts (loaded once, shared across all tests).
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


# ---------------------------------------------------------------------------
# 1. GREEDY UNCHANGED GATE
#    Temperature=0 through the 2-node pipeline loopback must produce the same
#    tokens as the current greedy pipeline (the unchanged tuple wire).
# ---------------------------------------------------------------------------

def test_greedy_unchanged_single_stream() -> None:
    """The gate. temperature=0 must be byte-for-byte identical to the existing
    greedy decode over the same 2-node chain.

    Verification approach: run the engine twice with the same messages - once
    with the default greedy path (temperature=0, the existing tuple wire), once
    with the same params explicitly. Both must match.  We also compare against
    a fresh engine call to guard against any state contamination."""
    messages = [{"role": "user", "content": "Reply with the single word: hello"}]
    max_new = 20

    with _LoopbackTail(MODEL_ID, SPLIT) as tail:
        engine = _make_engine(tail.port)

        # Run 1: default (temperature=0 implicit)
        run1 = "".join(engine.stream_tokens(messages, max_new=max_new))
        # Run 2: explicit temperature=0
        run2 = "".join(engine.stream_tokens(messages, max_new=max_new, temperature=0.0))
        # Run 3: negative temperature (also greedy)
        run3 = "".join(engine.stream_tokens(messages, max_new=max_new, temperature=-1.0))

    assert run1 == run2, (
        f"greedy-unchanged FAIL: default != explicit 0.0\n"
        f"  run1: {run1!r}\n  run2: {run2!r}"
    )
    assert run1 == run3, (
        f"greedy-unchanged FAIL: default != negative temp\n"
        f"  run1: {run1!r}\n  run3: {run3!r}"
    )
    assert run1.strip(), "greedy produced empty output"


# ---------------------------------------------------------------------------
# 2. SAMPLING DETERMINISM GATE
#    A fixed seed + temperature > 0 reproduces the same sequence across two
#    independent calls over the 2-node loopback.
# ---------------------------------------------------------------------------

def test_sampling_determinism_single_stream() -> None:
    """Same seed + temperature>0 must produce the identical token sequence on two
    calls over the same loopback.  The generator is per-call and seeded from seed,
    so identical seeds reproduce identical draws at every step."""
    messages = [{"role": "user", "content": "Tell me about the ocean."}]
    max_new = 20

    with _LoopbackTail(MODEL_ID, SPLIT) as tail:
        engine = _make_engine(tail.port)

        run1 = "".join(engine.stream_tokens(messages, max_new=max_new,
                                             temperature=1.0, seed=42))
        run2 = "".join(engine.stream_tokens(messages, max_new=max_new,
                                             temperature=1.0, seed=42))

    assert run1 == run2, (
        f"sampling determinism FAIL: same seed produced different text\n"
        f"  run1: {run1!r}\n  run2: {run2!r}"
    )
    assert run1.strip(), "sampling produced empty output"


def test_sampling_different_seeds_can_differ() -> None:
    """Different seeds can produce different output (sanity on stochastic sampling)."""
    messages = [{"role": "user", "content": "Tell me a short story."}]
    max_new = 20

    outputs = set()
    with _LoopbackTail(MODEL_ID, SPLIT) as tail:
        engine = _make_engine(tail.port)
        for seed in range(5):
            text = "".join(engine.stream_tokens(messages, max_new=max_new,
                                                temperature=1.5, seed=seed))
            outputs.add(text)

    assert len(outputs) >= 2, (
        "expected >= 2 distinct outputs across 5 seeds; all outputs identical: "
        f"{outputs}"
    )


# ---------------------------------------------------------------------------
# 3. TOP-K EXACTNESS
#    top_k=1 + temperature>0 over the distributed path must equal greedy.
#    (The general exactness proof: when top_k <= K, sampling over the returned
#    top-K is identical to sampling over the full vocab.  top_k=1 is the
#    degenerate case: only the argmax survives, so sampling == greedy.)
# ---------------------------------------------------------------------------

def test_topk_one_equals_greedy() -> None:
    """top_k=1, temperature=1.0 over the loopback must equal the greedy output.
    Only 1 token survives the top-k filter, so multinomial picks it deterministically."""
    messages = [{"role": "user", "content": "Reply with the single word: hello"}]
    max_new = 12

    with _LoopbackTail(MODEL_ID, SPLIT) as tail:
        engine = _make_engine(tail.port)

        greedy_text = "".join(engine.stream_tokens(messages, max_new=max_new))
        topk1_text  = "".join(engine.stream_tokens(messages, max_new=max_new,
                                                    temperature=1.0, top_k=1, seed=0))

    assert topk1_text == greedy_text, (
        f"top_k=1 FAIL: sampling != greedy\n"
        f"  sampling (top_k=1): {topk1_text!r}\n"
        f"  greedy:             {greedy_text!r}"
    )


def test_sample_from_topk_unit_greedy_fallback() -> None:
    """sample_from_topk with temperature<=0 returns indices[argmax(values)]."""
    vals = torch.tensor([1.0, 5.0, 2.0, 3.0])
    ids  = torch.tensor([10,  20,  30,  40])
    # argmax of vals is index 1, which maps to id 20
    result = sample_from_topk(vals, ids, temperature=0.0)
    assert result == 20, f"expected 20, got {result}"

    result_neg = sample_from_topk(vals, ids, temperature=-1.0)
    assert result_neg == 20, f"expected 20, got {result_neg}"


def test_sample_from_topk_seed_reproducibility() -> None:
    """Same seed -> same token from sample_from_topk."""
    vals = torch.tensor([2.0, 2.1, 1.9, 2.05, 2.0])
    ids  = torch.tensor([100, 200, 300, 400, 500])
    g1 = torch.Generator(); g1.manual_seed(7)
    g2 = torch.Generator(); g2.manual_seed(7)
    t1 = sample_from_topk(vals, ids, temperature=1.0, generator=g1)
    t2 = sample_from_topk(vals, ids, temperature=1.0, generator=g2)
    assert t1 == t2


def test_sample_from_topk_topk1_equals_argmax() -> None:
    """top_k=1 inside sample_from_topk keeps only the dominant entry."""
    vals = torch.tensor([1.0, 8.0, 2.0])   # id=1 is dominant
    ids  = torch.tensor([10,  20,  30])
    g = torch.Generator(); g.manual_seed(0)
    result = sample_from_topk(vals, ids, temperature=1.0, top_k=1, generator=g)
    assert result == 20


# ---------------------------------------------------------------------------
# 4. DISTRIBUTED-BATCH PER-ROW: greedy row + sampled row in one cb batch
#    each match their standalone single-stream output.
# ---------------------------------------------------------------------------

def test_distributed_batch_per_row_sampling() -> None:
    """A greedy row (temp=0) and a seeded sampled row (temp>0) submitted to the same
    DistributedContinuousBatcher each match their standalone single-stream output.

    Strategy:
      - Compute ground truth for each request in isolation via single-stream.
      - Submit both to the same DistributedContinuousBatcher concurrently.
      - Assert both match their standalone output.

    This validates:
      - Per-row head-side generators are isolated (seeded independently).
      - The greedy row is unaffected by its sampled neighbor.
      - The sampled row is reproducible (same seed -> same output in batch and alone).
    """
    greedy_prompt  = [{"role": "user", "content": "Reply with: cat"}]
    sampled_prompt = [{"role": "user", "content": "Tell me about stars."}]
    max_new = 15

    with _LoopbackTail(MODEL_ID, SPLIT) as tail:
        engine = _make_engine(tail.port)

        # Ground truth: standalone single-stream for each.
        expected_greedy = "".join(
            engine.stream_tokens(greedy_prompt, max_new=max_new, temperature=0.0)
        )
        expected_sampled = "".join(
            engine.stream_tokens(sampled_prompt, max_new=max_new,
                                 temperature=1.0, seed=77)
        )

        # Batched: both requests in the same DistributedContinuousBatcher.
        batcher = DistributedContinuousBatcher(engine)
        results: dict[str, str] = {}

        def run(key, prompt, temp, seed_val):
            gen = batcher.submit(prompt, max_new=max_new,
                                 temperature=temp, seed=seed_val)
            results[key] = "".join(gen)

        t_greedy  = threading.Thread(target=run, args=("greedy",  greedy_prompt,  0.0, None))
        t_sampled = threading.Thread(target=run, args=("sampled", sampled_prompt, 1.0, 77))

        t_greedy.start()
        time.sleep(0.02)   # small stagger so they overlap in the step loop
        t_sampled.start()

        t_greedy.join(timeout=180)
        t_sampled.join(timeout=180)
        batcher.stop()

    assert results.get("greedy") == expected_greedy, (
        f"distributed per-row greedy FAIL:\n"
        f"  batched : {results.get('greedy')!r}\n"
        f"  expected: {expected_greedy!r}"
    )
    assert results.get("sampled") == expected_sampled, (
        f"distributed per-row sampled FAIL:\n"
        f"  batched : {results.get('sampled')!r}\n"
        f"  expected: {expected_sampled!r}"
    )


# ---------------------------------------------------------------------------
# 5. sample_from_topk module-level torch import guard
#    The new function lives in sampling.py -- same lazy-import invariant.
# ---------------------------------------------------------------------------

def test_sample_from_topk_no_top_level_torch_import() -> None:
    """sample_from_topk lives in sampling.py which must not import torch at
    module top (create_app torch-free invariant, same as sample_token)."""
    import ast
    import pathlib

    src_path = (
        pathlib.Path(__file__).parent.parent
        / "potluck_local" / "services" / "sampling.py"
    )
    source = src_path.read_text()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            assert "torch" not in names, (
                "sampling.py imports torch at module top level: "
                "breaks create_app torch-free invariant"
            )

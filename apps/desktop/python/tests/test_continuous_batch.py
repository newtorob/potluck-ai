"""Continuous batching tests — Phase 1 single-node ContinuousBatcher.

All tests are gated on torch + transformers and require the
Qwen/Qwen2.5-0.5B-Instruct model in the local HF cache (same as the whole-model
smoke test). They run on CPU / fp32 for determinism; no GPU required.

Three properties are covered:

1. CORRECTNESS GATE (the bar): submit 3 requests with different prompts and
   different lengths, with staggered admission (req 2 and 3 arrive after req 1 has
   already generated a few tokens). Each request's output must be token-for-token
   identical to what it produces in single-stream (greedy determinism). Batching,
   neighbours, and admission timing must NOT change any request's output.

2. EVICTION SAFETY: a short request that finishes early does not corrupt the
   still-running ones (verified both by the correctness gate and by an explicit
   case that mixes a 3-token and a 30-token request).

3. THROUGHPUT/SHARING: N concurrent requests complete in FEWER total forward passes
   than running them serially, and max observed batch size > 1. (Uses stats hook.)
"""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")

import torch  # noqa: E402
from potluck_local.services.pipeline_parallel import WholeModelEngine  # noqa: E402
from potluck_local.services.continuous_batch import ContinuousBatcher  # noqa: E402

MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"


# ---------------------------------------------------------------------------
# Runtime wiring: load API -> ContinuousBatcher reachable through LLMRuntime
# ---------------------------------------------------------------------------

def test_runtime_load_whole_model_continuous_batch_wiring() -> None:
    """The runtime wires continuous_batch end-to-end on the whole-model path:

    - load_whole_model(continuous_batch=True) -> is_loaded() True and the new
      continuous_batch_active() accessor reads True (what /health surfaces).
    - The single batched stream produces non-empty text (batcher with 1 row ==
      single-stream; correctness is already gated by the dedicated tests above).
    - unload_model() stops the batcher: continuous_batch_active() flips back to
      False and is_loaded() is False.
    - The default (continuous_batch=False) load leaves continuous_batch_active()
      False so the single-stream path is unchanged.
    """
    import asyncio

    from potluck_local.services.runtime import LLMRuntime

    rt = LLMRuntime()
    try:
        # Default load: NOT batched.
        rt.load_whole_model(MODEL_ID, device="cpu", dtype="float32")
        assert rt.is_loaded()
        assert rt.continuous_batch_active() is False
        rt.unload_model()
        assert rt.is_loaded() is False

        # Opt-in load: batched.
        result = rt.load_whole_model(
            MODEL_ID, device="cpu", dtype="float32", continuous_batch=True)
        assert result["continuous_batch"] is True
        assert rt.is_loaded()
        assert rt.continuous_batch_active() is True

        # Stream a single chat through the batcher-backed runtime path.
        async def _collect_stream() -> str:
            chunks: list[str] = []
            async for sse in rt.stream_chat(
                [{"role": "user", "content": "Reply with: hello"}], max_tokens=12
            ):
                chunks.append(sse)
            return "".join(chunks)

        out = asyncio.run(_collect_stream())
        assert "[DONE]" in out
        # At least one content delta was emitted (non-empty generation).
        assert '"content"' in out
    finally:
        rt.unload_model()

    assert rt.continuous_batch_active() is False
    assert rt.is_loaded() is False


@pytest.fixture(scope="module")
def engine():
    """One WholeModelEngine shared across all tests in this module (heavy load)."""
    return WholeModelEngine(MODEL_ID, device="cpu", dtype=torch.float32)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _single_stream_tokens(eng: WholeModelEngine, messages: list[dict],
                           max_new: int = 30) -> list[int]:
    """Run single-stream greedy decode; return the list of produced token ids.
    We replicate the WholeModelEngine._argmax_next + loop to get raw token ids
    (not text deltas) for exact comparison."""
    from transformers import DynamicCache
    enc = eng.tokenizer.apply_chat_template(
        messages, add_generation_prompt=True, return_tensors="pt", return_dict=True)
    input_ids = enc["input_ids"].to(eng.device)
    attn = enc.get("attention_mask")
    if attn is not None:
        attn = attn.to(eng.device)
    cache = DynamicCache()
    eos_id = eng.tokenizer.eos_token_id
    produced = []
    with torch.no_grad():
        out = eng.model(input_ids=input_ids, attention_mask=attn,
                        past_key_values=cache, use_cache=True)
        nxt = int(torch.argmax(out.logits[:, -1, :], dim=-1).item())
        for _ in range(max_new):
            if eos_id is not None and nxt == eos_id:
                break
            produced.append(nxt)
            step_ids = torch.tensor([[nxt]], device=eng.device)
            out = eng.model(input_ids=step_ids, past_key_values=cache, use_cache=True)
            nxt = int(torch.argmax(out.logits[:, -1, :], dim=-1).item())
    return produced


def _single_stream_text(eng: WholeModelEngine, messages: list[dict],
                         max_new: int = 30) -> str:
    ids = _single_stream_tokens(eng, messages, max_new=max_new)
    return eng.tokenizer.decode(ids, skip_special_tokens=True)


def _collect(gen) -> str:
    return "".join(gen)


# ---------------------------------------------------------------------------
# Test 1: CORRECTNESS GATE
# ---------------------------------------------------------------------------

def test_correctness_staggered_admission(engine) -> None:
    """Three requests with different prompts and different max_new lengths.

    Req 1 is admitted first. After it has generated a few tokens, req 2 and req 3
    are admitted (staggered). All three must produce exactly the same text as
    single-stream greedy decode — no matter who their neighbours are or when they
    joined.
    """
    prompts = [
        [{"role": "user", "content": "Reply with: cat"}],
        [{"role": "user", "content": "Reply with: dog"}],
        [{"role": "user", "content": "Reply with: fish"}],
    ]
    max_news = [20, 25, 15]

    # Ground truth from single-stream.
    expected = [
        _single_stream_text(engine, p, max_new=m)
        for p, m in zip(prompts, max_news)
    ]

    batcher = ContinuousBatcher(engine)
    results = [None, None, None]

    def run(idx, delay=0.0):
        if delay:
            time.sleep(delay)
        gen = batcher.submit(prompts[idx], max_new=max_news[idx])
        results[idx] = _collect(gen)

    # Req 0 starts immediately; req 1 and 2 are staggered by a small delay so
    # req 0 has had a few decode steps before they arrive.
    t0 = threading.Thread(target=run, args=(0, 0.0))
    t1 = threading.Thread(target=run, args=(1, 0.05))
    t2 = threading.Thread(target=run, args=(2, 0.10))

    t0.start(); t1.start(); t2.start()
    t0.join(timeout=120); t1.join(timeout=120); t2.join(timeout=120)

    batcher.stop()

    for i in range(3):
        assert results[i] is not None, f"request {i} produced no result"
        assert results[i] == expected[i], (
            f"request {i} correctness FAIL:\n"
            f"  batched : {results[i]!r}\n"
            f"  expected: {expected[i]!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: EVICTION SAFETY — short request finishes first, long one continues
# ---------------------------------------------------------------------------

def test_eviction_safety_short_and_long(engine) -> None:
    """A very short request (max_new=3) finishes long before the long one
    (max_new=30). After the short one evicts, the long one must still produce
    the correct output."""
    short_prompt = [{"role": "user", "content": "Say: yes"}]
    long_prompt  = [{"role": "user", "content": "Count from one to ten slowly."}]

    expected_short = _single_stream_text(engine, short_prompt, max_new=3)
    expected_long  = _single_stream_text(engine, long_prompt, max_new=30)

    batcher = ContinuousBatcher(engine)
    results = {}

    def run(key, prompt, max_new):
        gen = batcher.submit(prompt, max_new=max_new)
        results[key] = _collect(gen)

    ts = threading.Thread(target=run, args=("short", short_prompt, 3))
    tl = threading.Thread(target=run, args=("long", long_prompt, 30))
    ts.start(); tl.start()
    ts.join(timeout=120); tl.join(timeout=120)
    batcher.stop()

    assert results["short"] == expected_short, (
        f"short request FAIL:\n  batched: {results['short']!r}\n  expected: {expected_short!r}"
    )
    assert results["long"] == expected_long, (
        f"long request FAIL:\n  batched: {results['long']!r}\n  expected: {expected_long!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: THROUGHPUT — batched forward count < serial forward count
# ---------------------------------------------------------------------------

def test_throughput_fewer_forwards_than_serial(engine) -> None:
    """N concurrent requests complete in FEWER total forward passes than N serial
    runs, and max observed batch size > 1.

    Forward count math:
      Serial: each request costs 1 prefill + max_new decode steps = 1 + max_new.
      Total serial = N * (1 + max_new).
      Batched: each decode step covers ALL active sequences in one forward.
      With B requests sharing steps, decode steps collapse from N*max_new to
      roughly max_new (when all are active simultaneously). Total batched ~
      N prefills + max_new shared decode steps. So batched << serial when B > 1.

    We use identical prompts (same text, different messages dicts) so the single-
    stream oracle is cheap to compute once, and we cap max_new=15 for speed.
    """
    N = 3
    max_new = 15
    prompt = [{"role": "user", "content": "Say: hello"}]

    # Count forward passes for N sequential single-stream runs.
    # Each single-stream run: 1 prefill forward + up to max_new decode forwards.
    # We don't re-run the actual model; we estimate:
    #   serial_forwards = N * (1 + max_new)  (upper bound — EOS may cut short)
    # Use actual batcher stats vs this bound.
    serial_forwards_bound = N * (1 + max_new)

    batcher = ContinuousBatcher(engine)
    threads = []
    results = [None] * N

    def run(i):
        gen = batcher.submit(prompt, max_new=max_new)
        results[i] = _collect(gen)

    for i in range(N):
        t = threading.Thread(target=run, args=(i,))
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    batcher.stop()
    st = batcher.stats()

    assert st["max_batch_size"] > 1, (
        f"max_batch_size={st['max_batch_size']} — batch never grew beyond 1; "
        "requests may have serialised rather than sharing steps"
    )
    assert st["n_forward"] < serial_forwards_bound, (
        f"n_forward={st['n_forward']} >= serial_bound={serial_forwards_bound}; "
        "batching provided no forward-pass reduction"
    )
    # All N outputs should be non-empty.
    for i, r in enumerate(results):
        assert r is not None and r.strip(), f"request {i} produced empty output"

    # Report numbers for the test log.
    print(
        f"\n[throughput] N={N} max_new={max_new} | "
        f"batched_forwards={st['n_forward']} vs serial_bound={serial_forwards_bound} | "
        f"max_batch={st['max_batch_size']} n_generated={st['n_generated']}"
    )


# ---------------------------------------------------------------------------
# Test 4: SINGLE-REQUEST correctness (batcher with one request = single-stream)
# ---------------------------------------------------------------------------

def test_single_request_matches_single_stream(engine) -> None:
    """When only one request is active, the batcher must produce identical output
    to the single-stream path (greedy determinism with no neighbours)."""
    messages = [{"role": "user", "content": "Reply with the single word: hello"}]
    expected = _single_stream_text(engine, messages, max_new=16)

    batcher = ContinuousBatcher(engine)
    result = _collect(batcher.submit(messages, max_new=16))
    batcher.stop()

    assert result == expected, (
        f"single-request FAIL:\n  batched: {result!r}\n  expected: {expected!r}"
    )

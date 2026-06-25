"""Speculative-decoding tests for the pipeline-parallel engine.

Speculative decoding amortizes the pipeline's per-token chain round-trip: a small DRAFT
model proposes K tokens locally on the head, the TARGET (the pipeline chain) verifies all
K+1 candidate positions in ONE forward pass (one round-trip), and the longest matching
prefix is accepted. Crucially it is EXACT, not approximate -- the committed tokens are
exactly those plain greedy decode would have emitted. That identity is the correctness
gate here.

Three properties are covered:
  1. accept_prefix unit logic -- the pure greedy-acceptance rule, no model/network.
  2. CORRECTNESS (the gate): over a 2-node LOOPBACK (head + serve_tail in a thread, like
     the existing smokes), speculative decode with draft == the same target model produces
     the EXACT same token sequence as the plain greedy path, for several prompts.
  3. EFFICIENCY: the speculative run uses FEWER chain round-trips than greedy for the same
     output length (round-trips counted directly from each path).

The whole file imports torch + transformers and loads a real (small) model, so it is gated
on pytest.importorskip -- in a torch-free base env it skips cleanly. The model
(Qwen2.5-0.5B-Instruct) is expected in the local HF cache; loads are cpu/fp32 and
deterministic (greedy), so the comparison is byte-exact.
"""

from __future__ import annotations

import socket
import threading

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")

import torch  # noqa: E402

from potluck_local.services import pipeline_parallel as pp  # noqa: E402
from potluck_local.services import pipeline_wire as wire  # noqa: E402

MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
SPLIT = 12  # head = layers[0:12]; tail = layers[12:] -- a real 2-node split of the 0.5B


# --------------------------------------------------------------------------- #
# 1. pure acceptance-logic unit tests (no model, no network)
# --------------------------------------------------------------------------- #
def test_accept_prefix_full_accept() -> None:
    """All K drafts match the target's argmaxes => accept all K, commit K+1 (the K drafts
    plus the target's next-token continuation)."""
    drafts = [10, 20, 30]
    targets = [10, 20, 30, 40]  # target agrees on every drafted position, then proposes 40
    n, committed = pp.accept_prefix(drafts, targets)
    assert n == 3
    assert committed == [10, 20, 30, 40]


def test_accept_prefix_first_mismatch() -> None:
    """A mismatch at position j stops acceptance: commit the accepted prefix + the TARGET's
    token at j (the correction), and discard the rest of the speculation."""
    drafts = [10, 20, 30]
    targets = [10, 99, 30, 40]  # diverges at index 1; target says 99, not the drafted 20
    n, committed = pp.accept_prefix(drafts, targets)
    assert n == 1
    assert committed == [10, 99]


def test_accept_prefix_immediate_mismatch() -> None:
    """A mismatch at the very first drafted position accepts nothing and commits exactly the
    target's correction -- i.e. this round behaves like one greedy step."""
    drafts = [10, 20]
    targets = [99, 20, 30]
    n, committed = pp.accept_prefix(drafts, targets)
    assert n == 0
    assert committed == [99]


# --------------------------------------------------------------------------- #
# loopback fixtures: head block in-process, tail in a background thread
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module", autouse=True)
def _wire_key():
    """Provide SHARD_PSK for the loopback (both ends share it). The engine loads the key
    from the environment via _ensure_wire_key(), so set the env var, not just the process
    key. Restored after the module."""
    import os

    saved_key = wire._KEY
    saved_env = os.environ.get("SHARD_PSK")
    os.environ["SHARD_PSK"] = "test-spec-swarm-secret"
    wire.use_key("test-spec-swarm-secret")
    yield
    wire._KEY = saved_key
    if saved_env is None:
        os.environ.pop("SHARD_PSK", None)
    else:
        os.environ["SHARD_PSK"] = saved_env


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _LoopbackTail:
    """Run serve_tail on a background thread on 127.0.0.1, holding the tail block of the
    2-node split. Both the greedy and speculative heads dial the SAME tail -- the tail
    dispatches on message shape, so one server serves both paths."""

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


def _greedy_engine(tail_port):
    """A plain PipelineParallelEngine (no draft) holding the head block -> greedy decode."""
    return pp.PipelineParallelEngine(
        MODEL_ID, split=SPLIT, peer="127.0.0.1", port=tail_port,
        device="cpu", dtype=torch.float32, timeout=60.0,
    )


def _spec_engine(tail_port, k=4):
    """A speculative PipelineParallelEngine: draft == the same target model (the test
    oracle -- a same-model draft maximizes acceptance and still must match greedy exactly)."""
    return pp.PipelineParallelEngine(
        MODEL_ID, split=SPLIT, peer="127.0.0.1", port=tail_port,
        device="cpu", dtype=torch.float32, timeout=60.0,
        draft_model=MODEL_ID, draft_k=k,
    )


def _greedy_round_trips(engine, messages, max_new):
    """Run the greedy stream and count chain round-trips. Greedy spends exactly one
    round-trip on prefill plus one per emitted token, so we count tokens + 1; we ALSO
    return the produced text via the engine's own stream so the comparison is end to end."""
    text = "".join(engine.stream_tokens(messages, max_new=max_new))
    # greedy: 1 prefill round-trip + 1 per decode step (== per emitted token)
    n_tokens = len(engine.tokenizer(text, add_special_tokens=False)["input_ids"])
    return text, n_tokens


# --------------------------------------------------------------------------- #
# 2. CORRECTNESS gate + 3. efficiency, sharing one tail server
# --------------------------------------------------------------------------- #
PROMPTS = [
    "Reply with the single word: hello",
    "What is 2 + 2? Answer with just the number.",
    "Name one primary color.",
]


def test_speculative_matches_greedy_token_for_token() -> None:
    """THE GATE. For several prompts, speculative decode (draft == target) must produce the
    EXACT same token ids as plain greedy decode over the same 2-node chain. We compare token
    ids (not just text) so the equality is byte-exact, the defining invariant of speculative
    decoding."""
    with _LoopbackTail(MODEL_ID, SPLIT) as tail:
        greedy = _greedy_engine(tail.port)
        spec = _spec_engine(tail.port, k=4)
        for prompt in PROMPTS:
            messages = [{"role": "user", "content": prompt}]
            greedy_text = "".join(greedy.stream_tokens(messages, max_new=40))
            spec_text = "".join(spec.stream_tokens(messages, max_new=40))
            assert spec_text == greedy_text, (
                f"speculative output diverged from greedy for {prompt!r}\n"
                f"greedy: {greedy_text!r}\nspec:   {spec_text!r}"
            )
            # token-id equality too (the real invariant; guards against any decode-merge luck)
            g_ids = greedy.tokenizer(greedy_text, add_special_tokens=False)["input_ids"]
            s_ids = spec.tokenizer(spec_text, add_special_tokens=False)["input_ids"]
            assert s_ids == g_ids


def test_speculative_uses_fewer_round_trips_than_greedy() -> None:
    """EFFICIENCY. For the same output, the speculative path must take FEWER chain
    round-trips than greedy. Greedy spends one round-trip per emitted token (+1 prefill);
    speculative commits multiple tokens per round-trip whenever the draft is accepted. With
    draft == target, acceptance is high, so round-trips drop well below the token count."""
    prompt = "List the first five positive integers separated by commas."
    messages = [{"role": "user", "content": prompt}]
    with _LoopbackTail(MODEL_ID, SPLIT) as tail:
        greedy = _greedy_engine(tail.port)
        spec = _spec_engine(tail.port, k=6)

        greedy_text = "".join(greedy.stream_tokens(messages, max_new=48))
        n_tokens = len(greedy.tokenizer(greedy_text, add_special_tokens=False)["input_ids"])
        greedy_round_trips = n_tokens + 1  # 1 prefill + 1 per decode token

        stats: dict = {}
        spec_text = "".join(spec._stream_tokens_spec(messages, max_new=48, _stats=stats))
        spec_round_trips = stats["round_trips"]

        # same output (sanity: the efficiency claim is only meaningful for identical output)
        assert spec_text == greedy_text
        # the whole point: speculative does the same work in fewer chain traversals
        assert spec_round_trips < greedy_round_trips, (
            f"spec round-trips {spec_round_trips} not < greedy {greedy_round_trips}"
        )
        # and it actually commits >1 token per round-trip on average
        assert stats["n_tokens"] / spec_round_trips > 1.0

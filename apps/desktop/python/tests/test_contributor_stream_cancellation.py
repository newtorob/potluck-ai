"""Regression test — 2026-07-31 P1 security audit, finding #5.

Two related leaks in the contributor sidecar:

1. LlamaCppInferenceEngine.stream_completion/stream_chat (contributor/
   inference.py) ran a bounded-queue (maxsize=64) producer on a daemon
   thread that pushed tokens via a BLOCKING
   `asyncio.run_coroutine_threadsafe(queue.put(text), loop).result()`, with
   no cancellation and no join. If the consumer (the async generator's
   caller) stops reading before the producer finishes — e.g. because the WS
   send failed and _handle_real_job aborted, or the caller cancelled/aclose()'d
   the generator — the producer thread can be left blocked forever inside
   that `.result()` call once the queue fills, leaking the OS thread AND
   leaving the underlying (not thread-safe) Llama instance's generator
   half-consumed. A later job reusing the same singleton engine could then
   spawn a second producer thread that races the stuck first one on the
   same Llama instance.

2. client.py's _receive_loop spawned job-handler tasks via
   `asyncio.create_task(...)` with no tracking, so a WS disconnect left any
   in-flight handler (and the producer thread underneath it) running
   detached in the background instead of being cancelled.

The fix: a threading.Event the generator's `finally` sets + a queue drain
(to unstick a producer blocked on a full queue) + a bounded join, invoked
whenever the generator is exhausted, errors, OR is abandoned early
(GeneratorExit via aclose()). Plus: _receive_loop now tracks every spawned
task and cancels + awaits them all in its own `finally`.
"""
from __future__ import annotations

import asyncio
import threading

import pytest
import websockets

from potluck_local.contributor.inference import LlamaCppInferenceEngine


class _FakeLlamaManyTokens:
    """Yields far more chunks than the producer's queue (maxsize=64) can
    hold without being drained -- so a consumer that stops reading early is
    guaranteed to catch the producer mid-`queue.put(...).result()`, which is
    exactly the state the real deadlock happens in."""

    def create_completion(self, **kwargs):
        for i in range(500):
            yield {"choices": [{"text": f"tok{i} "}]}

    def create_chat_completion(self, **kwargs):
        for i in range(500):
            yield {"choices": [{"delta": {"content": f"tok{i} "}}]}


def _make_engine(tmp_path) -> LlamaCppInferenceEngine:
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"not a real gguf")
    engine = LlamaCppInferenceEngine(model_path=model_path, model_slug="fake-slug")
    engine._llama = _FakeLlamaManyTokens()  # skip the real (async) load path
    return engine


def _produce_threads() -> list[threading.Thread]:
    """Find the engine's producer thread specifically, by its target
    function's identity -- NOT by Thread.name (the fixed code names it
    "potluck-llamacpp-producer", but that name is part of the fix itself, so
    filtering by it would make this test blind to the pre-fix,
    default-named "Thread-N (_produce)" thread) and NOT by a before/after
    thread-count diff (loop.run_in_executor's default ThreadPoolExecutor
    lazily spins up its own persistent, legitimately-idle worker thread the
    first time anything awaits it -- e.g. the fix's own producer.join() call
    -- which a raw "any new thread" diff would misreport as a leak).
    Threading.Thread stores the callable passed as `target=` on `_target`;
    both the pre-fix and fixed code name that inner function `_produce`,
    so matching on its `__name__` identifies exactly the thread under test
    on either side of the fix.
    """
    return [
        t for t in threading.enumerate()
        if getattr(getattr(t, "_target", None), "__name__", None) == "_produce"
        and t.is_alive()
    ]


@pytest.mark.parametrize("method", ["stream_completion", "stream_chat"])
def test_producer_thread_does_not_leak_when_stream_abandoned_early(tmp_path, method) -> None:
    assert _produce_threads() == [], "a prior test leaked a producer thread"

    engine = _make_engine(tmp_path)
    still_alive_after_abandon: list[bool] = []

    async def scenario() -> None:
        if method == "stream_completion":
            gen = engine.stream_completion(prompt="hello")
        else:
            gen = engine.stream_chat(messages=[{"role": "user", "content": "hi"}])

        got: list[str] = []
        async for item in gen:
            got.append(item)
            if len(got) >= 3:
                break
        assert got  # sanity: we actually got some tokens before abandoning
        # Mirrors what happens when a WS send fails mid-stream or the caller
        # disconnects: the generator is closed without being exhausted.
        await gen.aclose()

        # Check thread liveness while THIS event loop is still running -- not
        # after asyncio.run() returns. asyncio.run()'s own shutdown sequence
        # cancels every outstanding task on the loop, including the pending
        # queue.put(...) coroutine the stuck producer thread is blocked
        # waiting on -- which would incidentally unblock (and thus hide) a
        # leaked pre-fix producer thread as a side effect of the TEST
        # harness tearing down, not because the code under test cleaned up
        # after itself. Polling from inside the still-alive loop is the only
        # way to observe the real, production-faithful hang.
        deadline = asyncio.get_event_loop().time() + 3.0
        while _produce_threads() and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
        still_alive_after_abandon.append(bool(_produce_threads()))

    asyncio.run(scenario())

    assert still_alive_after_abandon == [False], (
        "producer thread was still alive (leaked) after the stream was "
        "abandoned early, checked while its own event loop was still running"
    )


@pytest.mark.parametrize("method", ["stream_completion", "stream_chat"])
def test_engine_reusable_after_abandoned_stream(tmp_path, method) -> None:
    """A second call against the same (singleton-style) engine after an
    abandoned stream must not race a still-alive producer thread from the
    first call -- proof the lock + cleanup make the engine safe to reuse."""
    engine = _make_engine(tmp_path)

    async def abandon_one_stream() -> None:
        gen = (
            engine.stream_completion(prompt="hello")
            if method == "stream_completion"
            else engine.stream_chat(messages=[{"role": "user", "content": "hi"}])
        )
        async for _ in gen:
            break
        await gen.aclose()

    async def run_to_completion() -> list[str]:
        gen = (
            engine.stream_completion(prompt="second")
            if method == "stream_completion"
            else engine.stream_chat(messages=[{"role": "user", "content": "second"}])
        )
        return [item async for item in gen]

    async def scenario() -> list[str]:
        await abandon_one_stream()
        return await asyncio.wait_for(run_to_completion(), timeout=5.0)

    tokens = asyncio.run(scenario())
    assert len(tokens) == 500  # the fake's full output, uncorrupted


# ---------------------------------------------------------------------------
# client.py: spawned job-handler tasks are tracked and torn down on
# disconnect, not left running detached in the background.
# ---------------------------------------------------------------------------


class _FakeWS:
    """Yields one job_assigned message, then raises ConnectionClosed on the
    next recv() -- simulating a peer disconnecting mid-job."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self.sent: list[str] = []

    async def recv(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        # A real websockets.recv() awaits actual network I/O, which yields
        # control back to the event loop -- long enough for a task spawned
        # on the PREVIOUS iteration (asyncio.create_task doesn't run
        # synchronously) to actually start. This fake has no I/O to await,
        # so it has to yield explicitly to be a faithful stand-in.
        await asyncio.sleep(0.05)
        raise websockets.ConnectionClosed(None, None)

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _job_assigned_json() -> str:
    import json

    return json.dumps(
        {
            "type": "job_assigned",
            "job_id": "00000000-0000-0000-0000-0000000000aa",
            "model_slug": "fake-slug",
            "prompt": "hello",
            "messages": [],
            "sampling": {
                "max_tokens": 16,
                "temperature": 0.7,
                "top_p": 0.95,
                "top_k": None,
                "repeat_penalty": None,
                "stop_sequences": None,
                "seed": None,
            },
        }
    )


def test_receive_loop_cancels_in_flight_job_handlers_on_disconnect(monkeypatch) -> None:
    from potluck_local.contributor import client as client_mod
    from potluck_local.contributor.config import Settings

    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()

    async def _hanging_stub_job(ws, job) -> None:  # noqa: ANN001
        handler_started.set()
        try:
            # Simulate a job handler that's still driving generation (e.g.
            # blocked reading from the producer's queue) when the WS dies.
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise

    # The loop now takes its job handler through LoopHooks (M2.5) rather than
    # dispatching on settings; the invariant under test is unchanged.
    settings = Settings(_env_file=None, inference_enabled=False)
    hooks = client_mod.standalone_hooks(settings)
    hooks.job_handler = _hanging_stub_job
    tracker = client_mod.JobTracker()
    ws = _FakeWS([_job_assigned_json()])

    async def scenario() -> None:
        # _receive_loop propagates ConnectionClosed once ws.recv() raises it
        # (unchanged pre/post-fix -- the caller, _session, handles reconnect).
        # What must be true by the time that propagates is that the `finally`
        # has already cancelled + awaited the spawned handler.
        with pytest.raises(websockets.ConnectionClosed):
            await client_mod._receive_loop(ws, hooks, tracker)
        assert tracker.count == 0, "a cancelled handler must not stay counted in flight"

    async def run_with_timeout() -> None:
        await asyncio.wait_for(scenario(), timeout=5.0)
        # _receive_loop must not have propagated until the spawned handler
        # was actually cancelled+awaited -- not merely requested.
        assert handler_started.is_set()
        assert handler_cancelled.is_set()

    asyncio.run(run_with_timeout())

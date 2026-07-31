"""Regression test — 2026-07-31 P1 security audit, finding #4.

routers/models.py called runtime.load_model(...) directly on the event loop
(blocking every other in-flight request for the whole multi-second GGUF
load), and runtime.unload_model() had NO coordination with the
asyncio.Lock (_inference_lock) that stream_chat/chat/stream_chat_raw/
score_sequence/benchmark hold for the full duration of an active generation.
A worker thread could be mid `model.create_chat_completion()` while the
event-loop thread ran unload_model() -> _release_model() -> model.close(),
freeing the llama.cpp handle out from under the still-running worker thread
(use-after-free).

The fix: load_model_async / load_pipeline_async / load_whole_model_async /
unload_model_async on LLMRuntime acquire the SAME _inference_lock generation
holds, and run the actual blocking work in a thread-pool executor. These are
now the only load/unload entrypoints routers/models.py calls.

These tests exercise the concurrency contract directly against LLMRuntime,
with the real llama-cpp-python Llama(...) / .close() calls monkeypatched out
(no GGUF file, no ~/.potluck, needed) — the property under test is lock
ordering and event-loop responsiveness, not model correctness.
"""
from __future__ import annotations

import asyncio
import time

from potluck_local.services.runtime import LLMRuntime


def test_unload_model_async_waits_for_in_flight_generation() -> None:
    """unload_model_async() must not run its body until a generation that's
    already holding _inference_lock has released it. Pre-fix, unload_model()
    had no lock at all and would have run immediately, out of order."""
    rt = LLMRuntime()
    events: list[str] = []

    async def fake_generation() -> None:
        async with rt._inference_lock:
            events.append("gen_start")
            await asyncio.sleep(0.1)
            events.append("gen_end")

    def fake_unload() -> None:
        events.append("unload_ran")

    rt.unload_model = fake_unload  # type: ignore[method-assign]

    async def scenario() -> None:
        gen_task = asyncio.create_task(fake_generation())
        await asyncio.sleep(0.01)  # let the generation acquire the lock first
        await rt.unload_model_async()
        await gen_task

    asyncio.run(scenario())

    assert events == ["gen_start", "gen_end", "unload_ran"]


def test_load_model_async_waits_for_in_flight_generation() -> None:
    """Symmetric case: a load can't swap the model out from under a
    generation that's already running, either."""
    rt = LLMRuntime()
    events: list[str] = []

    async def fake_generation() -> None:
        async with rt._inference_lock:
            events.append("gen_start")
            await asyncio.sleep(0.1)
            events.append("gen_end")

    def fake_load_model(model_path: str, **kwargs) -> dict:
        events.append("load_ran")
        return {"model_id": "fake", "model_path": model_path}

    rt.load_model = fake_load_model  # type: ignore[method-assign]

    async def scenario() -> None:
        gen_task = asyncio.create_task(fake_generation())
        await asyncio.sleep(0.01)
        await rt.load_model_async("fake/path.gguf")
        await gen_task

    asyncio.run(scenario())

    assert events == ["gen_start", "gen_end", "load_ran"]


def test_load_model_async_does_not_block_the_event_loop() -> None:
    """The blocking Llama() construction must run in a thread-pool executor,
    not directly on the event loop -- otherwise every other in-flight
    request (including concurrent ticks of this test's own timer task)
    stalls for the whole load duration."""
    rt = LLMRuntime()

    def slow_blocking_load(model_path: str, **kwargs) -> dict:
        time.sleep(0.3)  # stands in for the real multi-second Llama(...) call
        return {"model_id": "fake"}

    rt.load_model = slow_blocking_load  # type: ignore[method-assign]

    async def scenario() -> int:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        ticker_task = asyncio.create_task(ticker())
        await rt.load_model_async("fake/path.gguf")
        ticker_task.cancel()
        return ticks

    ticks = asyncio.run(scenario())

    # If load_model_async blocked the event loop for the full 0.3s sleep,
    # the ticker task would never get scheduled at all (ticks == 0).
    assert ticks >= 3, f"event loop appears to have been blocked during load (ticks={ticks})"


def test_unload_model_async_does_not_block_the_event_loop() -> None:
    rt = LLMRuntime()

    def slow_blocking_unload() -> None:
        time.sleep(0.3)

    rt.unload_model = slow_blocking_unload  # type: ignore[method-assign]

    async def scenario() -> int:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        ticker_task = asyncio.create_task(ticker())
        await rt.unload_model_async()
        ticker_task.cancel()
        return ticks

    ticks = asyncio.run(scenario())
    assert ticks >= 3, f"event loop appears to have been blocked during unload (ticks={ticks})"


def test_load_pipeline_async_and_load_whole_model_async_share_the_same_lock() -> None:
    """The pipeline/whole-model load paths must go through the same shared
    lock as the GGUF path and inference -- not a parallel, uncoordinated
    entrypoint."""
    rt = LLMRuntime()
    events: list[str] = []

    async def fake_generation() -> None:
        async with rt._inference_lock:
            events.append("gen_start")
            await asyncio.sleep(0.05)
            events.append("gen_end")

    def fake_load_pipeline(model_id: str, **kwargs) -> dict:
        events.append("pipeline_load_ran")
        return {"model_id": model_id}

    def fake_load_whole_model(model_id: str, **kwargs) -> dict:
        events.append("whole_model_load_ran")
        return {"model_id": model_id}

    rt.load_pipeline = fake_load_pipeline  # type: ignore[method-assign]
    rt.load_whole_model = fake_load_whole_model  # type: ignore[method-assign]

    # Both scenarios share one asyncio.run() / one event loop: rt._inference_lock
    # (an asyncio.Lock) binds to whichever loop first uses it, so reusing the
    # same LLMRuntime across two separate asyncio.run() calls would raise --
    # a test-harness artifact, not something production hits (one persistent
    # event loop for the process lifetime).
    pipeline_events: list[str] = []

    async def both_scenarios() -> None:
        gen_task = asyncio.create_task(fake_generation())
        await asyncio.sleep(0.005)
        await rt.load_pipeline_async("some/model")
        await gen_task

        pipeline_events.extend(events)
        events.clear()

        gen_task2 = asyncio.create_task(fake_generation())
        await asyncio.sleep(0.005)
        await rt.load_whole_model_async("some/model")
        await gen_task2

    asyncio.run(both_scenarios())
    assert pipeline_events == ["gen_start", "gen_end", "pipeline_load_ran"]
    assert events == ["gen_start", "gen_end", "whole_model_load_ran"]

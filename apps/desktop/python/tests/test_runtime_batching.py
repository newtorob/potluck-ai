"""Runtime concurrency regressions; no model downloads or GPU required."""

import asyncio
import json
import threading

import pytest

from potluck_local.services.runtime import LLMRuntime


class ControlledBackend:
    """Keep each producer alive until the test explicitly releases it."""

    def __init__(self, loop, names=("one", "two", "three")):
        self.loop = loop
        self.entered = {name: asyncio.Event() for name in names}
        self.release = {name: threading.Event() for name in names}
        self.exited = {name: asyncio.Event() for name in names}
        self.calls = {}
        self.fail = set()

    def submit(self, messages, **kwargs):
        name = messages[0]["content"]
        self.calls[name] = kwargs
        self.loop.call_soon_threadsafe(self.entered[name].set)
        try:
            yield f"{name}:start"
            if not self.release[name].wait(5):
                raise TimeoutError(f"test did not release {name}")
            if name in self.fail:
                raise RuntimeError(f"{name} failed")
            yield f"{name}:end"
        finally:
            self.loop.call_soon_threadsafe(self.exited[name].set)

    stream_tokens = submit

    def create_chat_completion(self, messages, **kwargs):
        for text in self.submit(messages, **kwargs):
            yield {"choices": [{"delta": {"content": text}}]}

    def release_all(self):
        for event in self.release.values():
            event.set()


async def collect(rt, name, **kwargs):
    return [chunk async for chunk in rt.stream_chat(
        [{"role": "user", "content": name}], **kwargs
    )]


def content(chunks):
    return "".join(json.loads(chunk.removeprefix("data: "))["choices"][0]
                   ["delta"]["content"] for chunk in chunks if "choices" in chunk)


async def entered(backend, *names):
    await asyncio.wait_for(asyncio.gather(
        *(backend.entered[name].wait() for name in names)
    ), 2)


def test_batched_runtime_admits_concurrent_streams_and_isolates_sampling():
    async def scenario():
        rt = LLMRuntime()
        backend = rt._batcher = ControlledBackend(asyncio.get_running_loop())
        tasks = [asyncio.create_task(collect(rt, name, temperature=temp,
                    max_tokens=12, top_k=8, top_p=0.9, seed=seed))
                 for name, temp, seed in [("one", 0.0, 11), ("two", 0.7, 22)]]
        try:
            # Neither producer can finish yet. Both must be admitted together.
            await entered(backend, "one", "two")
            assert not any(task.done() for task in tasks)
        finally:
            backend.release_all()
            results = await asyncio.gather(*tasks)
        assert [content(chunks) for chunks in results] == [
            "one:startone:end", "two:starttwo:end"]
        assert all(chunks[-1] == "data: [DONE]\n\n" for chunks in results)
        assert backend.calls == {
            "one": dict(max_new=12, temperature=0.0, top_k=8, top_p=0.9, seed=11),
            "two": dict(max_new=12, temperature=0.7, top_k=8, top_p=0.9, seed=22),
        }
        assert not rt._inference_lock.locked()
    asyncio.run(scenario())


@pytest.mark.parametrize("backend_kind", ["llama", "pipeline"])
def test_unbatched_runtime_still_serializes_streams(backend_kind):
    async def scenario():
        rt = LLMRuntime()
        backend = ControlledBackend(asyncio.get_running_loop())
        setattr(rt, "_model" if backend_kind == "llama" else "_pipeline", backend)
        first = asyncio.create_task(collect(rt, "one"))
        tasks = [first]
        try:
            await entered(backend, "one")
            second = asyncio.create_task(collect(rt, "two"))
            tasks.append(second)
            await asyncio.sleep(0.02)
            assert not backend.entered["two"].is_set()
            backend.release["one"].set()
            await first
            await entered(backend, "two")
        finally:
            backend.release_all()
            await asyncio.gather(*tasks)
        assert not rt._inference_lock.locked()
    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["unload_model", "load_model",
                                   "load_pipeline", "load_whole_model"])
def test_lifecycle_waits_for_all_streams_and_later_requests_use_new_backend(method):
    async def scenario():
        rt = LLMRuntime()
        loop = asyncio.get_running_loop()
        old = rt._batcher = ControlledBackend(loop)
        new = ControlledBackend(loop)
        lifecycle_entered = asyncio.Event()
        lifecycle_release = threading.Event()

        def swap(*args, **kwargs):
            loop.call_soon_threadsafe(lifecycle_entered.set)
            assert lifecycle_release.wait(5)
            rt._batcher = new if method != "unload_model" else None
            return {}

        setattr(rt, method, swap)
        tasks = [asyncio.create_task(collect(rt, name)) for name in ("one", "two")]
        lifecycle = later = None
        try:
            await entered(old, "one", "two")
            args = () if method == "unload_model" else ("new-model",)
            lifecycle = asyncio.create_task(getattr(rt, method + "_async")(*args))
            await asyncio.sleep(0.02)
            later = asyncio.create_task(collect(rt, "three"))
            await asyncio.sleep(0.02)
            assert not lifecycle_entered.is_set()
            assert not old.entered["three"].is_set()
            old.release["one"].set()
            await tasks[0]
            assert not lifecycle_entered.is_set()
            old.release["two"].set()
            await tasks[1]
            await asyncio.wait_for(lifecycle_entered.wait(), 2)
            assert not later.done()
            lifecycle_release.set()
            await lifecycle
            if method != "unload_model":
                await entered(new, "three")
            new.release_all()
            result = await later
            assert not old.entered["three"].is_set()
            if method == "unload_model":
                assert "No model loaded" in "".join(result)
            else:
                assert content(result) == "three:startthree:end"
        finally:
            old.release_all()
            new.release_all()
            lifecycle_release.set()
            await asyncio.gather(*tasks, *([lifecycle] if lifecycle else []),
                                 *([later] if later else []))
        assert not rt._inference_lock.locked()
    asyncio.run(scenario())


@pytest.mark.parametrize("termination", ["error", "cancel", "close"])
def test_ending_one_batched_stream_does_not_stop_another(termination):
    async def scenario():
        rt = LLMRuntime()
        backend = rt._batcher = ControlledBackend(asyncio.get_running_loop())
        first_stream = rt.stream_chat([{"role": "user", "content": "one"}])
        first = None
        second = asyncio.create_task(collect(rt, "two"))
        try:
            if termination == "close":
                await anext(first_stream)
                first = asyncio.create_task(first_stream.aclose())
            else:
                first = asyncio.create_task(collect(rt, "one"))
            await entered(backend, "one", "two")
            if termination == "error":
                backend.fail.add("one")
            elif termination == "cancel":
                first.cancel()
                await asyncio.sleep(0)
                first.cancel()  # cleanup must keep its model lease on repeat cancellation
                await asyncio.sleep(0)
            backend.release["one"].set()
            await first
            await asyncio.wait_for(backend.exited["one"].wait(), 2)
            assert not second.done()
            assert rt._inference_lock.locked()  # second still owns model access
            backend.release["two"].set()
            assert content(await second) == "two:starttwo:end"
            # End/error/cancel paths must leave admission usable.
            backend.release["three"].set()
            assert content(await collect(rt, "three")) == "three:startthree:end"
        finally:
            backend.release_all()
            await first_stream.aclose()
            await asyncio.gather(second, *([first] if first else []), return_exceptions=True)
        assert not rt._inference_lock.locked()
    asyncio.run(scenario())


def test_cancelled_model_swap_keeps_admission_closed_until_worker_finishes():
    async def scenario():
        rt = LLMRuntime()
        loop = asyncio.get_running_loop()
        backend = ControlledBackend(loop)
        loading = asyncio.Event()
        release = threading.Event()

        def load(*args, **kwargs):
            loop.call_soon_threadsafe(loading.set)
            assert release.wait(5)
            rt._batcher = backend

        rt.load_whole_model = load
        lifecycle = asyncio.create_task(rt.load_whole_model_async("new"))
        request = None
        try:
            await asyncio.wait_for(loading.wait(), 2)
            lifecycle.cancel()
            await asyncio.sleep(0)
            lifecycle.cancel()
            request = asyncio.create_task(collect(rt, "one"))
            await asyncio.sleep(0.02)
            assert not request.done()
            assert not backend.entered["one"].is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await lifecycle
            await entered(backend, "one")
            backend.release_all()
            assert content(await request) == "one:startone:end"
        finally:
            release.set()
            backend.release_all()
            await asyncio.gather(lifecycle, *([request] if request else []),
                                 return_exceptions=True)
    asyncio.run(scenario())


def test_cancelling_queued_lifecycle_reopens_batch_admission():
    async def scenario():
        rt = LLMRuntime()
        backend = rt._batcher = ControlledBackend(asyncio.get_running_loop())
        first = asyncio.create_task(collect(rt, "one"))
        second = lifecycle = None
        try:
            await entered(backend, "one")
            lifecycle = asyncio.create_task(rt.unload_model_async())
            await asyncio.sleep(0.02)
            lifecycle.cancel()
            with pytest.raises(asyncio.CancelledError):
                await lifecycle
            second = asyncio.create_task(collect(rt, "two"))
            await entered(backend, "two")
            assert not first.done()
        finally:
            backend.release_all()
            await asyncio.gather(first, *([second] if second else []),
                                 *([lifecycle] if lifecycle else []), return_exceptions=True)
        assert not rt._inference_lock.locked()
    asyncio.run(scenario())


def test_gguf_load_retires_the_previous_batcher(monkeypatch):
    """A swap to GGUF must not leave stream_chat routing to the retired batcher."""
    import sys
    from types import SimpleNamespace

    rt = LLMRuntime()
    rt._atexit_registered = True
    stopped = []
    rt._batcher = SimpleNamespace(stop=lambda: stopped.append(True))
    rt._pipeline = object()
    model = object()
    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=lambda **kw: model))
    # The fake engine has no log hooks to capture.
    monkeypatch.setattr("potluck_local.services.runtime._install_log_capture", lambda: None)

    asyncio.run(rt.load_model_async("/models/new/model.gguf"))

    assert stopped == [True]
    assert rt._pipeline is None
    assert not rt.continuous_batch_active()
    assert rt._model is model

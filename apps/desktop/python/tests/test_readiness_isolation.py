"""A failed GPU probe must never kill or outlive its caller."""
import asyncio
import sys
import threading

import pytest

from potluck_local.services import gpu_probe


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize('mode', ['failure', 'timeout', 'cancel', 'bad_output'])
async def test_probe_reaps_failed_or_cancelled_process(monkeypatch, mode):
    real_spawn = asyncio.create_subprocess_exec
    children = []
    started = asyncio.Event()
    async def spawn(*args, **kwargs):
        code = {
            'failure': 'import os, signal; os.kill(os.getpid(), signal.SIGKILL)',
            'timeout': 'import time; time.sleep(60)',
            'cancel': 'import time; time.sleep(60)',
            'bad_output': 'print("x" * 10000)',
        }[mode]
        child = await real_spawn(sys.executable, '-c', code, **kwargs)
        children.append(child)
        started.set()
        return child
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(gpu_probe, 'PROBE_TIMEOUT', .2)
    task = asyncio.create_task(gpu_probe.compute_probe('cpu'))
    await started.wait()
    if mode == 'cancel':
        assert not (await gpu_probe.compute_probe('cpu'))[0]  # bounded single-flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert not (await task)[0]
    assert children[0].returncode is not None
    assert not gpu_probe._gate.locked()


@pytest.mark.anyio
async def test_loaded_probe_obeys_inference_and_unload_locks():
    from potluck_local.services.runtime import LLMRuntime
    from potluck_local.routers.readiness import _run_inference_probe
    runtime = LLMRuntime()
    entered, release = threading.Event(), threading.Event()
    closed = []
    class Model:
        def create_chat_completion(self, **kwargs):
            entered.set()
            assert release.wait(3)
            yield {'choices': [{'delta': {'content': '1'}}]}
        def close(self):
            closed.append(True)
    runtime._model = Model()
    await runtime._inference_lock.acquire()
    task = asyncio.create_task(_run_inference_probe(runtime))
    await asyncio.sleep(.05)
    assert not entered.is_set()
    runtime._inference_lock.release()
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        unload = asyncio.create_task(runtime.unload_model_async())
        await asyncio.sleep(.05)
        assert not closed
    finally:
        release.set()
    assert (await task)[0]
    await unload
    assert closed


@pytest.mark.anyio
async def test_probe_reports_incomplete_loaded_stream():
    from potluck_local.routers.readiness import _run_inference_probe
    class Runtime:
        async def stream_chat(self, *args, **kwargs):
            yield 'data: {"choices": [{"delta": {"content": "1"}}]}\n\n'
    assert not (await _run_inference_probe(Runtime()))[0]

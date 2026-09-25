import asyncio
from contextlib import suppress
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import threading
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from potluck_local.app import create_app
from potluck_local.services import split_manager as sm, split_native as sn
from potluck_local.services.runtime import LLMRuntime
from potluck_local.services.settings_manager import PotluckSettings
from potluck_local.services.model_lifecycle import ModelLifecycle
from potluck_local.services.native_supervisor import allocation_counter, failure_code


class Process:
    def __init__(self, *args, **kwargs):
        self.closed = False
        self.process = SimpleNamespace(poll=lambda: None)
        self.placement = []
    def heartbeat(self):
        if self.closed:
            raise sn.SplitError("dead")
    def close(self):
        self.closed = True


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    settings = PotluckSettings()
    settings.ui[sm.WORKER_KEY] = "true"
    runtime = LLMRuntime()
    manager = sm.SplitManager()
    async def roster():
        return [{"mesh_ip": "100.64.1.2", "node_name": "worker"}]
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sm, "_manager", manager)
    monkeypatch.setattr(sm, "get_settings", lambda: settings)
    monkeypatch.setattr(sm, "save_settings", lambda new: settings.ui.update(new.ui))
    monkeypatch.setattr(sm, "get_runtime", lambda: runtime)
    monkeypatch.setattr(sm, "get_model_lifecycle", lambda: ModelLifecycle())
    monkeypatch.setattr(sm, "household_peers", roster)
    monkeypatch.setattr(sm, "engine_bundle", lambda: (tmp_path, {**sn.PIN, "backend": "cpu"}))
    monkeypatch.setattr(sm, "NativeProcess", Process)
    return manager, runtime, settings


def test_worker_lease_bound_to_opt_in_member_version_and_expiry(isolated):
    manager, runtime, settings = isolated
    async def run():
        req = {"commit": sn.PIN["commit"], "request_id": str(uuid4())}
        with pytest.raises(sn.SplitError):
            await manager.acquire("192.168.1.2", req)
        with pytest.raises(sn.SplitError):
            await manager.acquire("100.64.1.2", {**req, "commit": "a" * 40})
        lease = await manager.acquire("100.64.1.2", req)
        assert runtime._split_worker_reserved
        repeated = await manager.acquire("100.64.1.2", req)
        assert repeated["lease"] == lease["lease"] and repeated["ttl"] == lease["ttl"]
        assert repeated["freeMemoryBytes"] > 0
        with pytest.raises(sn.SplitError):
            await manager.acquire("100.64.1.2", {**req, "request_id": str(uuid4())})
        for host, token in [("100.64.1.3", lease["lease"]), ("100.64.1.2", "wrong")]:
            with pytest.raises(sn.SplitError):
                manager.authenticate_lease(host, token)
        manager.worker["expires"] = time.monotonic() - 1
        with pytest.raises(sn.SplitError):
            await manager.renew("100.64.1.2", lease["lease"])
        await manager.close()
        assert not runtime._split_worker_reserved
    asyncio.run(run())


def test_disabled_or_loaded_worker_cannot_be_reserved(isolated):
    manager, runtime, settings = isolated
    async def run():
        req = {"commit": sn.PIN["commit"], "request_id": str(uuid4())}
        settings.ui[sm.WORKER_KEY] = "false"
        with pytest.raises(sn.SplitError):
            await manager.acquire("100.64.1.2", req)
        settings.ui[sm.WORKER_KEY] = "true"
        runtime._model = object()
        with pytest.raises(sn.SplitError):
            await manager.acquire("100.64.1.2", req)
        assert manager.worker is None
    asyncio.run(run())


def test_worker_off_kills_owned_process_and_fences_old_token(isolated):
    manager, runtime, settings = isolated
    async def run():
        lease = await manager.acquire("100.64.1.2", {"commit": sn.PIN["commit"], "request_id": str(uuid4())})
        process = manager.worker["process"]
        with pytest.raises(RuntimeError, match="reserved"):
            await runtime.load_model_async("never-opened.gguf")
        await manager.enable_worker(False)
        assert process.closed and not runtime._split_worker_reserved
        assert not manager.enabled()
        with pytest.raises(sn.SplitError):
            manager.authenticate_lease("100.64.1.2", lease["lease"])
        await manager.close()
    asyncio.run(run())


def test_plan_rejects_wrong_pin_before_network(isolated, monkeypatch, tmp_path):
    manager, _, _ = isolated
    async def run():
        monkeypatch.setattr(sm, "artifact_identity", lambda path: ("a" * 64, (0, 0, 12)))
        with pytest.raises(sn.SplitError, match="match"):
            await manager.plan({"id": "tiny", "sha256": "b" * 64, "sizeBytes": 12}, ["worker"], 1024)
        with pytest.raises(sn.SplitError, match="distinct"):
            await manager.plan({"id": "tiny"}, ["worker", "worker"], 1024)
    asyncio.run(run())


def test_split_control_requires_token_even_in_dev(isolated, monkeypatch):
    monkeypatch.delenv("POTLUCK_SIDECAR_TOKEN", raising=False)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(), client=("127.0.0.1", 1234)), base_url="http://localhost") as client:
            assert (await client.get("/split/status")).status_code == 503
    asyncio.run(run())


def test_websocket_has_its_own_auth_boundary(isolated, monkeypatch):
    # TestClient source is not a live mesh member; HTTP middleware is irrelevant.
    monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", "test")
    client = TestClient(create_app())
    with pytest.raises(Exception):
        with client.websocket_connect("/peer/split/tunnel", headers={"authorization": "Bearer guessed"}):
            pytest.fail("unauthorized WebSocket accepted")


def test_missing_or_mixed_engine_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("POTLUCK_SPLIT_ENGINE_DIR", str(tmp_path))
    with pytest.raises(sn.SplitError):
        sn.engine_bundle()
    (tmp_path / "manifest.json").write_text(json.dumps({**sn.PIN, "commit": "0" * 40}))
    with pytest.raises(sn.SplitError):
        sn.engine_bundle()


@pytest.mark.parametrize("finish,done,valid", [(True, True, True), (False, True, False), (True, False, False), (False, False, False)])
def test_native_stream_requires_finish_and_done(monkeypatch, finish, done, valid):
    content = 'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
    if finish:
        content += 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    if done:
        content += 'data: [DONE]\n\n'
    real_client = httpx.Client
    monkeypatch.setattr(sn.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=content)), **kwargs))
    model = sn.SplitModel(Process(), 12345, "secret", lambda: None)
    if valid:
        assert list(model.create_chat_completion(messages=[], stream=True))[-1]["choices"][0]["finish_reason"] == "stop"
    else:
        with pytest.raises(sn.SplitError):
            list(model.create_chat_completion(messages=[], stream=True))
        assert model.failure and model.closed


def test_explicit_cancel_is_not_relabelled_as_engine_failure():
    model = sn.SplitModel(Process(), 12345, "secret", lambda: None)
    model.cancel()
    model.failed("Socket closed after cancellation")
    assert model.closed and model.failure is None


def test_adapter_failure_sets_failed_session_state(isolated, monkeypatch):
    manager, _, _ = isolated
    async def run():
        head = {"adapter": SimpleNamespace(failure="Worker connection lost")}
        manager.head, manager.state = head, "ready"
        cleaned = []
        async def cleanup(value):
            cleaned.append(value)
        monkeypatch.setattr(manager, "_cleanup_head", cleanup)
        await manager._adapter_closed(head)
        assert manager.state == "failed" and manager.error == "Worker connection lost"
        assert cleaned == [head]
    asyncio.run(run())


def test_delayed_split_cleanup_preserves_replacement_model():
    async def run():
        runtime = LLMRuntime()
        previous, replacement = object(), object()
        runtime._model = previous
        await runtime._inference_lock.acquire()
        release = asyncio.create_task(runtime.release_split_async(previous))
        await asyncio.sleep(0)
        runtime._model = replacement
        runtime._inference_lock.release()
        await release
        assert runtime._model is replacement
    asyncio.run(run())


def test_supervisor_eof_terminates_native_child(tmp_path):
    pidfile = tmp_path / "pid"
    child = "import os,time,pathlib; pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid())); time.sleep(120)"
    supervisor = subprocess.Popen([sys.executable, "-m", "potluck_local.services.native_supervisor", sys.executable, "-c", child], stdin=subprocess.PIPE)
    try:
        end = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < end:
            time.sleep(.02)
        assert pidfile.exists()
        pid = int(pidfile.read_text())
        supervisor.stdin.close()
        supervisor.wait(timeout=6)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=6)


def test_native_allocation_counters_keep_only_device_numbers():
    assert allocation_counter("load_tensors: RPC0[127.0.0.1:54321] model buffer size = 217.23 MiB") == {
        "device": "RPC", "modelMiB": 217.23, "rpcPort": 54321}
    assert allocation_counter("load_tensors: CPU_Mapped model buffer size = 137.94 MiB") == {
        "device": "CPU", "modelMiB": 137.94}
    assert allocation_counter("load_tensors: MTL0_Mapped model buffer size = 20.5 MiB") == {
        "device": "Metal", "modelMiB": 20.5}
    for line in ["prompt: a private document", "model path: /private/model.gguf",
                 "load_tensors: RPC0[private-host:123] model buffer size = 20 MiB",
                 "load_tensors: RPC0[127.0.0.1:54321] model buffer size = 0.00 MiB"]:
        assert allocation_counter(line) is None


def test_native_cuda_memory_failure_is_actionable_without_raw_logs():
    diagnostic = "0.09 E ggml_backend_cuda_buffer_type_alloc_buffer: allocating 4379.45 MiB on device 0: cudaMalloc failed: out of memory"
    assert failure_code(diagnostic) == {"error": "cuda_out_of_memory"}
    for line in ["prompt: out of memory /private/model.gguf", "cudaMalloc failed: invalid argument",
                 "error loading /private/model.gguf"]:
        assert failure_code(line) is None
    child = "import sys,time; time.sleep(.15); print(" + repr(diagnostic) + ",file=sys.stderr); print('private prompt and path',file=sys.stderr); sys.exit(1)"
    process = sn.NativeProcess([sys.executable, "-c", child])
    try:
        process.process.wait(timeout=6)
        with pytest.raises(sn.SplitError, match="head computer ran out of GPU memory") as error:
            process.heartbeat()
        assert error.value.code == "split_gpu_memory"
        assert "private" not in str(error.value)
        assert process.placement == []
    finally:
        process.close()


def test_expired_lease_retry_gets_fresh_process_and_token(isolated):
    manager, _, _ = isolated
    async def run():
        request = {"commit": sn.PIN["commit"], "request_id": str(uuid4())}
        first = await manager.acquire("100.64.1.2", request)
        process = manager.worker["process"]
        manager.worker["expires"] = time.monotonic() - 1
        second = await manager.acquire("100.64.1.2", request)
        assert first["lease"] != second["lease"] and process.closed
        await manager.close()
    asyncio.run(run())


def test_completed_split_stays_loaded_but_cancel_stops_native_worker():
    class Model:
        managed_split = True
        closed = False
        block = False
        released = threading.Event()
        def create_chat_completion(self, **kwargs):
            yield {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]}
            if self.block:
                self.released.wait(3)
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        def cancel(self):
            self.closed = True
            self.released.set()
        def close(self):
            self.cancel()
    async def run():
        runtime = LLMRuntime()
        model = Model()
        runtime._model = model
        runtime._model_id = "tiny"
        for _ in range(2):
            chunks = [c async for c in runtime.stream_chat_raw([{"role": "user", "content": "x"}])]
            assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
            assert not model.closed
        model.block = True
        stream = runtime.stream_chat_raw([{"role": "user", "content": "x"}])
        assert (await anext(stream))["choices"][0]["delta"]["content"] == "hello"
        await asyncio.wait_for(stream.aclose(), 2)
        assert model.closed and not runtime.is_loaded()
        assert not runtime._inference_lock.locked()
    asyncio.run(run())


def test_stop_cancels_in_progress_load_and_releases_lease(isolated, monkeypatch):
    manager, _, _ = isolated
    async def run():
        cleaned = []
        head = {"id": "test"}
        manager.head = head
        manager.state = "loading"
        manager.load_task = asyncio.create_task(asyncio.sleep(100))
        async def cleanup(value):
            cleaned.append(value)
        monkeypatch.setattr(manager, "_cleanup_head", cleanup)
        await manager.stop()
        assert manager.load_task.cancelled()
        assert cleaned == [head]
        assert manager.head is None and manager.state == "idle"
    asyncio.run(run())


def test_local_control_token_and_nonmember_rejection(isolated, monkeypatch):
    monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", "test")
    async def run():
        app = create_app()
        for host, path, auth, status in [
            ("127.0.0.1", "/split/status", None, 401),
            ("127.0.0.1", "/split/status", "Bearer test", 200),
            ("100.64.1.2", "/split/status", "Bearer test", 403),
            ("192.168.1.2", "/peer/split/status", None, 403),
        ]:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 1234)), base_url="http://localhost") as client:
                response = await client.get(path, headers={"Authorization": auth} if auth else {})
                assert response.status_code == status
    asyncio.run(run())

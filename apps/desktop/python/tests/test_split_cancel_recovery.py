"""Cancellation recovery with the real adapter, HTTP transport and worker threads."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

import pytest

from potluck_local.services import split_native
from potluck_local.services.runtime import LLMRuntime
from potluck_local.services.split_native import SplitError, SplitModel


STALE_ACTION = '```action\n{"tool":"write_file","args":{"path":"stale.txt","content":"cancelled"}}\n```'


class NativeFixture:
    """An event-controlled native HTTP endpoint; no generation code is mocked."""

    def __init__(self, *, before_token: bool = False, before_headers: bool = False,
                 slot_state: str = "normal", failure: str | None = None):
        self.before_token = before_token
        self.before_headers = before_headers
        self.slot_state = slot_state
        self.failure = failure
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.closed = False
        self.close_count = 0
        self.calls: list[dict] = []
        self.active = 0
        self.maximum_active = 0
        self.guard = threading.Lock()
        self.connections: set[socket.socket] = set()
        # SplitModel consults NativeProcess.process.poll().
        self.process = self
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path != "/slots" or self.headers.get("Authorization") != "Bearer fixture-key":
                    self.send_error(404)
                    return
                if fixture.slot_state == "unresponsive":
                    fixture.release.wait(5)
                with fixture.guard:
                    processing = fixture.active > 0
                if fixture.slot_state == "idle":
                    processing = False
                value = [{"id": 0, "is_processing": processing or fixture.slot_state == "busy"}]
                if fixture.slot_state == "unknown":
                    value = {"unexpected": "no slot status"}
                body = json.dumps(value).encode()
                with suppress(BrokenPipeError, ConnectionResetError, OSError):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def do_POST(self):
                if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != "Bearer fixture-key":
                    self.send_error(404)
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                first = body["messages"][0]["content"] == "FIRST"
                with fixture.guard:
                    fixture.calls.append(body)
                    fixture.connections.add(self.connection)
                    fixture.active += 1
                    fixture.maximum_active = max(fixture.maximum_active, fixture.active)
                if first:
                    fixture.started.set()
                try:
                    if first and fixture.before_headers:
                        fixture.release.wait(5)
                    self.send_response(500 if fixture.failure == "status" else 200)
                    self.send_header("Content-Type", "text/event-stream" if body["stream"] else "application/json")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if fixture.failure == "status":
                        self.wfile.write(b'{"error":"engine failed"}')
                        self.wfile.flush()
                        return
                    if fixture.failure == "truncated":
                        if body["stream"]:
                            self.frame({"choices": [{"delta": {"content": "incomplete"}, "finish_reason": None}]})
                        else:
                            self.wfile.write(b'{"choices":[')
                            self.wfile.flush()
                        return
                    if body["stream"]:
                        if first and not fixture.before_token:
                            self.frame({"choices": [{"delta": {"content": "prefix"}, "finish_reason": None}]})
                        if first:
                            fixture.release.wait(5)
                        self.frame({"choices": [{"delta": {"content": STALE_ACTION if first else "NEXT_OK"}, "finish_reason": None}]})
                        self.frame({"choices": [{"delta": {}, "finish_reason": "stop"}]})
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    else:
                        if first:
                            fixture.release.wait(5)
                        self.wfile.write(json.dumps({"choices": [{"message": {"role": "assistant", "content": STALE_ACTION if first else "NEXT_OK"}, "finish_reason": "stop"}]}).encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    self.close_connection = True
                    with fixture.guard:
                        fixture.connections.discard(self.connection)
                        fixture.active -= 1
                    if first:
                        fixture.finished.set()

            def frame(self, value):
                self.wfile.write(("data: " + json.dumps(value) + "\n\n").encode())
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def poll(self):
        return 0 if self.closed else None

    def heartbeat(self):
        if self.closed:
            raise SplitError("Fixture process stopped")

    def close(self):
        self.closed = True
        self.close_count += 1
        self.release.set()
        with self.guard:
            connections = list(self.connections)
        for connection in connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)

    def shutdown(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def native(monkeypatch, tmp_path):
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    fixtures: list[NativeFixture] = []

    def create(**kwargs):
        fixture = NativeFixture(**kwargs)
        fixtures.append(fixture)
        runtime = LLMRuntime()
        closed: list[bool] = []
        model = SplitModel(fixture, fixture.server.server_port, "fixture-key", lambda: closed.append(True))
        runtime._model = model
        runtime._model_id = "fixture-split"
        return fixture, runtime, model, closed

    yield create
    for fixture in fixtures:
        fixture.shutdown()


async def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "fixture did not reach the expected state"
        await asyncio.sleep(0.005)


async def collect(runtime, mode, prompt, output):
    messages = [{"role": "user", "content": prompt}]
    if mode == "chat":
        output.append(await runtime.chat(messages))
        return
    async with aclosing(getattr(runtime, mode)(messages)) as stream:
        async for item in stream:
            output.append(item)


@pytest.mark.parametrize("mode", ["stream_chat", "stream_chat_raw", "chat"])
@pytest.mark.parametrize("before_token", [False, True])
def test_cancelled_request_preserves_healthy_split_and_next_request(native, mode, before_token):
    fixture, runtime, model, closed = native(before_token=before_token)

    async def run():
        output = []
        task = asyncio.create_task(collect(runtime, mode, "FIRST", output))
        try:
            await eventually(fixture.started.is_set)
            if mode != "chat" and not before_token:
                await eventually(lambda: bool(output))
            task.cancel()
            asyncio.get_running_loop().call_later(0.05, fixture.release.set)
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 3)
            assert not model.closed, "ordinary request cancellation stopped the loaded split"
            assert model.failure is None
            assert not fixture.closed and closed == []
            assert not runtime._inference_lock.locked()
            assert "stale.txt" not in json.dumps(output)
            assert "[DONE]" not in json.dumps(output)
            assert '"finish_reason": "stop"' not in json.dumps(output)
            next_output = []
            await asyncio.wait_for(collect(runtime, mode, "SECOND", next_output), 3)
            assert "NEXT_OK" in json.dumps(next_output)
            assert "stale.txt" not in json.dumps(next_output)
            assert fixture.maximum_active == 1
            assert len(fixture.calls) == 2
        finally:
            fixture.release.set()
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 3)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["stream_chat", "stream_chat_raw", "chat"])
def test_cancel_before_headers_delivers_no_output_and_only_reuses_if_confirmed(native, mode):
    fixture, runtime, model, closed = native(before_headers=True)

    async def run():
        output = []
        task = asyncio.create_task(collect(runtime, mode, "FIRST", output))
        try:
            await eventually(fixture.started.is_set)
            task.cancel()
            asyncio.get_running_loop().call_later(0.05, fixture.release.set)
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 3)
            assert output == []
            assert not runtime._inference_lock.locked()
            if model.closed:
                # An idle slot alone cannot prove a sent pre-header request was admitted.
                assert fixture.closed and closed == [True]
            else:
                assert closed == []
                assert fixture.finished.is_set()
                next_output = []
                await asyncio.wait_for(collect(runtime, mode, "SECOND", next_output), 3)
                assert "NEXT_OK" in json.dumps(next_output)
        finally:
            fixture.release.set()
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 3)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["stream_chat", "stream_chat_raw", "chat"])
def test_repeated_cancellation_holds_lock_until_native_idle_then_runs_queued_request(native, mode):
    fixture, runtime, model, closed = native(before_token=True)

    async def run():
        output, queued_output = [], []
        task = asyncio.create_task(collect(runtime, mode, "FIRST", output))
        queued = None
        try:
            await eventually(fixture.started.is_set)
            queued = asyncio.create_task(collect(runtime, mode, "SECOND", queued_output))
            await eventually(lambda: runtime._inference_lock.waiting > 0)
            for _ in range(5):
                task.cancel()
                await asyncio.sleep(0.02)
                assert runtime._inference_lock.locked()
                assert len(fixture.calls) == 1
                assert not queued.done()
                assert not fixture.closed
            fixture.release.set()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 3)
            await asyncio.wait_for(queued, 3)
            assert not model.closed and closed == []
            assert output == []
            assert "NEXT_OK" in json.dumps(queued_output)
            assert "stale.txt" not in json.dumps(queued_output)
            assert fixture.maximum_active == 1
            assert len(fixture.calls) == 2
        finally:
            fixture.release.set()
            for pending in [task, queued]:
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await asyncio.wait_for(asyncio.shield(pending), 3)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["stream_chat", "stream_chat_raw", "chat"])
def test_cancel_before_executor_start_sends_no_native_request(native, mode):
    fixture, runtime, model, closed = native()

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        occupied, release_executor = threading.Event(), threading.Event()

        def occupy():
            occupied.set()
            release_executor.wait(5)

        blocker = loop.run_in_executor(None, occupy)
        await eventually(occupied.is_set)
        output = []
        task = asyncio.create_task(collect(runtime, mode, "FIRST", output))
        try:
            await eventually(runtime._inference_lock.locked)
            task.cancel()
            await asyncio.sleep(0.025)
            release_executor.set()
            await blocker
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 3)
            assert fixture.calls == []
            assert output == []
            assert not model.closed and closed == []
            next_output = []
            await asyncio.wait_for(collect(runtime, mode, "SECOND", next_output), 3)
            assert "NEXT_OK" in json.dumps(next_output)
        finally:
            release_executor.set()
            fixture.release.set()
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 3)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["stream_chat", "stream_chat_raw", "chat"])
@pytest.mark.parametrize("failure", ["status", "truncated"])
def test_real_native_failures_still_close_split(native, mode, failure):
    fixture, runtime, model, closed = native(failure=failure)

    async def run():
        output = []
        if mode == "chat":
            with pytest.raises(SplitError):
                await asyncio.wait_for(collect(runtime, mode, "FIRST", output), 3)
        else:
            await asyncio.wait_for(collect(runtime, mode, "FIRST", output), 3)
            assert "error" in json.dumps(output).lower()
            assert '"finish_reason": "stop"' not in json.dumps(output)
        assert model.closed and model.failure
        assert fixture.closed and closed == [True]
        assert not runtime._inference_lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["stream_chat", "stream_chat_raw"])
def test_closing_stream_after_one_token_preserves_model(native, mode):
    fixture, runtime, model, closed = native()

    async def run():
        stream = getattr(runtime, mode)([{"role": "user", "content": "FIRST"}])
        try:
            first = await asyncio.wait_for(anext(stream), 3)
            assert "prefix" in json.dumps(first)
            asyncio.get_running_loop().call_later(0.05, fixture.release.set)
            await asyncio.wait_for(stream.aclose(), 3)
            assert not model.closed and closed == []
            output = []
            await asyncio.wait_for(collect(runtime, mode, "SECOND", output), 3)
            assert "NEXT_OK" in json.dumps(output)
            assert fixture.maximum_active == 1
        finally:
            fixture.release.set()
            await stream.aclose()

    asyncio.run(run())


def test_late_cancel_for_completed_request_cannot_interrupt_new_request(native):
    fixture, _runtime, model, closed = native()
    previous, current = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(model.create_chat_completion,
                                messages=[{"role": "user", "content": "SECOND"}],
                                stream=False, cancel_event=previous)
        assert first.result(timeout=3)["choices"][0]["finish_reason"] == "stop"
        pending = executor.submit(model.create_chat_completion,
                                  messages=[{"role": "user", "content": "FIRST"}],
                                  stream=False, cancel_event=current)
        try:
            assert fixture.started.wait(2)
            previous.set()
            model.cancel(previous)
            assert not current.is_set()
            assert not model.closed and closed == []
            assert not pending.done()
            fixture.release.set()
            assert pending.result(timeout=3)["choices"][0]["finish_reason"] == "stop"
            assert fixture.maximum_active == 1
        finally:
            fixture.release.set()


@pytest.mark.parametrize("mode", ["stream_chat", "stream_chat_raw", "chat"])
@pytest.mark.parametrize("slot_state", ["busy", "unknown", "unresponsive"])
def test_unresponsive_or_unverifiable_native_slot_forces_close(native, monkeypatch, mode, slot_state):
    monkeypatch.setattr(split_native, "CANCEL_GRACE_SECONDS", 0.2)
    fixture, runtime, model, closed = native(before_token=True, slot_state=slot_state)

    async def run():
        output = []
        task = asyncio.create_task(collect(runtime, mode, "FIRST", output))
        try:
            await eventually(fixture.started.is_set)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 2)
            assert model.closed and fixture.closed
            assert closed == [True]
            assert not runtime.is_loaded()
            assert not runtime._inference_lock.locked()
            assert output == []
            assert len(fixture.calls) == 1
        finally:
            fixture.release.set()
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 3)

    asyncio.run(run())


def test_idle_slot_without_pre_header_admission_proof_cannot_preserve_split(native, monkeypatch):
    monkeypatch.setattr(split_native, "CANCEL_GRACE_SECONDS", 0.2)
    fixture, runtime, model, closed = native(before_headers=True, slot_state="idle")

    async def run():
        output = []
        task = asyncio.create_task(collect(runtime, "stream_chat_raw", "FIRST", output))
        try:
            await eventually(fixture.started.is_set)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 2)
            assert model.closed and fixture.closed and closed == [True]
            assert output == []
            assert not runtime._inference_lock.locked()
        finally:
            fixture.release.set()
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 3)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["stream_chat", "stream_chat_raw", "chat"])
def test_explicit_model_stop_finishes_waiting_request_and_releases_lock(native, mode):
    fixture, runtime, model, closed = native(before_token=True)

    async def run():
        output = []
        task = asyncio.create_task(collect(runtime, mode, "FIRST", output))
        try:
            await eventually(fixture.started.is_set)
            await asyncio.to_thread(model.close)
            if mode == "chat":
                with pytest.raises(SplitError):
                    await asyncio.wait_for(asyncio.shield(task), 2)
            else:
                await asyncio.wait_for(asyncio.shield(task), 2)
                assert "error" in json.dumps(output).lower()
            assert model.closed and closed == [True]
            assert not runtime._inference_lock.locked()
            assert "stale.txt" not in json.dumps(output)
        finally:
            fixture.release.set()
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 3)

    asyncio.run(run())

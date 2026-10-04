"""Quiet-stream liveness races and real gateway retry policy."""

import asyncio
import json

import httpx
import pytest

from potluck_local.services import gateway_routing as routing
from potluck_local.services import peer_liveness as liveness
from potluck_local.services.remote_stream import RemoteStreamError

TOKEN = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
DONE = b'data: [DONE]\n\n'
PEER = {"node_name": "first", "mesh_ip": "100.64.0.2"}
OTHER = {"node_name": "replacement", "mesh_ip": "100.64.0.3"}
SHA = "a" * 64


class Wire(httpx.AsyncByteStream):
    def __init__(self, first=b"", delayed=b"", delay=0):
        self.first, self.delayed, self.delay = first, delayed, delay
        self.closed = False

    async def __aiter__(self):
        if self.first:
            yield self.first
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.delayed:
            yield self.delayed

    async def aclose(self):
        self.closed = True


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr(liveness, "QUIET_BEFORE_PROBE_S", 0.01)
    monkeypatch.setattr(liveness, "PROBE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(liveness, "FAILED_PROBE_INTERVAL_S", 0.005)
    monkeypatch.setattr(routing, "FIRST_TOKEN_TIMEOUT_S", 0.4)
    monkeypatch.setattr(routing, "IDLE_TIMEOUT_S", 0.4)
    monkeypatch.setattr(routing, "REQUEST_TIMEOUT_S", 1)
    calls, probes, wires = [], [], []
    real_client = httpx.AsyncClient

    async def roster():
        return [PEER, OTHER]

    monkeypatch.setattr(routing, "household_peers", roster)

    def install(first, health, *, headers_delay=0):
        async def handler(req):
            if req.url.path == "/health":
                probes.append(req.url.host)
                return await health(req, len(probes))
            if req.url.path == "/peer/capabilities":
                return httpx.Response(200, json={"loaded_model": "pinned", "available_for_inference": True,
                                                "model_lock_version": 1, "loaded_model_detail": {"sha256": SHA}})
            calls.append((req.url.host, json.loads(req.content)))
            if req.url.host == PEER["mesh_ip"] and headers_delay:
                await asyncio.sleep(headers_delay)
            wire = first if req.url.host == PEER["mesh_ip"] else Wire(TOKEN + DONE)
            wires.append(wire)
            return httpx.Response(200, stream=wire,
                                  headers={"x-potluck-model-lock": "1", "x-potluck-model-sha256": SHA})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    return install, calls, probes, wires


async def collect():
    return [chunk async for chunk in routing.stream_peer_openai(
        PEER, [{"role": "user", "content": "hello"}], "pinned", 0.2, 32, sha256=SHA)]


async def dead(req, count):
    raise httpx.ConnectError("unreachable")


async def alive(req, count):
    return httpx.Response(200)


@pytest.mark.parametrize("headers_delay", [0, 60])
def test_unreachable_before_output_retries_with_same_pin(harness, headers_delay):
    install, calls, probes, wires = harness
    install(Wire(delay=60), dead, headers_delay=headers_delay)
    chunks = asyncio.run(collect())
    assert [host for host, _ in calls] == [PEER["mesh_ip"], OTHER["mesh_ip"]]
    assert calls[0][1] == calls[1][1]
    assert calls[1][1]["potluck_model_sha256"] == SHA
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert len(probes) == 2
    assert all(w.closed for w in wires)


def test_unreachable_after_output_fails_without_replay(harness):
    install, calls, probes, wires = harness
    install(Wire(TOKEN, delay=60), dead)
    async def run():
        stream = routing.stream_peer_openai(PEER, [], "pinned", 0.2, 32, sha256=SHA)
        assert (await anext(stream))["choices"][0]["delta"]["content"] == "hello"
        with pytest.raises(RemoteStreamError) as error:
            await anext(stream)
        assert error.value.category == "peer_unreachable"
    asyncio.run(run())
    assert len(calls) == 1 and len(probes) == 2
    assert all(w.closed for w in wires)


@pytest.mark.parametrize("first", [b"", TOKEN])
@pytest.mark.parametrize("status", [200, 404, 503])
def test_slow_reachable_worker_keeps_its_progress_budget(harness, first, status):
    install, calls, probes, wires = harness
    async def response(req, count):
        return httpx.Response(status)
    install(Wire(first, TOKEN + DONE, delay=0.07), response)
    chunks = asyncio.run(collect())
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert len(calls) == 1 and len(probes) >= 2
    assert all(w.closed for w in wires)


def test_one_failed_probe_does_not_interrupt_a_healthy_stream(harness):
    install, calls, probes, wires = harness
    async def flaky(req, count):
        return await (dead(req, count) if count % 2 else alive(req, count))
    install(Wire(TOKEN, DONE, delay=0.08), flaky)
    asyncio.run(collect())
    assert len(calls) == 1 and len(probes) >= 3
    assert all(w.closed for w in wires)


def test_output_arriving_during_probe_wins_and_cancels_probe(harness, monkeypatch):
    install, calls, probes, wires = harness
    monkeypatch.setattr(liveness, "PROBE_TIMEOUT_S", 1)
    cancelled = []
    async def hanging(req, count):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)
    install(Wire(delayed=TOKEN + DONE, delay=0.025), hanging)
    asyncio.run(collect())
    assert len(calls) == 1 and probes and cancelled
    assert all(w.closed for w in wires)


def test_healthy_probe_does_not_extend_idle_budget(harness, monkeypatch):
    install, calls, probes, wires = harness
    monkeypatch.setattr(routing, "IDLE_TIMEOUT_S", 0.06)
    install(Wire(TOKEN, delay=60), alive)
    with pytest.raises(RemoteStreamError) as error:
        asyncio.run(collect())
    assert error.value.category == "stream_timeout"
    assert len(calls) == 1 and len(probes) >= 2
    assert all(w.closed for w in wires)


def test_cancel_while_probing_closes_both_requests_and_never_retries(harness, monkeypatch):
    install, calls, probes, wires = harness
    monkeypatch.setattr(liveness, "PROBE_TIMEOUT_S", 1)
    async def run():
        started = asyncio.Event()
        ended = asyncio.Event()
        async def hanging(req, count):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                ended.set()
        install(Wire(TOKEN, delay=60), hanging)
        task = asyncio.create_task(collect())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ended.is_set()
    asyncio.run(run())
    assert len(calls) == 1 and len(probes) == 1
    assert all(w.closed for w in wires)


def test_consumer_close_cleans_up_stream_without_probe_or_retry(harness):
    install, calls, probes, wires = harness
    install(Wire(TOKEN, delay=60), alive)
    async def run():
        stream = routing.stream_peer_openai(PEER, [], "pinned", 0.2, 32, sha256=SHA)
        await anext(stream)
        await stream.aclose()
    asyncio.run(run())
    assert len(calls) == 1 and not probes
    assert all(w.closed for w in wires)


def test_probe_hang_has_an_absolute_timeout(harness):
    install, calls, probes, wires = harness
    async def hanging(req, count):
        await asyncio.sleep(60)
    install(Wire(TOKEN, delay=60), hanging)
    with pytest.raises(RemoteStreamError) as error:
        asyncio.run(collect())
    assert error.value.category == "peer_unreachable"
    assert len(calls) == 1 and len(probes) == 2
    assert all(w.closed for w in wires)


def test_fast_stream_needs_no_health_probes(harness):
    install, calls, probes, wires = harness
    install(Wire(TOKEN + DONE), dead)
    asyncio.run(collect())
    assert len(calls) == 1 and not probes
    assert all(w.closed for w in wires)

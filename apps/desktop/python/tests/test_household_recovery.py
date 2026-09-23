"""Fault injection for the real HTTP household retry path; no models or mesh."""
import asyncio
import json

import httpx
import pytest

from potluck_local.services import gateway_routing as routing
from potluck_local.services.remote_stream import RemoteStreamError

TOKEN = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
DONE = b'data: [DONE]\n\n'
ROLE = b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
PEERS = [{'node_name': f'p{i}', 'mesh_ip': f'100.64.0.{i}'} for i in range(2, 6)]


class Wire(httpx.AsyncByteStream):
    def __init__(self, content=b'', stall=False, comments=False):
        self.content, self.stall, self.comments = content, stall, comments
        self.closed = False

    async def __aiter__(self):
        if self.content:
            yield self.content
        if self.stall:
            while True:
                await asyncio.sleep(0.005)
                if self.comments:
                    yield b': ping\n\n'

    async def aclose(self):
        self.closed = True


@pytest.fixture
def harness(monkeypatch):
    real_client = httpx.AsyncClient
    calls, wires = [], []
    async def roster():
        return PEERS
    monkeypatch.setattr(routing, 'household_peers', roster)
    monkeypatch.setattr(routing, 'FIRST_TOKEN_TIMEOUT_S', 0.025)
    monkeypatch.setattr(routing, 'IDLE_TIMEOUT_S', 0.025)
    monkeypatch.setattr(routing, 'REQUEST_TIMEOUT_S', 0.2)
    def install(responses, models=None):
        def handler(req):
            if req.url.path == '/peer/capabilities':
                return httpx.Response(200, json={'loaded_model': (models or {}).get(req.url.host, 'test'),
                                                'available_for_inference': True})
            calls.append((req.url.host, json.loads(req.content)))
            value = responses[req.url.host]
            if isinstance(value, Exception):
                raise value
            if isinstance(value, int):
                return httpx.Response(value)
            wires.append(value)
            return httpx.Response(200, stream=value)
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real_client(
            transport=httpx.MockTransport(handler), **kw))
    return install, calls, wires


async def collect():
    return [c async for c in routing.stream_peer_openai(PEERS[0], [{'role': 'user', 'content': 'hello'}], 'test', 0.2, 42)]


@pytest.mark.parametrize('failure', [httpx.ConnectError('offline'), 503, Wire(), Wire(ROLE), Wire(stall=True), Wire(ROLE, stall=True, comments=True)])
def test_failure_before_output_tries_another_same_model_machine(failure, harness):
    install, calls, wires = harness
    install({'100.64.0.2': failure, '100.64.0.3': Wire(TOKEN + DONE)})
    chunks = asyncio.run(collect())
    assert [host for host, _ in calls] == ['100.64.0.2', '100.64.0.3']
    assert calls[0][1] == calls[1][1]  # unchanged input/model/parameters
    assert [c['choices'][0]['delta'].get('content') for c in chunks] == ['hi', None]
    assert all(w.closed for w in wires)


@pytest.mark.parametrize('failure', [Wire(TOKEN), Wire(TOKEN, stall=True), Wire(TOKEN + b'data: {"error":"gone"}\n\n')])
def test_failure_after_output_never_retries(failure, harness):
    install, calls, wires = harness
    install({'100.64.0.2': failure})
    async def run():
        chunks = []
        with pytest.raises(RemoteStreamError):
            async for c in routing.stream_peer_openai(PEERS[0], [], 'test', 0.2, 42):
                chunks.append(c)
        assert chunks[0]['choices'][0]['delta']['content'] == 'hi'
        assert len(chunks) == 1
    asyncio.run(run())
    assert len(calls) == 1
    assert all(w.closed for w in wires)


def test_model_mismatch_is_skipped_and_scope_never_expands(harness):
    install, calls, _ = harness
    install({'100.64.0.2': 503, '100.64.0.4': Wire(TOKEN + DONE)}, {'100.64.0.3': 'different'})
    asyncio.run(collect())
    assert [host for host, _ in calls] == ['100.64.0.2', '100.64.0.4']


def test_cancel_closes_attempt_without_retry(harness):
    install, calls, wires = harness
    install({'100.64.0.2': Wire(stall=True)})
    async def run():
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert len(calls) == 1
    assert all(w.closed for w in wires)


def test_attempt_and_overall_limits(harness, monkeypatch):
    install, calls, wires = harness
    install({p['mesh_ip']: Wire(stall=True, comments=True) for p in PEERS})
    monkeypatch.setattr(routing, 'REQUEST_TIMEOUT_S', 0.04)
    with pytest.raises(RemoteStreamError):
        asyncio.run(collect())
    assert len(calls) == 2  # total budget, rather than a fresh budget per attempt
    assert all(w.closed for w in wires)


def test_nonretryable_request_error(harness):
    install, calls, _ = harness
    install({'100.64.0.2': 422})
    with pytest.raises(routing.PeerDispatchError):
        asyncio.run(collect())
    assert len(calls) == 1


def test_no_spare_does_not_repeat_failed_worker(harness, monkeypatch):
    install, calls, _ = harness
    install({'100.64.0.2': 503})
    async def roster():
        return [PEERS[0]]
    monkeypatch.setattr(routing, 'household_peers', roster)
    with pytest.raises(RemoteStreamError):
        asyncio.run(collect())
    assert len(calls) == 1

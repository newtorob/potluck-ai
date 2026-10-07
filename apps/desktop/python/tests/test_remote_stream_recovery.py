"""Exercise remote wire contracts through the actual direct/pool adapters."""
import asyncio
import json

import httpx
import pytest

from potluck_local.services import gateway_routing as direct, pool_routing as pool
from potluck_local.services.remote_stream import RemoteStreamError


def event(obj):
    return f'data: {json.dumps(obj)}\n\n'


TOKEN = event({'choices': [{'delta': {'content': 'partial'}}]})
DONE = 'data: [DONE]\n\n'
META = event({'meta': {'finish_reason': 'length', 'output_tokens': 7,
                       'duration_ms': 400, 'served_scope': 'circle', 'peer_id': 'never-forward'}})


@pytest.fixture
def transport(monkeypatch):
    real_client = httpx.AsyncClient
    def install(body):
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real_client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body)), **kw))
        monkeypatch.setattr(pool, 'coordinator_http_base', lambda: 'https://coordinator.example')
        monkeypatch.setattr(pool, '_headers', lambda base=None: {})
        monkeypatch.setattr(pool, '_pool_multiturn_enabled', lambda: False)
    return install


def source(route):
    messages = [{'role': 'user', 'content': 'hello'}]
    if route == 'direct':
        return direct.stream_peer_openai({'mesh_ip': '100.64.0.2'}, messages, 'test', 0.7, 20)
    return pool.stream_pool_openai(messages, 'test', 0.7, 20)


@pytest.mark.parametrize('route', ['direct', 'pool'])
@pytest.mark.parametrize('tail', ['', 'data: {broken}\n\n', event({'error': 'worker dropped'}) + DONE])
def test_incomplete_stream_never_emits_success(route, tail, transport):
    transport(TOKEN + tail)
    async def run():
        chunks = []
        with pytest.raises(RemoteStreamError):
            async for chunk in source(route):
                chunks.append(chunk)
        assert ''.join(c['choices'][0]['delta'].get('content', '') for c in chunks) == 'partial'
        assert all(c['choices'][0]['finish_reason'] is None for c in chunks)
    asyncio.run(run())


@pytest.mark.parametrize('body', [TOKEN + DONE, TOKEN + META, META + event({'error': {'category': 'peer_disconnected', 'message': 'gone'}}) + DONE])
def test_pool_requires_summary_and_done_and_no_error(body, transport):
    transport(body)
    async def run():
        with pytest.raises(RemoteStreamError):
            await pool.complete_pool_openai([], 'test', 0.7, 20)
    asyncio.run(run())


def test_pool_preserves_finish_usage_and_aggregate_metadata(transport):
    transport(TOKEN + META + DONE)
    result = asyncio.run(pool.complete_pool_openai([], 'test', 0.7, 20))
    assert result['choices'][0]['message']['content'] == 'partial'
    assert result['choices'][0]['finish_reason'] == 'length'
    assert result['usage']['completion_tokens'] == 7
    assert result['potluck'] == {'finish_reason': 'length', 'output_tokens': 7,
                                 'duration_ms': 400, 'served_scope': 'circle'}


@pytest.mark.parametrize('content', ['', TOKEN])
def test_legacy_direct_done_is_explicit_success(content, transport):
    transport(content + DONE)
    result = asyncio.run(direct.complete_peer_openai({'mesh_ip': '100.64.0.2'}, [], 'test', 0.7, 20))
    assert result['choices'][0]['finish_reason'] == 'stop'
    assert result['choices'][0]['message']['content'] == ('partial' if content else '')


def test_direct_keeps_length_and_usage_and_parses_multiline_crlf(transport):
    body = ': heartbeat\r\n\r\ndata: {"choices":\r\ndata: [{"delta":{"content":"hi"}}]}\r\n\r\n'
    body += event({'choices': [{'delta': {}, 'finish_reason': 'length'}],
                   'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}})
    transport(body + 'data: [DONE]')
    result = asyncio.run(direct.complete_peer_openai({'mesh_ip': '100.64.0.2'}, [], 'test', 0.7, 20))
    assert result['choices'][0]['message']['content'] == 'hi'
    assert result['choices'][0]['finish_reason'] == 'length'
    assert result['usage']['total_tokens'] == 3


def test_finish_chunk_without_done_is_not_success(transport):
    transport(TOKEN + event({'choices': [{'delta': {}, 'finish_reason': 'stop'}]}))
    async def run():
        with pytest.raises(RemoteStreamError):
            await direct.complete_peer_openai({'mesh_ip': '100.64.0.2'}, [], 'test', 0.7, 20)
    asyncio.run(run())


@pytest.mark.parametrize('path', ['/v1/chat/completions', '/v1/completions', '/api/chat', '/api/generate'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('success', [False, True])
def test_public_gateway_terminal_contract(path, stream, success, transport, monkeypatch):
    from fastapi import FastAPI
    from potluck_local.routers import gateway, ollama
    from unittest.mock import AsyncMock, Mock
    real_client = httpx.AsyncClient
    terminal = event({'choices': [{'delta': {}, 'finish_reason': 'length'}]}) + DONE
    transport(TOKEN + (terminal if success else ''))
    monkeypatch.setattr(gateway, 'get_runtime', lambda: Mock(is_loaded=lambda: False))
    monkeypatch.setattr(ollama, 'get_runtime', lambda: Mock(is_loaded=lambda: False))
    monkeypatch.setattr(direct, 'peer_for_model', AsyncMock(return_value={'node_name': 'p1', 'mesh_ip': '100.64.0.2'}))
    monkeypatch.setattr(gateway, '_record_consumed', AsyncMock())
    app = FastAPI()
    app.include_router(gateway.router, prefix='/v1')
    app.include_router(ollama.router, prefix='/api')
    async def run():
        async with real_client(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            return await client.post(path, json={'model': 'test', 'stream': stream,
                                                'messages': [{'role': 'user', 'content': 'hello'}], 'prompt': 'hello'})
    response = asyncio.run(run())
    if success:
        assert response.status_code == 200
        field = 'done_reason' if path.startswith('/api/') else 'finish_reason'
        assert f'\"{field}\": \"length\"' in response.text or f'\"{field}\":\"length\"' in response.text
    elif stream:
        assert response.status_code == 200
        assert 'partial' in response.text
        assert '"error"' in response.text
        assert '"finish_reason": "stop"' not in response.text
        assert '"done": true' not in response.text
        assert response.headers['x-potluck-served-by'] == 'household'
    else:
        assert response.status_code == 502
        assert response.json()['detail']['code'] == 'stream_interrupted'

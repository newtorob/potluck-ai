"""Rendered counts follow native validation and never invoke inference."""
import asyncio
import threading
import pytest
from potluck_local.services import gateway_routing
from test_reasoning_controls import native
from test_gateway_reasoning_tools import USER, client, serve, FakeRuntime
from test_split_native_guard import INJECTED, TOOLS

@pytest.mark.parametrize('tools', [None, TOOLS])
def test_native_count_matches_guarded_render(client, monkeypatch, tools):
    runtime, engine = native(monkeypatch)
    runtime._loaded_n_ctx = 8192
    serve(monkeypatch, runtime)
    body = {'model': 'local-model', 'messages': [USER], 'max_tokens': 2048,
            'reasoning_budget_tokens': 256}
    if tools:
        body.update(tools=tools, parallel_tool_calls=False)
        from potluck_local.services.model_traits import TemplateTraits
        runtime._model.template_traits = TemplateTraits(supports_tools=True, thinks=True)
    response = client.post('/v1/potluck/count-tokens', json=body)
    assert response.status_code == 200, response.text
    count = response.json()
    assert count['context_window'] == 8192 and count['prompt_tokens'] > 0
    assert all(r[0] not in ('/v1/completions', '/v1/chat/completions', '/slots') for r in engine.requests)
    if tools:
        assert engine.body('/apply-template')['parallel_tool_calls'] is False
        assert engine.body('/apply-template')['tools'] == TOOLS
    assert client.get('/v1/potluck/capabilities').json()['prompt_count_version'] == 1


def test_protected_text_count_uses_guard_and_closes_ticket(client, monkeypatch):
    runtime, engine = native(monkeypatch)
    runtime._loaded_n_ctx = 8192
    serve(monkeypatch, runtime)
    response = client.post('/v1/potluck/count-tokens', json={'model': 'local-model', 'messages': [{'role': 'user', 'content': INJECTED}]})
    assert response.status_code == 200, response.text
    assert response.json()['prompt_tokens'] > 0
    assert any(r[0] == '/tokenize' for r in engine.requests)
    assert all(r[0] != '/v1/completions' for r in engine.requests)
    response = client.post('/v1/potluck/count-tokens', json={'model': 'local-model', 'messages': [{'role': 'user', 'content': INJECTED}], 'max_tokens': 1024, 'reasoning_budget_tokens': 256})
    assert response.status_code == 400
    assert not runtime._model.closed


def test_unavailable_backend_and_remote_route_return_no_estimate(client, monkeypatch):
    serve(monkeypatch, FakeRuntime())
    response = client.post('/v1/potluck/count-tokens', json={'messages': [USER]})
    assert response.status_code == 200 and response.json()['prompt_tokens'] is None
    async def find(model): return {'mesh_ip': '100.64.0.2', '_capabilities': {}}, None
    monkeypatch.setattr(gateway_routing, 'find_peer', find)
    response = client.post('/v1/potluck/count-tokens', headers={'x-potluck-scope': 'mine'}, json={'model': 'remote', 'messages': [USER]})
    assert response.status_code == 200 and response.json() == {'model': 'remote', 'scope': 'mine', 'prompt_tokens': None, 'context_window': None}

@pytest.mark.parametrize('scope', ['pool', 'circle', 'bogus'])
def test_count_refuses_non_owned_scope(client, monkeypatch, scope):
    runtime, engine = native(monkeypatch)
    serve(monkeypatch, runtime)
    assert client.post('/v1/potluck/count-tokens', headers={'x-potluck-scope': scope}, json={'messages': [USER]}).status_code == 422
    assert not engine.requests


def test_count_rechecks_identity_under_lifecycle_gate(monkeypatch):
    runtime, engine = native(monkeypatch)
    async def run():
        with pytest.raises(Exception, match='model'):
            await runtime.count_chat_tokens([USER], expected_model='different-model')
    asyncio.run(run())
    assert not engine.requests


def test_cancelled_count_drains_worker_before_releasing_lifecycle_gate(monkeypatch):
    runtime, _ = native(monkeypatch)
    started, release = threading.Event(), threading.Event()
    def count(**kwargs):
        started.set()
        assert release.wait(5)
        return 12
    monkeypatch.setattr(runtime._model, 'count_chat_tokens', count)
    async def run():
        task = asyncio.create_task(runtime.count_chat_tokens([USER]))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert runtime._admission_lock.locked()
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runtime._admission_lock.locked()
    asyncio.run(run())

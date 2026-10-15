"""Native household finishes stay native; text-only engines stay honest."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from potluck_local.routers import peer
from potluck_local.services.remote_stream import openai_chunks
from potluck_local.services.runtime import LLMRuntime

MODEL_PATH = '/Users/private-member/.potluck/models/private/model.gguf'
TOOL = {'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}
TEMPLATE = '''{%- if tools %}{%- for tool in tools %}{{ tool | tojson }}{%- endfor %}{%- endif %}
{%- for message in messages %}{{ message.role }}: {{ message.content }}{%- endfor %}'''
USER = {'role': 'user', 'content': 'a private request'}


def client(runtime, monkeypatch):
    monkeypatch.setattr(peer, 'get_runtime', lambda: runtime)
    monkeypatch.setattr(peer, 'get_settings', lambda: SimpleNamespace(network=SimpleNamespace(contributor_mode=True)))
    monkeypatch.setattr(peer, '_record_served', AsyncMock())
    app = FastAPI()
    app.include_router(peer.router, prefix='/peer')
    return TestClient(app)


def normalized(response):
    async def run():
        return [chunk async for chunk in openai_chunks(httpx.Response(200, content=response.content), 'test')]
    return asyncio.run(run())


@pytest.mark.parametrize('reason', ['stop', 'length'])
@pytest.mark.parametrize('with_tools', [False, True])
def test_native_peer_finish_and_tools_survive_actual_runtime_and_route(reason, with_tools, monkeypatch):
    class Model:
        metadata = {'tokenizer.chat_template': TEMPLATE}
        calls = []

        def create_chat_completion(self, **kwargs):
            self.calls.append(kwargs)
            return iter([
                {'model': MODEL_PATH, 'choices': [{'index': 0, 'delta': {'content': 'partial'}, 'finish_reason': None}]},
                {'model': MODEL_PATH, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}],
                 'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}},
            ])

    model = Model()
    runtime = LLMRuntime()
    runtime._model, runtime._model_id = model, 'test'
    runtime._load_report = {'offloaded_layers': 0, 'used_n_gpu_layers': 0}
    response = client(runtime, monkeypatch).post('/peer/chat/completions', json={
        'model': 'test', 'messages': [USER], 'stream': True, 'stop': ['END'],
        **({'tools': [TOOL]} if with_tools else {}),
    })
    assert response.status_code == 200
    assert 'x-potluck-batch-protocol' not in response.headers
    assert MODEL_PATH not in response.text and '/Users/' not in response.text
    wire = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]
    assert all(chunk['model'] == 'test' for chunk in wire)
    terminal = normalized(response)[-1]
    assert terminal['choices'][0]['finish_reason'] == reason
    assert terminal['usage']['completion_tokens'] == 3
    assert 'potluck' not in terminal
    assert model.calls[0]['messages'] == [USER]
    assert model.calls[0]['stop'] == ['END']
    assert model.calls[0].get('tools') == ([TOOL] if with_tools else None)


@pytest.mark.parametrize('pipeline,batching', [(True, False), (False, True), (True, True)])
def test_legacy_engine_keeps_ordinary_chat_with_inferred_finish(pipeline, batching, monkeypatch):
    class Runtime:
        def is_loaded(self): return True
        def current_model_id(self): return 'test'
        def loaded_pipeline(self): return {'active': True} if pipeline else None
        def continuous_batch_active(self): return batching

        async def stream_chat(self, **kwargs):
            yield 'data: {"choices":[{"delta":{"content":"legacy chat"}}]}\n\n'
            yield 'data: [DONE]\n\n'

        def stream_chat_raw(self, *args, **kwargs):
            raise AssertionError('legacy runtime does not support native finish data')

    response = client(Runtime(), monkeypatch).post('/peer/chat/completions', json={
        'model': 'test', 'messages': [USER], 'stream': True,
    })
    assert response.status_code == 200
    chunks = normalized(response)
    assert ''.join(chunk['choices'][0]['delta'].get('content', '') for chunk in chunks) == 'legacy chat'
    assert chunks[-1]['choices'][0]['finish_reason'] == 'stop'
    assert chunks[-1]['potluck'] == {'finish_reason_inferred': True}


def test_batch_peer_also_replaces_private_engine_path(monkeypatch):
    class Runtime:
        def is_loaded(self): return True
        def current_model_id(self): return 'test'
        def assert_model_identity(self, *args): pass

        async def stream_chat_raw(self, *args, **kwargs):
            yield {'model': MODEL_PATH, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}

    response = client(Runtime(), monkeypatch).post('/peer/chat/completions', json={
        'model': 'test', 'messages': [USER], 'stream': True,
        'potluck_batch': True, 'potluck_model_sha256': 'a' * 64,
    })
    assert response.status_code == 200
    assert response.headers['x-potluck-batch-protocol'] == '1'
    assert MODEL_PATH not in response.text
    assert normalized(response)[-1]['choices'][0]['finish_reason'] == 'stop'

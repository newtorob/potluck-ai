"""Exact artifact identity, fail-closed routing, and queued-load race regressions."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from potluck_local.routers import gateway, models, peer
from potluck_local.services import gateway_routing as routing
from potluck_local.services.model_identity import artifact_identity, ModelIdentityError, lock_headers
from potluck_local.services.runtime import LLMRuntime

A = hashlib.sha256(b'weights-a').hexdigest()
B = hashlib.sha256(b'weights-b').hexdigest()
SOURCE = 'https://huggingface.co/org/model/resolve/main/model-Q4_K_M.gguf'

class Model:
    def __init__(self): self.calls = 0
    def create_chat_completion(self, **kwargs):
        self.calls += 1
        if kwargs['stream']:
            return iter([{'choices': [{'delta': {'content': 'hello'}, 'finish_reason': None}]}])
        return {'choices': [{'message': {'content': 'hello'}, 'finish_reason': 'stop'}]}

@pytest.fixture
def runtime(tmp_path):
    path = tmp_path / 'test' / 'model.gguf'
    path.parent.mkdir()
    path.write_bytes(b'weights-a')
    rt = LLMRuntime()
    rt._model, rt._model_id, rt._model_path = Model(), 'test', str(path)
    rt._artifact = artifact_identity(path)
    return rt

@pytest.fixture
def client(runtime, monkeypatch):
    monkeypatch.setattr(gateway, 'get_runtime', lambda: runtime)
    monkeypatch.setattr(peer, 'get_runtime', lambda: runtime)
    monkeypatch.setattr(peer, 'get_settings', lambda: SimpleNamespace(network=SimpleNamespace(contributor_mode=True)))
    async def record(*a, **kw): pass
    monkeypatch.setattr(gateway, '_record_consumed', record)
    monkeypatch.setattr(peer, '_record_served', record)
    app = FastAPI()
    app.include_router(gateway.router, prefix='/v1')
    app.include_router(peer.router, prefix='/peer')
    app.include_router(models.router, prefix='/models')
    return TestClient(app)

def request(client, endpoint='/v1/chat/completions', *, model='test', digest=A, stream=False, scope='local'):
    body = {'model': model, 'stream': stream}
    if digest is not None: body['potluck_model_sha256'] = digest
    body.update({'prompt': 'hi'} if endpoint == '/v1/completions' else {'messages': [{'role': 'user', 'content': 'hi'}]})
    return client.post(endpoint, json=body, headers={'X-Potluck-Scope': scope})

@pytest.mark.parametrize('endpoint', ['/v1/chat/completions', '/v1/completions', '/peer/chat/completions'])
@pytest.mark.parametrize('stream', [True, False])
def test_matching_lock_serves_and_mismatches_never_generate(client, runtime, endpoint, stream):
    response = request(client, endpoint, stream=stream)
    assert response.status_code == 200 and 'hello' in response.text
    if stream:
        assert response.headers['x-potluck-model-sha256'] == A
        assert response.headers['x-potluck-model-lock'] == '1'
    assert runtime._model.calls == 1
    for model, digest in [('other', A), ('test', B), ('other', None)]:
        assert request(client, endpoint, stream=stream, model=model, digest=digest).status_code == 409
    assert runtime._model.calls == 1

@pytest.mark.parametrize('scope', ['', 'pool', 'circle', 'typo'])
def test_locked_request_never_widens_scope(client, runtime, scope):
    assert request(client, scope=scope).status_code == 400
    assert runtime._model.calls == 0

@pytest.mark.parametrize('model,digest', [(None, A), ('test', 'bad')])
def test_incomplete_or_invalid_identity_rejected(client, runtime, model, digest):
    assert request(client, model=model, digest=digest).status_code in (400, 422)
    assert runtime._model.calls == 0

def test_same_slug_different_bytes_routes_to_exact_household_match(client, runtime, monkeypatch):
    seen = []
    async def find(model, digest):
        seen.append((model, digest))
        return {'mesh_ip': '100.64.0.2'}
    async def complete(*a, **kw):
        assert kw['sha256'] == B
        return {'choices': [{'message': {'content': 'peer'}}]}
    monkeypatch.setattr(routing, 'peer_for_model', find)
    monkeypatch.setattr(routing, 'complete_peer_openai', complete)
    assert request(client, digest=B, scope='mine').status_code == 200
    assert seen == [('test', B)] and runtime._model.calls == 0

@pytest.mark.parametrize('entry', ['chat', 'stream_chat', 'stream_chat_raw'])
@pytest.mark.parametrize('change', ['slug', 'digest', 'backend'])
def test_identity_checked_after_waiting_for_inference_admission(runtime, entry, change):
    async def run():
        await runtime._inference_lock.acquire()
        async def invoke():
            call = getattr(runtime, entry)([], expected_model='test', expected_sha256=A)
            if entry == 'chat': return await call
            return [c async for c in call]
        task = asyncio.create_task(invoke())
        await asyncio.sleep(0)
        if change == 'slug': runtime._model_id = 'new-model'
        elif change == 'digest': runtime._artifact = (B, runtime._artifact[1])
        else: runtime._loaded_rpc_servers = ('100.64.0.2:50052',)
        runtime._inference_lock.release()
        with pytest.raises(ModelIdentityError): await task
        assert runtime._model.calls == 0
    asyncio.run(run())

def test_changed_file_invalidates_loaded_digest(runtime):
    assert runtime.current_model_sha256() == A
    Path(runtime._model_path).write_bytes(b'weights-b')
    assert runtime.current_model_sha256() is None
    with pytest.raises(ModelIdentityError): runtime.assert_model_identity('test', A)

def test_pin_hashes_actual_bytes_and_sync_checks_existing_file(client, monkeypatch, tmp_path):
    monkeypatch.setattr(models, 'models_dir', lambda: tmp_path)
    manifest = {'sha256': B, 'download_url': SOURCE}
    monkeypatch.setattr(models.model_registry, 'get_manifest', lambda _: manifest)
    (tmp_path / 'test' / 'model.gguf.sha256').write_text(B)
    model = client.get('/models/test/identity').json()['model']
    assert model['sha256'] == A and 'source' not in model
    manifest['sha256'] = A
    assert client.get('/models/test/identity').json()['model']['source']['url'] == SOURCE
    assert client.post('/models/test/install-locked', json=model).json()['status'] == 'verified'
    model['sha256'] = B
    assert client.post('/models/test/install-locked', json=model).status_code == 409
    assert (tmp_path / 'test' / 'model.gguf').read_bytes() == b'weights-a'

def test_locked_install_uses_lock_not_updated_catalog(client, monkeypatch, tmp_path):
    monkeypatch.setattr(models, 'models_dir', lambda: tmp_path)
    monkeypatch.setattr(models.model_registry, 'get_manifest', lambda _: {'sha256': B})
    calls = []
    async def download(**kwargs): calls.append(kwargs)
    monkeypatch.setattr(models, 'get_downloader', lambda: SimpleNamespace(get_progress=lambda _: None, download_model=download))
    lock = {'id': 'new', 'format': 'gguf', 'sha256': A, 'sizeBytes': 9, 'source': {'url': SOURCE}}
    assert client.post('/models/new/install-locked', json=lock).status_code == 202
    assert calls == [{'model_id': 'new', 'url': SOURCE, 'expected_sha256': A, 'expected_size': 9}]
    for url in ['http://127.0.0.1/private', SOURCE + '?token=secret', 'https://huggingface.co/x/y/resolve/main/../../bad.gguf']:
        lock['source']['url'] = url
        assert client.post('/models/new/install-locked', json=lock).status_code == 422
    assert len(calls) == 1

@pytest.mark.parametrize('acknowledge', [True, False])
def test_household_retries_preserve_digest_and_skip_old_or_mismatched_peers(monkeypatch, acknowledge):
    real_client = httpx.AsyncClient
    peers = [{'mesh_ip': f'100.64.0.{i}'} for i in range(2, 6)]
    async def roster(): return peers
    monkeypatch.setattr(routing, 'household_peers', roster)
    requests = []
    def handler(req):
        host = req.url.host
        if req.url.path.endswith('/capabilities'):
            return httpx.Response(200, json={'available_for_inference': True, 'loaded_model': 'test',
                'model_lock_version': 1 if host != '100.64.0.3' else 0,
                'loaded_model_detail': {'sha256': B if host == '100.64.0.4' else A}})
        requests.append((host, json.loads(req.content)))
        if host == '100.64.0.2': return httpx.Response(503)
        return httpx.Response(200, headers=lock_headers(A) if acknowledge else {},
                             content=b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    async def run():
        assert (await routing.peer_for_model('test', A))['mesh_ip'] == '100.64.0.2'
        output = []
        async def collect():
            async for chunk in routing.stream_peer_openai(peers[0], [], 'test', 0.2, 8, sha256=A): output.append(chunk)
        if acknowledge: await collect()
        else:
            with pytest.raises(routing.RemoteStreamError): await collect()
            assert not output
    asyncio.run(run())
    assert [host for host, _ in requests] == ['100.64.0.2', '100.64.0.5']
    assert all(body['potluck_model_sha256'] == A for _, body in requests)

@pytest.mark.parametrize('endpoint', ['/v1/chat/completions', '/v1/completions', '/peer/chat/completions'])
def test_swap_after_route_selection_returns_stream_error_before_any_tokens(client, runtime, monkeypatch, endpoint):
    original = runtime.assert_model_identity
    def swap_after_preflight(model, digest=None):
        original(model, digest)
        runtime._model_id = 'swapped'
    monkeypatch.setattr(runtime, 'assert_model_identity', swap_after_preflight)
    response = request(client, endpoint, stream=True)
    assert response.status_code == 200
    assert 'model_mismatch' in response.text
    assert 'hello' not in response.text
    assert runtime._model.calls == 0

@pytest.mark.parametrize('mutation', [None, 'replace', 'remove'])
def test_native_load_captures_bytes_and_invalidates_changes_during_load(tmp_path, monkeypatch, mutation):
    import sys
    path = tmp_path / 'test' / 'model.gguf'
    path.parent.mkdir()
    path.write_bytes(b'weights-a')
    def native(**kwargs):
        if mutation == 'replace': path.write_bytes(b'weights-b')
        if mutation == 'remove': path.unlink()
        return Model()
    monkeypatch.setitem(sys.modules, 'llama_cpp', SimpleNamespace(Llama=native))
    rt = LLMRuntime()
    rt._atexit_registered = True
    rt.load_model(str(path))
    assert rt.current_model_sha256() == (A if mutation is None else None)
    rt.unload_model()
    assert rt.current_model_sha256() is None

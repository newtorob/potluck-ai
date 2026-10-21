"""Pinned recipe operations must not silently use catalog or global settings."""
import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from potluck_local.routers import models
from potluck_local.services import model_lifecycle, model_recipes, runtime, local_server
from potluck_local.services.model_identity import ModelIdentityError
from potluck_local.services.settings_manager import get_settings


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    recipe = {'id': 'tiny-cpu', 'model': {'id': 'tiny', 'sha256': 'a' * 64,
              'size_bytes': 120, 'repository': 'owner/model', 'revision': 'b' * 40,
              'filename': 'tiny.gguf'},
              'runtime': {'context_size': 4096, 'gpu_layers': 0, 'native_cache_mib': 128},
              'engine': {'backend': 'llama-server', 'acceleration': 'cpu'}}
    report = {'can_install': True, 'can_load': True, 'installed': True,
              'artifact_verified': True, 'issues': [], 'recipe': recipe}
    monkeypatch.setattr(model_recipes, 'get_recipe', lambda name: recipe if name == 'tiny-cpu' else None)
    monkeypatch.setattr(model_recipes, 'load_recipes', lambda: [recipe])
    monkeypatch.setattr(model_recipes, 'preflight_recipe', lambda *a, **kw: report.copy())
    model_path = tmp_path / 'models' / 'tiny' / 'model.gguf'
    monkeypatch.setattr(model_recipes, 'recipe_path', lambda _: model_path)
    monkeypatch.setattr(model_recipes, 'recipe_url', lambda _: 'https://huggingface.co/owner/model/resolve/' + 'b' * 40 + '/tiny.gguf')
    lifecycle = model_lifecycle.ModelLifecycle()
    monkeypatch.setattr(model_lifecycle, '_lifecycle', lifecycle)
    rt = SimpleNamespace(load_model_async=AsyncMock(return_value={'model_id': 'tiny', 'backend': 'llama-server'}))
    monkeypatch.setattr(models, 'get_runtime', lambda: rt)
    downloader = SimpleNamespace(get_progress=lambda _: None, download_model=AsyncMock())
    monkeypatch.setattr(models, 'get_downloader', lambda: downloader)
    monkeypatch.setattr(models.model_registry, 'get_manifest', lambda _: pytest.fail('Recipe must not follow catalog'))
    app = FastAPI()
    app.include_router(models.router, prefix='/models')
    return SimpleNamespace(client=TestClient(app), recipe=recipe, report=report, runtime=rt,
                           downloader=downloader, lifecycle=lifecycle, path=model_path)


def test_inspection_does_not_download_load_or_change_startup(rig):
    rig.lifecycle.remember('previous')
    assert rig.client.get('/models/recipes').json() == {'schema_version': 1, 'recipes': [rig.recipe]}
    assert rig.client.get('/models/recipes/tiny-cpu/preflight').json()['can_load'] is True
    assert get_settings().runtime.startup_model_id == 'previous'
    rig.runtime.load_model_async.assert_not_called()
    rig.downloader.download_model.assert_not_called()


@pytest.mark.parametrize('action,method', [('preflight', 'get'), ('install', 'post'), ('load', 'post')])
def test_unknown_recipe_returns_404_without_side_effect(rig, action, method):
    response = getattr(rig.client, method)(f'/models/recipes/absent/{action}')
    assert response.status_code == 404 and 'Unknown model recipe' in response.json()['detail']
    rig.runtime.load_model_async.assert_not_called()
    rig.downloader.download_model.assert_not_called()


def test_recipe_install_uses_immutable_url_hash_and_size(rig):
    rig.report.update(installed=False, artifact_verified=False, can_load=False)
    response = rig.client.post('/models/recipes/tiny-cpu/install')
    assert response.status_code == 202
    assert response.json() == {'status': 'downloading', 'slug': 'tiny', 'recipe_id': 'tiny-cpu'}
    rig.downloader.download_model.assert_awaited_once_with(
        model_id='tiny', url='https://huggingface.co/owner/model/resolve/' + 'b' * 40 + '/tiny.gguf',
        expected_sha256='a' * 64, expected_size=120, overwrite_existing=False,
    )


def test_recipe_install_does_not_overwrite_or_race_active_download(rig):
    rig.report['artifact_verified'] = False
    assert rig.client.post('/models/recipes/tiny-cpu/install').status_code == 409
    rig.report['installed'] = False
    rig.downloader.get_progress = lambda _: SimpleNamespace(status='verifying')
    assert rig.client.post('/models/recipes/tiny-cpu/install').status_code == 409
    rig.downloader.download_model.assert_not_called()


def test_install_verified_file_succeeds_without_engine_readiness(rig):
    rig.report.update(can_install=False, can_load=False)
    assert rig.client.post('/models/recipes/tiny-cpu/install').json()['status'] == 'already_installed'
    rig.downloader.download_model.assert_not_called()


@pytest.mark.parametrize('action', ['install', 'load'])
def test_resource_failure_does_not_mutate_current_model(rig, action):
    rig.lifecycle.remember('previous')
    rig.report.update(installed=False, artifact_verified=False)
    rig.report[f'can_{action}'] = False
    rig.report['issues'] = [{'code': 'insufficient_resources', 'message': 'Free more memory.', 'blocks': [action]}]
    response = rig.client.post(f'/models/recipes/tiny-cpu/{action}')
    assert response.status_code == 409 and response.json()['detail'] == 'Free more memory.'
    assert get_settings().runtime.startup_model_id == 'previous'
    rig.runtime.load_model_async.assert_not_called()
    rig.downloader.download_model.assert_not_called()


def test_load_uses_recipe_pins_and_clears_only_restart_choice(rig):
    rig.lifecycle.remember('previous')
    before = get_settings().model_dump()
    response = rig.client.post('/models/recipes/tiny-cpu/load')
    assert response.status_code == 200
    assert response.json()['autoload'] is False and response.json()['status'] == 'loaded'
    rig.runtime.load_model_async.assert_awaited_once_with(
        str(rig.path), n_ctx=4096, n_gpu_layers=0, backend='llama-server', native_cache_mib=128,
        expected_model_sha256='a' * 64, expected_engine_acceleration='cpu',
    )
    after = get_settings().model_dump()
    before['runtime']['startup_model_id'] = None
    assert before == after


def test_load_detecting_identity_change_keeps_previous_startup_choice(rig):
    rig.lifecycle.remember('previous')
    rig.runtime.load_model_async.side_effect = ModelIdentityError('Model changed after preflight.')
    response = rig.client.post('/models/recipes/tiny-cpu/load')
    assert response.status_code == 409
    assert get_settings().runtime.startup_model_id == 'previous'


@pytest.mark.parametrize('failure', ['hash', 'acceleration', 'offload'])
def test_native_pin_recheck_preserves_running_model_before_release(tmp_path, monkeypatch, failure):
    path = tmp_path / 'model.gguf'
    path.write_bytes(b'real bytes')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(local_server, 'bundle', lambda: (tmp_path, {'backend': 'cpu'}))
    rt = runtime.LLMRuntime()
    rt._model = current = object()
    monkeypatch.setattr(rt, '_release_model', lambda: pytest.fail('Existing model was released'))
    monkeypatch.setattr(local_server, 'start', lambda *a, **kw: pytest.fail('Wrong recipe started'))
    kwargs = dict(n_ctx=4096, n_gpu_layers=0, n_threads=None, cache_mib=128,
                  expected_model_sha256=digest, expected_engine_acceleration='cpu')
    if failure == 'hash':
        kwargs['expected_model_sha256'] = '0' * 64
    elif failure == 'acceleration':
        kwargs.update(expected_engine_acceleration='metal', n_gpu_layers=-1)
    else:
        kwargs['n_gpu_layers'] = -1
    with pytest.raises((ModelIdentityError, ValueError)):
        rt._load_local_server(str(path), **kwargs)
    assert rt._model is current


def test_native_pins_cannot_be_ignored_by_python_backend():
    rt = runtime.LLMRuntime()
    with pytest.raises(ValueError, match='native local backend'):
        asyncio.run(rt.load_model_async('unused', expected_model_sha256='a' * 64))

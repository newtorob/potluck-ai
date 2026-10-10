import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from potluck_local.services import model_setup as setup
from potluck_local.routers import models
from potluck_local.services.model_identity import ModelIdentityError

@pytest.fixture
def options(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, 'models_dir', lambda: tmp_path)
    monkeypatch.setattr(setup, 'detect_hardware', lambda: {'ram_total_gb': 16, 'ram_available_gb': 6, 'gpu_name': None})
    monkeypatch.setattr(setup.shutil, 'disk_usage', lambda _: SimpleNamespace(free=5 * setup.GIB))
    monkeypatch.setattr(setup, 'get_runtime', lambda: SimpleNamespace(is_loaded=lambda: False))
    entries = [
        {'slug': 'large', 'size_bytes': 8 * setup.GIB, 'min_ram_gb': 24, 'is_featured': True},
        {'slug': 'small', 'size_bytes': setup.GIB, 'min_ram_gb': 4},
        {'slug': 'medium', 'size_bytes': 2 * setup.GIB, 'min_ram_gb': 8, 'is_featured': True},
    ]
    monkeypatch.setattr(setup.model_registry, 'list_models', lambda: entries)
    return entries, tmp_path

def test_recommendation_checks_available_memory_and_model_disk_not_only_gpu_tier(options):
    result = setup.setup_options()
    assert result['recommendedModel'] == 'medium'
    assert not next(m for m in result['models'] if m['id'] == 'large')['fits']
    assert result['hardware']['gpu_name'] is None  # CPU-only is a supported option

def test_installed_model_preferred_over_new_download(options):
    entries, _ = options
    entries[1].update(installed=True, size_on_disk=setup.GIB)
    assert setup.setup_options()['recommendedModel'] == 'small'

def test_imported_model_available_without_catalog(options):
    _, base = options
    folder = base / 'imported'
    folder.mkdir(); (folder / 'model.gguf').write_bytes(b'weights')
    assert setup.setup_options()['recommendedModel'] == 'imported'

def test_no_download_recommendation_when_disk_or_ram_is_insufficient(options, monkeypatch):
    monkeypatch.setattr(setup.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1))
    assert setup.setup_options()['recommendedModel'] is None

def test_existing_loaded_model_does_not_require_its_memory_twice(options, monkeypatch):
    entries, _ = options
    entries[1].update(installed=True, size_on_disk=setup.GIB)
    monkeypatch.setattr(setup, 'detect_hardware', lambda: {'ram_total_gb': 16, 'ram_available_gb': 0.5})
    monkeypatch.setattr(setup, 'get_runtime', lambda: SimpleNamespace(is_loaded=lambda: True, current_model_id=lambda: 'small'))
    assert setup.setup_options()['recommendedModel'] == 'small'

# With runtime.verify_answers off the route keeps its first behaviour: some text must come back.
# The answer check that replaced it by default is in test_acceptance_routes.py.
@pytest.mark.parametrize('result,status', [({'choices': [{'message': {'content': '4'}}]}, 200), ({'choices': []}, 503),
                                          ({'choices': [{'message': {'content': ''}}]}, 503)])
def test_local_probe_requires_generated_content_and_exact_identity(monkeypatch, answer_check_off, result, status):
    chat = AsyncMock(return_value=result)
    monkeypatch.setattr(models, 'get_runtime', lambda: SimpleNamespace(chat=chat))
    app = FastAPI(); app.include_router(models.router, prefix='/models')
    response = TestClient(app).post('/models/small/test', json={'sha256': 'a' * 64})
    assert response.status_code == status
    assert chat.call_args.kwargs['expected_model'] == 'small'
    assert chat.call_args.kwargs['expected_sha256'] == 'a' * 64
    assert chat.call_args.args[0][0]['content'].endswith('/no_think')

def test_probe_model_swap_is_clear_conflict(monkeypatch, answer_check_off):
    monkeypatch.setattr(models, 'get_runtime', lambda: SimpleNamespace(chat=AsyncMock(side_effect=ModelIdentityError('changed'))))
    app = FastAPI(); app.include_router(models.router, prefix='/models')
    assert TestClient(app).post('/models/small/test', json={}).status_code == 409

def test_matching_partial_download_needs_only_remaining_disk_space(options, monkeypatch):
    import json
    entries, base = options
    entries[1].update(download_url='https://example.test/small.gguf', sha256='a' * 64)
    folder = base / 'small'; folder.mkdir()
    with (folder / 'model.gguf.partial').open('wb') as partial:
        partial.truncate(int(0.9 * setup.GIB))
    (folder / 'model.gguf.partial.meta').write_text(json.dumps({'url': entries[1]['download_url'], 'sha256': 'a' * 64}))
    monkeypatch.setattr(setup.shutil, 'disk_usage', lambda _: SimpleNamespace(free=int(0.5 * setup.GIB)))
    assert setup.setup_options()['recommendedModel'] == 'small'
    entries[1]['sha256'] = 'b' * 64
    assert setup.setup_options()['recommendedModel'] is None

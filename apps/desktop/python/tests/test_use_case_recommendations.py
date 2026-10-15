"""Task guidance changes picks only within the existing evidence/resource gates."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from potluck_local.services import model_registry, model_setup
from potluck_local.services import acceptance
from potluck_local.routers.models import router
from test_measured_fit import Lab


@pytest.mark.parametrize('use_case,expected', [
    ('general', 'qwen3-8b-q4'), ('coding', 'qwen3-8b-q4'),
    ('writing', 'ministral-8b-instruct-2410-q4'), ('reasoning', 'deepseek-r1-distill-llama-8b-q4'),
])
def test_real_catalog_guidance_selects_different_fitting_models(tmp_path, monkeypatch, use_case, expected):
    lab = Lab(tmp_path, monkeypatch, available=12, total=16)
    lab.hardware.update(os='Darwin', arch='arm64', gpu_vram_gb=16)
    lab.entries = model_registry._load_bundled_catalog()['models']
    out = model_setup.setup_options(use_case)
    assert out['useCase'] == use_case
    assert out['recommended'][0]['id'] == expected
    assert all(next(m for m in out['models'] if m['id'] == p['id'])['fits'] for p in out['recommended'])


def test_task_match_cannot_override_failed_checks_or_resource_limits(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=6)
    lab.model('too-big', 8)
    lab.entries[-1]['use_cases'] = ['coding']
    lab.model('failed', 2)
    lab.entries[-1]['use_cases'] = ['coding']
    lab.prove('failed', verdict=acceptance.MODEL_FAILED)
    lab.model('fallback', 1)
    out = model_setup.setup_options('coding')
    assert [p['id'] for p in out['recommended']] == ['fallback']


def test_proven_model_stays_ahead_of_an_unproven_task_match(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=8)
    lab.model('proven', 1)
    lab.prove('proven', host=1)
    lab.bench('proven', 25)
    lab.model('coder', 2)
    lab.entries[-1]['use_cases'] = ['coding']
    assert model_setup.setup_options('coding')['recommended'][0]['id'] == 'proven'


def test_task_guidance_breaks_ties_between_proven_models(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=8)
    for slug, uses in [('writer', ['writing']), ('coder', ['coding', 'writing'])]:
        lab.model(slug, 2)
        lab.entries[-1]['use_cases'] = uses
        lab.prove(slug, host=2)
        lab.bench(slug, 25)
    assert model_setup.setup_options('writing')['recommended'][0]['id'] == 'writer'
    assert model_setup.setup_options('coding')['recommended'][0]['id'] == 'coder'


def test_http_use_is_validated_before_hardware_work(monkeypatch):
    calls = []
    def options(use_case):
        calls.append(use_case)
        return {'useCase': use_case}
    monkeypatch.setattr(model_setup, 'setup_options', options)
    app = FastAPI()
    app.include_router(router, prefix='/models')
    client = TestClient(app)
    assert client.get('/models/setup-options?use=writing').json() == {'useCase': 'writing'}
    assert client.get('/models/setup-options?use=invalid').status_code == 422
    assert calls == ['writing']

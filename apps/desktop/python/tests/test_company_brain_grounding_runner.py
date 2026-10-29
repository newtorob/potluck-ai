import asyncio
import json
import os
import shutil

import pytest

from potluck_local.services import company_brain_grounding_runner as runner


class FakeRuntime:
    def __init__(self, sha):
        self.sha = sha
        self.loads = 0
        self.unloaded = False

    def load_model(self, path, **settings):
        self.loads += 1
        assert settings == {'n_ctx': 8192, 'n_threads': 8, 'n_gpu_layers': 0,
                            'cpu_only': True, 'rpc_servers': None}
        return {'settings': settings}

    def current_model_id(self):
        return 'fixture-model'

    def current_model_sha256(self):
        return self.sha

    def unload_model(self):
        self.unloaded = True

    async def chat(self, messages, **kwargs):
        assert kwargs['response_format']['type'] == 'json_object'
        brief = {'facts': [], 'prior_fixes': [], 'hypotheses': [],
                 'recommended_checks': [], 'model_uncertainties': ['Not assessed']}
        return {'choices': [{'message': {'content': json.dumps(brief)}}]}


def test_suite_hash_and_rubric_checked_before_model_or_output(tmp_path, monkeypatch):
    fixtures = tmp_path / 'fixtures'
    shutil.copytree(runner.FIXTURES, fixtures)
    monkeypatch.setattr(runner, 'FIXTURES', fixtures)
    model = tmp_path / 'model.gguf'
    model.write_bytes(b'fake')
    sha = runner.base.digest(model.read_bytes())
    runtime = FakeRuntime(sha)
    monkeypatch.setattr(runner, 'get_runtime', lambda: runtime)
    for name in ('development', 'rubric'):
        path = fixtures / f'{name}.json'
        original = path.read_bytes()
        path.write_bytes(original + b' ')
        with pytest.raises(ValueError, match='hash mismatch'):
            asyncio.run(runner.run(tmp_path / 'run', 'development', model, sha, 8192, 8))
        assert not (tmp_path / 'run').exists() and runtime.loads == 0
        path.write_bytes(original)


def test_run_captures_results_restores_environment_and_refuses_overwrite(tmp_path, monkeypatch):
    model = tmp_path / 'model.gguf'
    model.write_bytes(b'fake')
    sha = runner.base.digest(model.read_bytes())
    runtime = FakeRuntime(sha)
    monkeypatch.setattr(runner, 'get_runtime', lambda: runtime)
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path / 'live'))
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'live.sqlite'))
    out = tmp_path / 'run'
    asyncio.run(runner.run(out, 'development', model, sha, 8192, 8))
    assert runtime.loads == 1 and runtime.unloaded
    assert os.environ['POTLUCK_DATA_DIR'] == str(tmp_path / 'live')
    assert os.environ['POTLUCK_MEMORY_DB_PATH'] == str(tmp_path / 'live.sqlite')
    assert not (tmp_path / 'live.sqlite').exists()
    results = json.loads((out / 'raw.json').read_text())
    assert results['assessment'] is None and len(results['outputs']) == 3
    for cid, item in results['outputs'].items():
        assert item['status'] == 'success' and item['semantic_support'] is None
        assert item['latency_seconds'] >= 0
        folder = out / 'attempts' / cid / 'raw'
        assert all((folder / name).exists() for name in ('request.json', 'raw-reply.json', 'result.json'))
    metadata = json.loads((out / 'run-metadata.json').read_text())
    assert metadata['suite_sha256'] == runner.base.digest((out / 'development.json').read_bytes())
    assert metadata['rubric_sha256'] == runner.base.digest((out / 'rubric.json').read_bytes())
    with pytest.raises(FileExistsError):
        asyncio.run(runner.run(out, 'development', model, sha, 8192, 8))
    assert runtime.loads == 1


def test_bad_model_and_settings_do_not_load_or_create_output(tmp_path, monkeypatch):
    model = tmp_path / 'model.gguf'
    model.write_bytes(b'fake')
    sha = runner.base.digest(model.read_bytes())
    runtime = FakeRuntime(sha)
    monkeypatch.setattr(runner, 'get_runtime', lambda: runtime)
    for path, expected, context, threads in [(model, '0' * 64, 8192, 8),
                                              (model, sha, 4096, 8),
                                              (tmp_path / 'missing', sha, 8192, 8)]:
        with pytest.raises(ValueError):
            asyncio.run(runner.run(tmp_path / 'run', 'fresh', path, expected, context, threads))
    assert runtime.loads == 0 and not (tmp_path / 'run').exists()


def test_load_failure_is_recorded_and_unloads(tmp_path, monkeypatch):
    model = tmp_path / 'model.gguf'
    model.write_bytes(b'fake')
    sha = runner.base.digest(model.read_bytes())
    class Failed(FakeRuntime):
        def load_model(self, path, **settings):
            raise RuntimeError('fixture load failure')
    runtime = Failed(sha)
    monkeypatch.setattr(runner, 'get_runtime', lambda: runtime)
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path / 'live'))
    with pytest.raises(RuntimeError, match='fixture load failure'):
        asyncio.run(runner.run(tmp_path / 'run', 'fresh', model, sha, 8192, 8))
    assert runtime.unloaded and os.environ['POTLUCK_DATA_DIR'] == str(tmp_path / 'live')
    assert json.loads((tmp_path / 'run' / 'run-error.json').read_text())['type'] == 'RuntimeError'

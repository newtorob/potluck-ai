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


def test_custom_frozen_fixture_keeps_failures_and_ignores_private_data(tmp_path, monkeypatch):
    fixtures = tmp_path / 'custom-fixtures'
    shutil.copytree(runner.FIXTURES, fixtures)
    manifest = {'corpus_id': 'different-corpus', 'cases': [
        {'id': 'local-case', 'allowed_source': '# Example\nA local fact.',
         'question': 'What fact is recorded?'}]}
    content = json.dumps(manifest).encode()
    (fixtures / 'fresh.json').write_bytes(content)
    (fixtures / 'fresh.sha256').write_text(runner.base.digest(content) + '  fresh.json\n')
    rubric = json.dumps({'cases': {'local-case': {}}}).encode()
    (fixtures / 'rubric.json').write_bytes(rubric)
    (fixtures / 'rubric.sha256').write_text(runner.base.digest(rubric) + '  rubric.json\n')
    model = tmp_path / 'model.gguf'
    model.write_bytes(b'fake')
    sha = runner.base.digest(model.read_bytes())
    class Invalid(FakeRuntime):
        async def chat(self, messages, **kwargs):
            return {'choices': [{'message': {'content': 'not json'}}]}
    runtime = Invalid(sha)
    monkeypatch.setattr(runner, 'get_runtime', lambda: runtime)
    out = tmp_path / 'run'
    asyncio.run(runner.run(out, 'fresh', model, sha, 8192, 8, fixtures))
    raw = json.loads((out / 'raw.json').read_text())
    assert raw['corpus_id'] == 'different-corpus'
    assert raw['outputs']['local-case']['status'] == 'error'
    folder = out / 'attempts' / 'local-case' / 'raw'
    assert (folder / 'raw-reply.json').exists() and (folder / 'result.json').exists()
    assert json.loads((folder / 'result.json').read_text())['failure_stage'] == 'inference_or_validation'
    assert (out / '.gitignore').read_text() == '/data/\n**/*.sqlite*\n'
    assert (out / 'fresh.json').read_bytes() == content
    assert runtime.unloaded


def test_custom_fixture_invalid_id_and_hash_fail_before_loading(tmp_path, monkeypatch):
    fixtures = tmp_path / 'custom-fixtures'
    shutil.copytree(runner.FIXTURES, fixtures)
    model = tmp_path / 'model.gguf'
    model.write_bytes(b'fake')
    sha = runner.base.digest(model.read_bytes())
    runtime = FakeRuntime(sha)
    monkeypatch.setattr(runner, 'get_runtime', lambda: runtime)
    path = fixtures / 'development.json'
    manifest = json.loads(path.read_text())
    manifest['cases'][0]['id'] = '../outside'
    content = json.dumps(manifest).encode()
    path.write_bytes(content)
    (fixtures / 'development.sha256').write_text(runner.base.digest(content) + '\n')
    with pytest.raises(ValueError, match='Invalid frozen suite'):
        asyncio.run(runner.run(tmp_path / 'run', 'development', model, sha, 8192, 8, fixtures))
    path.write_bytes(content + b' ')
    with pytest.raises(ValueError, match='hash mismatch'):
        asyncio.run(runner.run(tmp_path / 'run', 'development', model, sha, 8192, 8, fixtures))
    assert runtime.loads == 0 and not (tmp_path / 'run').exists()


def test_interrupted_run_keeps_completed_attempt_and_error(tmp_path, monkeypatch):
    model = tmp_path / 'model.gguf'
    model.write_bytes(b'fake')
    sha = runner.base.digest(model.read_bytes())
    runtime = FakeRuntime(sha)
    monkeypatch.setattr(runner, 'get_runtime', lambda: runtime)
    original = runner.base.attempt
    count = 0
    async def interrupted(out, case, arm, runtime_arg, model_arg, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError('attempt setup failed')
        return await original(out, case, arm, runtime_arg, model_arg, **kwargs)
    monkeypatch.setattr(runner.base, 'attempt', interrupted)
    out = tmp_path / 'run'
    with pytest.raises(RuntimeError, match='attempt setup failed'):
        asyncio.run(runner.run(out, 'development', model, sha, 8192, 8))
    first_id = runner.read_suite('development')[0]['cases'][0]['id']
    assert (out / 'attempts' / first_id / 'raw' / 'result.json').exists()
    assert json.loads((out / 'run-error.json').read_text())['message'] == 'attempt setup failed'
    assert not (out / 'raw.json').exists() and runtime.unloaded


def test_template_selection_is_forwarded_and_invalid_fails_before_load(tmp_path, monkeypatch):
    model = tmp_path / 'model.gguf'
    model.write_bytes(b'fake')
    sha = runner.base.digest(model.read_bytes())
    runtime = FakeRuntime(sha)
    monkeypatch.setattr(runner, 'get_runtime', lambda: runtime)
    with pytest.raises(ValueError, match='template version'):
        asyncio.run(runner.run(tmp_path / 'bad', 'development', model, sha, 8192, 8,
                               template_version='invalid'))
    assert runtime.loads == 0 and not (tmp_path / 'bad').exists()
    out = tmp_path / 'v5'
    asyncio.run(runner.run(out, 'development', model, sha, 8192, 8,
                           template_version='investigation-v5'))
    metadata = json.loads((out / 'run-metadata.json').read_text())
    raw = json.loads((out / 'raw.json').read_text())
    assert metadata['template_version'] == raw['template_version'] == 'investigation-v5'
    for item in raw['outputs'].values():
        assert item['status'] == 'success'
        assert item['result']['provenance']['template_version'] == 'investigation-v5'

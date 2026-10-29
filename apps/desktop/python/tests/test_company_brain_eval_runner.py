import asyncio
import json
import os
from pathlib import Path

import aiosqlite
import pytest

from potluck_local.services import company_brain_eval_runner as runner


class FakeRuntime:
    def current_model_id(self):
        return 'fixture-model'

    def current_model_sha256(self):
        return 'a' * 64

    async def chat(self, messages, **kwargs):
        brief = {'facts': [], 'prior_fixes': [], 'hypotheses': [], 'recommended_checks': [],
                 'model_uncertainties': ['Not assessed']}
        return {'choices': [{'message': {'content': json.dumps(brief)}}]}


def test_frozen_cases_exercise_actual_lifecycle_and_capture(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path / 'data'))
    cases = runner.frozen_manifest()['cases']
    async def run():
        results = {}
        for case in cases:
            for arm in ('baseline', 'candidate'):
                results[case['id'], arm] = await runner.attempt(tmp_path, case, arm, FakeRuntime(), 'fixture-model')
        return results
    results = asyncio.run(run())
    for case in cases:
        for arm in ('baseline', 'candidate'):
            item = results[case['id'], arm]
            assert item['latency_seconds'] >= 0
            assert item['semantic_support'] is None
            folder = tmp_path / 'attempts' / case['id'] / arm
            assert (folder / 'result.json').exists()
            if case['id'] == 'stale-source':
                assert item['status'] == 'error' and item['failure_stage'] == 'pre_inference'
                assert not (folder / 'request.json').exists()
                retrieval = json.loads((folder / 'retrieval.json').read_text())
                assert retrieval['result']['scope_status']['ready'] == 0
            else:
                assert item['status'] == 'success', item
                assert (folder / 'raw-reply.json').exists()
                assert (folder / 'retrieval.json').exists()
                prompt = json.loads(json.loads((folder / 'request.json').read_text())['messages'][1]['content'])
                assert case['expected'] not in json.dumps(prompt)
                assert 'ORANGE-72' not in json.dumps(prompt)
    for case_id in ('factual-grounding', 'superseded-claim'):
        baseline_evidence = results[case_id, 'baseline']['result']['evidence']
        candidate_evidence = results[case_id, 'candidate']['result']['evidence']
        assert all(item['kind'] != 'reviewed_claim' for item in baseline_evidence)
        assert any(item['kind'] == 'reviewed_claim' for item in candidate_evidence)
        assert all('previously pending' not in item['content'] for item in candidate_evidence)
    for arm in ('baseline', 'candidate'):
        folder = tmp_path / 'attempts' / 'scope-isolation' / arm
        allowed = json.loads((folder / 'allowed-registration.json').read_text())
        excluded = json.loads((folder / 'excluded-registration.json').read_text())
        assert excluded['workspace'] != allowed['workspace']
        assert 'ORANGE-72' not in (folder / 'retrieval.json').read_text()
    async def check():
        db_path = tmp_path / 'attempts' / 'superseded-claim' / 'candidate' / 'isolated.sqlite'
        async with aiosqlite.connect(db_path) as db:
            rows = await (await db.execute('SELECT status FROM reviewed_claims ORDER BY status')).fetchall()
        assert rows == [('approved',), ('superseded',)]
    asyncio.run(check())


def test_preserves_failed_raw_reply_and_refuses_existing_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path / 'data'))
    class Invalid(FakeRuntime):
        async def chat(self, messages, **kwargs):
            return {'choices': [{'message': {'content': 'not json'}}]}
    case = runner.frozen_manifest()['cases'][-1]
    result = asyncio.run(runner.attempt(tmp_path, case, 'baseline', Invalid(), 'fixture-model'))
    assert result['status'] == 'error'
    folder = tmp_path / 'attempts' / case['id'] / 'baseline'
    assert json.loads((folder / 'raw-reply.json').read_text())['choices'][0]['message']['content'] == 'not json'
    assert json.loads((folder / 'retrieval.json').read_text())['result']['source_chunks']
    with pytest.raises(FileExistsError):
        asyncio.run(runner.attempt(tmp_path, case, 'baseline', Invalid(), 'fixture-model'))


def test_rejects_changed_fixture(tmp_path, monkeypatch):
    fixtures = tmp_path / 'fixtures'
    fixtures.mkdir()
    for path in runner.FIXTURES.iterdir():
        if path.suffix in ('.json', '.sha256'):
            (fixtures / path.name).write_bytes(path.read_bytes())
    (fixtures / 'synthetic_cases.json').write_text('{}')
    monkeypatch.setattr(runner, 'FIXTURES', fixtures)
    with pytest.raises(ValueError, match='hash mismatch'):
        runner.frozen_manifest()


def test_full_run_is_isolated_and_refuses_existing_output(tmp_path, monkeypatch):
    model = tmp_path / 'model.gguf'
    model.write_bytes(b'fake local model')
    sha = runner.digest(model.read_bytes())
    live_data = tmp_path / 'live-data'
    live_data.mkdir()
    config = live_data / 'config.json'
    config.write_text('{"marker":"unchanged"}')
    live_db = tmp_path / 'live.sqlite'
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(live_data))
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(live_db))

    class Loaded(FakeRuntime):
        unloaded = False
        loads = 0
        def load_model(self, path, **kwargs):
            self.loads += 1
            assert kwargs == {'n_ctx': 4096, 'n_threads': 2, 'n_gpu_layers': 0,
                              'cpu_only': True, 'rpc_servers': None}
            return {'actual_settings': kwargs}
        def current_model_sha256(self):
            return sha
        def unload_model(self):
            self.unloaded = True
    runtime = Loaded()
    monkeypatch.setattr(runner, 'get_runtime', lambda: runtime)
    out = tmp_path / 'new-run'
    asyncio.run(runner.run(out, model, sha, 4096, 2))
    assert runtime.unloaded and runtime.loads == 1
    assert os.environ['POTLUCK_DATA_DIR'] == str(live_data)
    assert os.environ['POTLUCK_MEMORY_DB_PATH'] == str(live_db)
    assert config.read_text() == '{"marker":"unchanged"}' and not live_db.exists()
    assert json.loads((out / 'manifest.json').read_text()) == runner.frozen_manifest()
    metadata = json.loads((out / 'run-metadata.json').read_text())
    assert metadata['model_config']['settings']['n_gpu_layers'] == 0
    assert len(json.loads((out / 'baseline.json').read_text())['outputs']) == 6
    with pytest.raises(FileExistsError):
        asyncio.run(runner.run(out, model, sha, 4096, 2))
    assert runtime.loads == 1
    link = tmp_path / 'model-link.gguf'
    link.symlink_to(model)
    with pytest.raises(ValueError, match='existing local model'):
        asyncio.run(runner.run(tmp_path / 'other-run', link, sha, 4096, 2))


def test_base_attempt_omits_template_override_by_default(tmp_path, monkeypatch):
    original = runner.investigations.create_investigation
    calls = []
    async def old_signature(scope, issue, query, model):
        calls.append(True)
        return await original(scope, issue, query, model)
    monkeypatch.setattr(runner.investigations, 'create_investigation', old_signature)
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path / 'data'))
    case = runner.frozen_manifest()['cases'][0]
    result = asyncio.run(runner.attempt(tmp_path, case, 'baseline', FakeRuntime(), 'fixture-model'))
    assert calls == [True] and result['status'] == 'success'


def test_capture_records_each_call_and_second_failure_preserves_first(tmp_path):
    class TwoCalls:
        def __init__(self):
            self.count = 0
        async def chat(self, messages, **kwargs):
            self.count += 1
            if self.count == 2:
                raise RuntimeError('second failed')
            return {'value': 'first reply'}
    capture = runner.CaptureRuntime(TwoCalls(), tmp_path)
    async def exercise():
        assert await capture.chat(['first'], temperature=0) == {'value': 'first reply'}
        with pytest.raises(RuntimeError, match='second failed'):
            await capture.chat(['second'], temperature=.7)
    asyncio.run(exercise())
    assert json.loads((tmp_path / 'request.json').read_text()) == json.loads((tmp_path / 'calls/001/request.json').read_text())
    assert json.loads((tmp_path / 'raw-reply.json').read_text()) == json.loads((tmp_path / 'calls/001/raw-reply.json').read_text())
    assert json.loads((tmp_path / 'calls/002/request.json').read_text())['messages'] == ['second']
    assert json.loads((tmp_path / 'calls/002/runtime-error.json').read_text())['message'] == 'second failed'
    assert not (tmp_path / 'runtime-error.json').exists()


def test_attempt_forwards_only_selected_options(tmp_path, monkeypatch):
    calls = []
    async def create(scope, issue, query, model, **kwargs):
        calls.append(kwargs)
        return {'ok': True}
    monkeypatch.setattr(runner.investigations, 'create_investigation', create)
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path / 'data'))
    case = runner.frozen_manifest()['cases'][0]
    result = asyncio.run(runner.attempt(tmp_path, case, 'baseline', FakeRuntime(), 'fixture-model',
        template_version='investigation-v6', generation_profile='qwen3-non-thinking', seed=0,
        questions=['What happened?']))
    assert result['status'] == 'success'
    assert calls == [{'template_version': 'investigation-v6', 'generation_profile': 'qwen3-non-thinking',
                      'seed': 0, 'questions': ['What happened?']}]

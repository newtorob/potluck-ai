import asyncio
import json
from types import SimpleNamespace

import pytest

from potluck_local.services import investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.source_collections import register_collection, scan_collection


@pytest.mark.parametrize('mode', ['factual', 'calculation', 'mixed'])
def test_bounded_workflow_keeps_request_and_replays_review(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    issue = 'What is measured, and how did average onboarding time change in days and percent?'
    rows = []
    if mode != 'calculation':
        rows.append({'request': 'What do these onboarding measurements cover?', 'operation': 'answer'})
    if mode != 'factual':
        rows += [{'request': 'What is the change in calendar days?', 'operation': 'difference'},
                 {'request': 'What is the percentage change?', 'operation': 'percent_change'}]
    calls = []

    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64

        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[1]['content'])
            calls.append(payload)
            assert kwargs['bounded_generation']
            if set(payload) == {'operations'}:
                assert len(payload['operations']) == 1
                assert payload['operations'][0]['request'] == rows[0]['request']
                data = {'R1': ['Measurement scope']}
            else:
                assert payload['issue'] == issue
                assert [r['request'] for r in payload['operations']] == [r['request'] for r in rows]
                assert 'evidence_notices' in payload
                data = {}
                if mode != 'calculation':
                    assert payload['answer_part_text_limits'] == {'R1P1': 1000}
                    sid = next(s['snippet_id'] for s in payload['snippets'] if 'completed onboardings' in s['text'])
                    data['R1'] = {'R1P1': {'status': 'supported', 'text': 'Completed onboardings only.', 'snippet_ids': [sid]}}
                if mode == 'factual':
                    assert 'quantities' not in payload and 'extraction_notices' not in payload
                    assert 'operand' not in messages[0]['content']
                else:
                    before, after = [q for q in payload['quantities'] if q['unit'] == 'calendar day']
                    assert [before['value'], after['value']] == ['20', '15']
                    for row in payload['operations']:
                        if row['operation'] == 'answer':
                            continue
                        roles = ({'minuend_id': after['id'], 'subtrahend_id': before['id']}
                                 if row['operation'] == 'difference' else
                                 {'baseline_id': before['id'], 'current_id': after['id']})
                        data[row['id']] = {'status': 'ready', 'reason': 'Cited measured averages.',
                            **roles, 'known_input_snippet_ids': list(dict.fromkeys([before['snippet_id'], after['snippet_id']])),
                            'competing_snippet_ids': [], 'limitation_snippet_ids': [], 'limitation_basis': 'none'}
            return {'choices': [{'message': {'content': json.dumps(data)}}]}

    monkeypatch.setattr(inv, 'get_runtime', Runtime)

    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'
        folder.mkdir()
        (folder / 'onboarding.md').write_text('# Onboarding\nAverage onboarding time changed from 20 to 15 calendar days.\n\nOnly completed onboardings are measured.\n')
        cid = await register_collection('w', 'docs', folder)
        await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,))
        item = await inv.create_investigation(scope, issue, 'onboarding', 'model',
            template_version='investigation-v13', reviewed_operations=rows, reviewed_plan_origin='fixture_supplied')
        p = item['provenance']
        assert p['operation_plan_origin'] == 'fixture_supplied'
        assert p['quantity_parser_version'] == 'v4'
        assert p['snippet_trace']['version'] == 'paragraph-v2'
        assert p['answer_part_plan_origin'] == (None if mode == 'calculation' else 'planned_unreviewed')
        assert p['answer_part_planner_version'] == (None if mode == 'calculation' else 'request-spans-fallback-v1')
        assert len(calls) == (1 if mode == 'calculation' else 2)
        if mode != 'calculation':
            assert p['answer_part_plan_validation']['R1']['mode'] == 'original_request_fallback'
            assert item['brief']['answers'][0]['parts'][0]['request_span'] == rows[0]['request']
        if mode != 'factual':
            traces = p['calculation_traces']
            assert [t['result'] for t in traces] == ['-5', '-25']
            assert all(t['algorithm_version'] == 'cited-calculations-v4' for t in traces)
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'owner', 'Review', corrected_brief=item['brief'])
        assert all('parts' not in a for a in reviewed['review']['corrected_brief']['answers'])
        from potluck_local.routers.investigations import export
        assert await export(item['id'], 'w', [cid])
    asyncio.run(run())

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from potluck_local.services import investigation_claim_answers as claims
from potluck_local.services import investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.source_collections import register_collection, scan_collection


def sample():
    plan = [{'id': 'R1', 'question_id': 'Q1', 'request': 'Explain the process.', 'operation': 'answer'}]
    parts = {'R1': [{'id': 'R1P1', 'request_span': 'Explain the process.'}]}
    snippets = [{'snippet_id': 'S1', 'citation_id': 'c1', 'quote': 'A fact.'}]
    return plan, parts, snippets


def decode(item, plan, parts, snippets):
    return claims.decode_results(json.dumps({'R1': {'R1P1': item}}), plan, snippets, [], [], parts)


def test_claim_limit_warning_and_correction_preserve_original_claims():
    plan, parts, snippets = sample()
    cap = claims.part_text_limits(parts)['R1P1']
    assert cap == 300
    item = {'claims': [{'text': 'x' * cap, 'status': 'supported', 'snippet_ids': ['S1']}], 'unmet_aspects': []}
    brief, _, trace = decode(item, plan, parts, snippets)
    answer = brief['answers'][0]
    assert answer['status'] == 'incomplete' and answer['model_status'] == 'supported'
    assert answer['parts'][0]['claims'][0]['text'] == 'x' * cap
    assert trace['output_integrity_warnings'][0]['reason'] == 'text_limit_reached'
    assert brief['model_uncertainties'] == [claims.WARNING]
    unchanged = deepcopy(brief)
    unchanged['answers'][0]['status'] = 'supported'
    with pytest.raises(ValueError):
        claims.corrected_brief(unchanged, brief, snippets)
    unchanged['answers'][0]['text'] += '  '
    with pytest.raises(ValueError):
        claims.corrected_brief(unchanged, brief, snippets)
    correction = deepcopy(brief)
    correction['answers'][0]['text'] = 'Reviewed text.'
    correction['model_uncertainties'] = []
    corrected = claims.corrected_brief(correction, brief, snippets)
    assert corrected['answers'][0]['status'] == 'incomplete'
    assert corrected['model_uncertainties'] == [claims.WARNING]
    assert 'parts' not in corrected['answers'][0]
    assert brief['answers'][0]['parts'][0]['claims'][0]['text'] == 'x' * cap
    correction['answers'][0]['status'] = 'supported'
    repaired = claims.corrected_brief(correction, brief, snippets)
    assert repaired['answers'][0]['status'] == 'supported'
    assert repaired['model_uncertainties'] == [claims.WARNING]
    assert 'parts' not in repaired['answers'][0]
    for status in ('unknown', 'conflicting'):
        correction['answers'][0]['status'] = status
        assert claims.corrected_brief(correction, brief, snippets)['answers'][0]['status'] == status


def test_unmet_aspect_warns_separately_from_evidence_status():
    plan, parts, snippets = sample()
    item = {'claims': [{'text': 'A fact.', 'status': 'supported', 'snippet_ids': ['S1']}],
            'unmet_aspects': ['The owner is not specified.']}
    brief, _, trace = decode(item, plan, parts, snippets)
    assert brief['answers'][0]['status'] == 'incomplete'
    assert brief['answers'][0]['parts'][0]['claims'][0]['status'] == 'supported'
    assert trace['output_integrity_warnings'][0]['reason'] == 'model_reported_unmet_aspects'


def test_six_claims_keep_individual_evidence_and_no_false_integrity_warning():
    plan, parts, snippets = sample()
    rows = [{'text': f'Fact {i}.', 'status': 'supported', 'snippet_ids': ['S1']} for i in range(6)]
    brief, _, trace = decode({'claims': rows, 'unmet_aspects': []}, plan, parts, snippets)
    result = brief['answers'][0]
    assert result['status'] == 'supported' and result['snippet_ids'] == ['S1']
    assert len(result['parts'][0]['claims']) == 6
    assert all(claim['snippet_ids'] == ['S1'] for claim in result['parts'][0]['claims'])
    assert not brief['model_uncertainties'] and not trace['output_integrity_warnings']
    assert 'not verified' in trace['coverage_assessment']


def test_unicode_and_separate_claim_citations_and_credential_redaction():
    plan, parts, snippets = sample()
    snippets.append({'snippet_id': 'S2', 'citation_id': 'c2', 'quote': 'Another fact.'})
    secret = 'sk-proj-' + 'Synthetic09_' * 4
    item = {'claims': [{'text': '完了。 ' + secret, 'status': 'supported', 'snippet_ids': ['S1']},
                       {'text': '• 次の段階', 'status': 'supported', 'snippet_ids': ['S2']}],
            'unmet_aspects': ['Missing detail: ' + secret]}
    brief, _, _ = decode(item, plan, parts, snippets)
    rows = brief['answers'][0]['parts'][0]['claims']
    assert rows[0]['snippet_ids'] == ['S1'] and rows[1]['snippet_ids'] == ['S2']
    assert secret not in json.dumps(brief)
    assert '完了。' in rows[0]['text'] and '次の段階' in rows[1]['text']


@pytest.mark.parametrize('submitted', [None, 'bad', 3, [None], [[]], ['bad'], [3]])
def test_malformed_correction_answers_fail_as_value_error(submitted):
    plan, parts, snippets = sample()
    item = {'claims': [{'text': 'A.', 'status': 'supported', 'snippet_ids': ['S1']}], 'unmet_aspects': ['detail']}
    brief, _, _ = decode(item, plan, parts, snippets)
    correction = deepcopy(brief); correction['answers'] = submitted
    with pytest.raises(ValueError):
        claims.corrected_brief(correction, brief, snippets)


@pytest.mark.parametrize('qid', [None, [], {}, 5])
def test_malformed_correction_id_fails_as_value_error(qid):
    plan, parts, snippets = sample()
    brief, _, _ = decode({'claims': [{'text': 'A.', 'status': 'supported', 'snippet_ids': ['S1']}],
                           'unmet_aspects': ['detail']}, plan, parts, snippets)
    correction = deepcopy(brief); correction['answers'][0]['question_id'] = qid
    with pytest.raises(ValueError):
        claims.corrected_brief(correction, brief, snippets)


def test_redaction_equivalent_correction_does_not_clear_integrity_warning():
    plan, parts, snippets = sample()
    secret = 'sk-proj-' + 'Synthetic09_' * 4
    item = {'claims': [{'text': 'Secret ' + secret, 'status': 'supported', 'snippet_ids': ['S1']}],
            'unmet_aspects': ['detail']}
    brief, _, _ = decode(item, plan, parts, snippets)
    correction = deepcopy(brief)
    correction['answers'][0]['status'] = 'supported'
    correction['answers'][0]['text'] = 'Secret ' + secret
    with pytest.raises(ValueError):
        claims.corrected_brief(correction, brief, snippets)


@pytest.mark.parametrize('tamper', ['claims', 'model_status', 'warning'])
def test_original_claims_status_and_warning_cannot_be_forged(tamper):
    plan, parts, snippets = sample()
    item = {'claims': [{'text': 'A.', 'status': 'supported', 'snippet_ids': ['S1']}], 'unmet_aspects': ['detail']}
    brief, _, _ = decode(item, plan, parts, snippets)
    correction = deepcopy(brief)
    if tamper == 'claims':
        correction['answers'][0]['parts'][0]['claims'][0]['text'] = 'Changed claim.'
    elif tamper == 'model_status':
        correction['answers'][0]['model_status'] = 'unknown'
    else:
        correction['model_uncertainties'] = ['Forged warning']
    with pytest.raises(ValueError):
        claims.corrected_brief(correction, brief, snippets)


def test_raw_cap_is_flagged_even_when_redaction_shortens_the_claim():
    plan, parts, snippets = sample()
    secret = 'sk-proj-' + 'Synthetic09_' * 4
    text = 'x' * (300 - len(secret) - 1) + ' ' + secret
    brief, _, trace = decode({'claims': [{'text': text, 'status': 'supported', 'snippet_ids': ['S1']}],
                              'unmet_aspects': []}, plan, parts, snippets)
    assert trace['output_integrity_warnings'][0]['reason'] == 'text_limit_reached'
    assert secret not in json.dumps(brief)


@pytest.mark.parametrize('count', [1, 2, 3, 4])
def test_claim_allocation_stays_below_total_text_bound(count):
    parts = {'R1': [{'id': f'P{i}'} for i in range(count)]}
    cap = claims.part_text_limits(parts)['P0']
    assert cap <= 300 and count * (6 * cap + 5) + count - 1 <= 4000


def test_claim_citations_validate_individually_and_cap_union():
    plan, parts, snippets = sample()
    with pytest.raises(ValueError):
        decode({'claims': [{'text': 'A', 'status': 'supported', 'snippet_ids': []}], 'unmet_aspects': []}, plan, parts, snippets)
    snippets = [{'snippet_id': f'S{i}', 'citation_id': f'c{i}', 'quote': 'fact'} for i in range(33)]
    rows = [{'text': 'Fact.', 'status': 'supported', 'snippet_ids': [f'S{i}' for i in range(n, min(n+7, 33))]} for n in range(0, 33, 7)]
    with pytest.raises(ValueError):
        decode({'claims': rows, 'unmet_aspects': []}, plan, parts, snippets)
    rows = [{'text': 'Fact.', 'status': 'supported', 'snippet_ids': [f'S{i}' for i in range(n, n+8)]}
            for n in range(0, 32, 8)]
    valid, _, _ = decode({'claims': rows, 'unmet_aspects': []}, plan, parts, snippets)
    assert len(valid['answers'][0]['snippet_ids']) == 32
    rows[0]['snippet_ids'].append('S32')
    with pytest.raises(ValueError):
        decode({'claims': rows, 'unmet_aspects': []}, plan, parts, snippets)


def test_v14_workflow_warning_survives_correction_and_export(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[1]['content'])
            if set(payload) == {'operations'}:
                data = {'R1': [payload['operations'][0]['request']]}
            else:
                sid = payload['snippets'][0]['snippet_id']
                sid2 = payload['snippets'][1]['snippet_id']
                cap = payload['answer_part_text_limits']['R1P1']
                data = {'R1': {'R1P1': {'claims': [
                    {'status': 'supported', 'text': 'x' * cap, 'snippet_ids': [sid]},
                    {'status': 'supported', 'text': 'A second fact.', 'snippet_ids': [sid2]}], 'unmet_aspects': []}}}
            return {'choices': [{'message': {'content': json.dumps(data)}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'; folder.mkdir()
        (folder / 'a.md').write_text('# A\nA useful source fact.\n')
        (folder / 'b.md').write_text('# B\nAnother useful source fact.\n')
        cid = await register_collection('w', 'docs', folder)
        await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,))
        item = await inv.create_investigation(scope, 'Explain A', 'useful', 'model',
            template_version='investigation-v14', reviewed_operations=[{'request': 'Explain A', 'operation': 'answer'}])
        assert item['brief']['answers'][0]['status'] == 'incomplete'
        assert item['provenance']['quantity_parser_version'] == 'v4'
        corrected = deepcopy(item['brief']); corrected['model_uncertainties'] = []
        corrected['answers'][0]['text'] = 'A reviewed answer.'
        corrected['answers'][0]['status'] = 'supported'
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'owner', 'review', corrected_brief=corrected)
        assert reviewed['review']['corrected_brief']['model_uncertainties'] == [claims.WARNING]
        assert reviewed['review']['corrected_brief']['answers'][0]['status'] == 'supported'
        assert 'parts' not in reviewed['review']['corrected_brief']['answers'][0]
        assert reviewed['brief']['answers'][0]['parts'][0]['claims'][0]['text'] == 'x' * 300
        from potluck_local.routers.investigations import export
        result = await export(item['id'], 'w', [cid])
        assert 'Original model output integrity warning:' in result['markdown']
        assert 'completeness and entailment are not verified' in result['markdown']
        assert 'Original machine claims (unverified)' in result['markdown']
        assert 'R1P1C1' in result['markdown'] and 'R1P1C2' in result['markdown']
        assert 'A reviewed answer' in result['markdown']
        assert 'separate from any owner correction' in result['markdown']
    asyncio.run(run())

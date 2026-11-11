import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from potluck_local.services import investigation_reviewed_claim_selection_v3 as v3
from potluck_local.services import investigation_reviewed_evidence as evidence_service


def evidence(text='Only approved releases may proceed:\n- Use a signed build.\n  - Keep the attestation.\n- Keep the rollback record.\n\nUnrelated marketing notes.'):
    return [{'kind': 'reviewed_claim', 'citation_id': 'claim:one', 'content': text,
             'sources': [{'citation_id': 'source:one:revision', 'content': text}]}]


def test_whole_nested_item_and_governing_context_are_automatic():
    records = evidence(); prepared = v3.prepare('Which build?', records)
    unit = next(u for u in prepared['units'] if 'Use a signed build.' in u['text'])
    assert 'Keep the attestation.' in unit['text']
    result = v3.decode(json.dumps({'status': 'supported', 'answer': 'The selected rule applies with its conditions.', 'unit_ids': [unit['id']]}), prepared)
    assert len(result['units']) == 4
    assert result['units'][0]['text'].strip() == 'Only approved releases may proceed:'
    assert result['included_context_unit_ids'] == ['U1', 'U3', 'U4']
    assert all(records[0]['content'][u['start']:u['end']] == u['text'] for u in result['units'])
    # Unlabelled prose may govern the section and stays conservatively attached.
    assert 'marketing' in '\n'.join(u['text'] for u in result['units'])


def test_separate_exception_cannot_be_selected_away():
    prepared = v3.prepare('May we deploy?', evidence('Deploy after approval.\n\nUnless the incident is open, in which case deployment must wait.\n\nUnrelated note.'))
    assert len(prepared['units']) == 1
    selected = v3.decode('{"status":"supported","answer":"The rule and its exception apply.","unit_ids":["U1"]}', prepared)
    assert 'Unless the incident is open' in selected['units'][0]['text']


@pytest.mark.parametrize('text', [
    'For approved members:\n\nBooking is allowed.',
    'Booking is allowed.\n\nThis permission excludes suspended members.',
    'Booking is allowed.\n\nNote: suspended members are excluded.',
    'Booking is allowed.\n\nThe exception is a suspended membership.',
    'For approved members:\n- Booking is allowed.\n\nThis permission excludes suspended members.',
])
def test_unlabelled_governing_prose_cannot_be_removed(text):
    prepared = v3.prepare('May we book?', evidence(text))
    for unit in prepared['units']:
        selected = v3.decode(json.dumps({'status': 'supported', 'answer': 'The selected rule applies with its conditions.', 'unit_ids': [unit['id']]}), prepared)
        assert ''.join(u['text'] for u in selected['units']).strip() == text.strip()


def test_subheadings_retain_ancestor_conditions_and_siblings_reset():
    text = '# Policy\n\nOnly approved members may book.\n\n## Cancellation\n\nCancellation is allowed.\n\n## Payments\n\nPayments are logged.'
    prepared = v3.prepare('May we cancel?', evidence(text))
    target = next(u for u in prepared['units'] if 'Cancellation is allowed.' in u['text'])
    selected = v3.decode(json.dumps({'status': 'supported', 'answer': 'The selected rule applies with its conditions.', 'unit_ids': [target['id']]}), prepared)
    shown = ''.join(u['text'] for u in selected['units'])
    assert '# Policy' in shown and 'Only approved members may book.' in shown
    assert '## Cancellation' in shown and 'Payments' not in shown


def test_long_prose_code_and_tables_are_never_character_clipped():
    long = 'A complete sentence with a condition. ' * 40
    table = '| Rule | Exception |\n| --- | --- |\n| Allow | Only with approval |\n'
    code = 'Configuration:\n```python\nx = 1\n' + '# preserved\n' * 100 + '```\n'
    for text in [long, table, code]:
        prepared = v3.prepare('What is stated?', evidence(text))
        ids = [u['id'] for u in prepared['units']]
        selected = v3.decode(json.dumps({'status': 'supported', 'answer': 'The selected rule applies with its conditions.', 'unit_ids': ids}), prepared)
        assert ''.join(u['text'] for u in selected['units']).strip() == text.strip()
    with pytest.raises(ValueError, match='Incomplete reviewed code'):
        v3.prepare('Code?', evidence('```python\nx = 1'))


@pytest.mark.parametrize('raw', [
    '{"answer":"A draft based on selected evidence.","status":"supported","unit_ids":[]}',
    '{"answer":"A draft based on selected evidence.","status":"supported","unit_ids":["U999"]}',
    '{"answer":"A draft based on selected evidence.","status":"supported","unit_ids":["U1","U1"]}',
    '{"answer":"A draft based on selected evidence.","status":"conflicting","unit_ids":[]}',
    '{"answer":"A draft based on selected evidence.","status":"supported","unit_ids":["U1"],"text":"invented"}',
    '{"answer":"A draft based on selected evidence.","status":"supported","status":"unknown","unit_ids":["U1"]}',
])
def test_untrusted_or_duplicate_selections_fail(raw):
    with pytest.raises(ValueError):
        v3.decode(raw, v3.prepare('Rule?', evidence()))


@pytest.mark.parametrize('answer,status,ids', [
    ('x' * 1801, 'supported', ['U1']),
    (' ', 'supported', ['U1']),
    ('Unsupported detail without a source.', 'unknown', []),
])
def test_draft_is_bounded_and_requires_selected_evidence(answer, status, ids):
    with pytest.raises(ValueError):
        v3.decode(json.dumps({'status': status, 'unit_ids': ids, 'answer': answer}), v3.prepare('Rule?', evidence()))


def test_explicit_unknown_conflict_and_authoritative_replay():
    records = evidence('Default 14 days.\n\nDefault seven days in the other section.')
    snippets, _ = evidence_service.snippets(records)
    operation = {'id': 'R1', 'request': 'Which default?'}
    raw = '{"status":"conflicting","answer":"The selected statements conflict: 14 days and seven days. Neither establishes a winner.","unit_ids":["U1"]}'
    brief, trace = v3.make_brief(operation['request'], operation, records, snippets, raw)
    assert 'conflict' in brief['answers'][0]['text']
    assert brief['answers'][0]['text'].index('14 days') < brief['answers'][0]['text'].index('seven days')
    assert brief['answers'][0]['passage_presentation']['passages'][0]['text'].startswith('Default 14')
    prepared = v3.prepare(operation['request'], records)
    digest = lambda x: hashlib.sha256(json.dumps(x, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    visible = {'model_visible_claim_ids': ['C1'], 'support_visibility': 'Supporting source excerpts are not in the model prompt.',
               'messages_sha256': digest(v3.messages(prepared)), 'schema_sha256': digest(v3.response_schema(prepared))}
    item = {'issue': operation['request'], 'evidence': records, 'brief': brief,
            'provenance': {'reviewed_claim_selection': trace, 'reviewed_claim_selection_input': visible, 'operation_plan': [operation]}}
    assert v3.validate_brief(item, snippets) == brief
    for target in ['text', 'passage_presentation']:
        forged = deepcopy(item)
        forged['brief']['answers'][0][target] = 'forged'
        with pytest.raises(ValueError, match='original'):
            v3.validate_brief(forged, snippets)
    forged = deepcopy(item); forged['evidence'][0]['sources'][0]['content'] += 'changed'
    with pytest.raises(ValueError):
        v3.validate_brief(forged, evidence_service.snippets(forged['evidence'])[0])
    unknown, _ = v3.make_brief(operation['request'], operation, records, snippets, '{"status":"unknown","answer":"The requested exact fact is not stated.","unit_ids":["U1"]}')
    assert unknown['answers'][0]['text'].startswith('The selected reviewed statements do not specify the requested fact.')
    empty, _ = v3.make_brief(operation['request'], operation, records, snippets, '{"status":"unknown","answer":"","unit_ids":[]}')
    assert empty['answers'][0]['source_excerpts'] == []


def test_real_service_get_correction_and_revocation(tmp_path, monkeypatch):
    from potluck_local.services import investigations as inv
    from potluck_local.services import investigation_verification as verification
    from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def validate_investigation_profile(self, *args, **kwargs): return None
        async def measure_investigation_prompt(self, messages, **kwargs): return {'input_tokens': 123, 'context_tokens': 8192}
        async def chat(self, messages, **kwargs):
            Runtime.actual = (deepcopy(messages), deepcopy(kwargs['response_format']['schema']))
            return {'choices': [{'message': {'content': '{"status":"supported","answer":"Only approved rollout releases may proceed. Use a signed build.","unit_ids":["U2"]}'}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    async def run():
        await init_memory_db(); folder = tmp_path / 'docs'; folder.mkdir()
        body = 'Only approved rollout releases may proceed:\n- Use a signed build.\n- Keep the rollback record.'
        source_file = folder / 'source.md'; source_file.write_text('# Rollout\n\n' + body)
        cid = await register_collection('w', 'docs', folder); await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,))
        result = await lookup_evidence(scope, 'rollout', 10, retrieval_version='v2')
        refs = [{k: result['source_chunks'][0][k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')}]
        claim = await propose_claim('w', (cid,), 'rollout', body, refs); await approve_claim('w', (cid,), claim, 'owner', 'checked')
        item = await inv.create_investigation(scope, 'Which rollout build?', 'rollout', 'model', template_version='investigation-v21',
            reviewed_operations=[{'request': 'Which rollout build?', 'operation': 'answer'}], evidence_selection='reviewed-current-v1',
            presentation_mode=v3.VERSION, generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1', reliability_mode='bounded-evidence-v1')
        assert 'Only approved rollout' in item['brief']['answers'][0]['text']
        assert 'rollback' not in item['brief']['answers'][0]['text']
        assert item['evidence'][0]['content'] == body
        assert (await inv.get_investigation(scope, item['id']))['brief'] == item['brief']
        with pytest.raises(ValueError, match='Automatic verification is unsupported'):
            verification.original_input(item)
        corrected = deepcopy(item['brief'])
        corrected['answers'][0]['text'] = 'Owner correction: use a signed build for an approved rollout.'
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'owner', 'reviewed', corrected_brief=corrected)
        assert reviewed['brief'] == item['brief']
        assert reviewed['review']['corrected_brief']['answers'][0]['text'] == corrected['answers'][0]['text']
        source_file.write_text('# Rollout\n\nThe source changed.'); await scan_collection('w', cid)
        with pytest.raises((ValueError, LookupError)):
            await inv.get_investigation(scope, item['id'])
    asyncio.run(run())

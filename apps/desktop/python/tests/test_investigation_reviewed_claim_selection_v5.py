import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_reviewed_claim_selection_v5 as v5
from potluck_local.services import investigation_reviewed_evidence as es


def evidence(text):
    return [{'kind': 'reviewed_claim', 'citation_id': 'claim:one', 'content': text,
             'sources': [{'citation_id': 'source:one:revision', 'content': text}]}]

def raw(ids, status='supported', quote=''):
    return json.dumps({'unit_ids': ids, 'status': status, 'missing_request_quote': quote})

def test_schema_and_request_bound_unknown():
    p = v5.prepare('What time and zone?', evidence('A period of five days applies.'))
    assert list(v5.response_schema(p)['properties']) == ['unit_ids', 'status', 'missing_request_quote']
    assert v5.decode(raw(['U1'], 'unknown', 'time and zone'), p)['missing_request_quote'] == 'time and zone'
    for x in [raw(['U1'], 'unknown', ''), raw(['U1'], 'unknown', 'invented'), raw(['U1'], 'supported', 'time'), '{"unit_ids":["U1"],"status":"supported","missing_request_quote":"","answer":"invented"}', '{"unit_ids":["U1"],"status":"supported","status":"unknown","missing_request_quote":""}']:
        with pytest.raises(ValueError): v5.decode(x, p)

def test_dependency_and_exact_render():
    records = evidence('1. Confirm approval.\n2. Apply change.\n3. Record completion.\n\nUnless suspended, in which case stop.')
    p = v5.prepare('How?', records)
    target = next(u['id'] for u in p['units'] if 'Record completion' in u['text'])
    selected = v5.decode(raw([target]), p)
    assert 'Confirm approval' in ''.join(u['text'] for u in selected['units'])
    assert 'Unless suspended' in ''.join(u['text'] for u in selected['units'])
    snippets, _ = es.snippets(records)
    brief, _ = v5.make_brief('How?', {'id':'q', 'request':'How?'}, records, snippets, raw([target]))
    assert all(u['text'].strip() in brief['answers'][0]['text'] for u in selected['units'])
    broken = deepcopy(snippets); broken[0]['quote'] += 'tamper'
    with pytest.raises(ValueError): v5.make_brief('How?', {'id':'q','request':'How?'}, records, broken, raw([target]))
    with pytest.raises(ValueError): v5.make_brief('How?', {'id':'q','request':'How?'}, records, snippets + snippets, raw([target]))

def test_table_and_bounds():
    unit = {'kind':'table','text':r'| `a|b` | c\|d | final |'}
    assert v5._render_unit(unit) == r'`a|b`: c\|d | final'
    table = '| A | B |\n| - | - |\n| 1 | 2 |'
    assert v5._render_unit({'kind':'table','text':table}) == table
    p = v5.prepare('Q?', evidence('A rule.'))
    for value in [raw(['U1','U1']), raw(['U999']), raw(['U1'], 'unknown', 'x'*401), 'x'*60001]:
        with pytest.raises(ValueError): v5.decode(value,p)

def test_large_unit_keeps_all_citations():
    records = evidence('\n\n'.join('Paragraph %d.' % i for i in range(18)))
    snippets, _ = es.snippets(records)
    brief, _ = v5.make_brief('Q?', {'id':'q','request':'Q?'}, records, snippets, raw(['U1']))
    assert len(brief['answers'][0]['snippet_ids']) == 18

def test_correction_preserves_presentation():
    records = evidence('A complete rule.')
    snippets, _ = es.snippets(records)
    brief, _ = v5.make_brief('Q?', {'id':'q','request':'Q?'}, records, snippets, raw(['U1']))
    modified = deepcopy(brief); modified['answers'][0]['passage_presentation']['answer'] = 'tampered'
    with pytest.raises(ValueError, match='cannot be edited'): v5.corrected_brief(modified, brief, snippets)

def test_authoritative_replay_and_tamper():
    import hashlib
    records = evidence('Default 14 days.\n\nDefault seven days in another section.')
    snippets, _ = es.snippets(records)
    op = {'id':'q','request':'Which default?'}
    reply = raw(['U1'], 'conflicting')
    brief, trace = v5.make_brief(op['request'], op, records, snippets, reply)
    assert 'no authoritative answer' in brief['answers'][0]['text']
    p = v5.prepare(op['request'], records)
    digest = lambda x: hashlib.sha256(json.dumps(x, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    visible = {'model_visible_claim_ids':['C1'], 'support_visibility':'Supporting source excerpts are not in the model prompt.',
               'messages_sha256':digest(v5.messages(p)), 'schema_sha256':digest(v5.response_schema(p))}
    item = {'issue':op['request'], 'evidence':records, 'brief':brief, 'provenance':{
        'reviewed_claim_selection':trace, 'reviewed_claim_selection_input':visible, 'operation_plan':[op]}}
    assert v5.validate_brief(item, snippets) == brief
    forged = deepcopy(item); forged['brief']['answers'][0]['text'] = 'forged'
    with pytest.raises(ValueError): v5.validate_brief(forged, snippets)
    forged = deepcopy(item); forged['evidence'][0]['sources'][0]['content'] += 'changed'
    with pytest.raises(ValueError): v5.validate_brief(forged, es.snippets(forged['evidence'])[0])


def test_real_service_source_render_get_review_export_and_revocation(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.routers import investigations as routes
    from potluck_local.services import investigation_verification as verification
    from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))

    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def validate_investigation_profile(self, *args, **kwargs): return None
        async def measure_investigation_prompt(self, messages, **kwargs):
            return {'input_tokens': 123, 'context_tokens': 8192}
        async def chat(self, messages, **kwargs):
            assert list(kwargs['response_format']['schema']['properties']) == ['unit_ids', 'status', 'missing_request_quote']
            return {'choices': [{'message': {'content': raw(['U2'])}}]}

    monkeypatch.setattr(inv, 'get_runtime', Runtime)

    async def run():
        await init_memory_db()
        folder = tmp_path / 'docs'; folder.mkdir()
        body = 'Only approved rollout releases may proceed:\n- Use a signed build.\n- Keep the rollback record.\n\nUnless the incident remains open, in which case wait.'
        source = folder / 'source.md'; source.write_text('# Rollout\n\n' + body)
        collection = await register_collection('w', 'docs', folder)
        await scan_collection('w', collection)
        scope = EvidenceScope('w', (collection,))
        found = await lookup_evidence(scope, 'rollout', 10, retrieval_version='v2')
        refs = [{k: found['source_chunks'][0][k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')}]
        claim = await propose_claim('w', (collection,), 'rollout', body, refs)
        await approve_claim('w', (collection,), claim, 'test-reviewer', 'fixture source checked')
        item = await inv.create_investigation(scope, 'Which rollout build?', 'rollout', 'model',
            template_version='investigation-v21', reviewed_operations=[{'request': 'Which rollout build?', 'operation': 'answer'}],
            evidence_selection='reviewed-current-v1', presentation_mode=v5.VERSION,
            generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1',
            reliability_mode='bounded-evidence-v1')
        text = item['brief']['answers'][0]['text']
        assert all(value in text for value in ('Only approved', 'signed build', 'rollback record', 'incident remains open'))
        assert item['brief']['answers'][0]['passage_presentation']['version'] == v5.VERSION
        assert 'rendered from source' in item['provenance']['factual_generation']
        assert (await inv.get_investigation(scope, item['id']))['brief'] == item['brief']
        exported = (await routes.export(item['id'], scope.workspace, list(scope.collection_ids)))['markdown']
        assert 'Presented reviewed claim IDs: C1' in exported and 'incident remains open' in exported
        with pytest.raises(ValueError, match='Automatic verification is unsupported'):
            verification.original_input(item)
        forged = deepcopy(item['brief']); forged['answers'][0]['source_excerpts'][0]['quote'] += ' forged'
        with pytest.raises(inv.BriefValidationError):
            await inv.review_investigation(scope, item['id'], 'correct', 'test-reviewer', 'tamper test', corrected_brief=forged)
        assert (await inv.get_investigation(scope, item['id']))['review'] is None
        corrected = deepcopy(item['brief'])
        corrected['answers'][0]['text'] = 'Use a signed build after approval; wait while the incident remains open.'
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'test-reviewer', 'fixture correction', corrected_brief=corrected)
        assert reviewed['brief'] == item['brief']
        reread = await inv.get_investigation(scope, item['id'])
        assert reread['review']['corrected_brief']['answers'][0]['text'] == corrected['answers'][0]['text']
        assert 'passage_presentation' not in reread['review']['corrected_brief']['answers'][0]
        exported = (await routes.export(item['id'], scope.workspace, list(scope.collection_ids)))['markdown']
        assert 'The sections above use the reviewed correction. The original draft is retained in Potluck.' in exported
        source.write_text('# Rollout\n\nA changed source.'); await scan_collection('w', collection)
        with pytest.raises((ValueError, LookupError)):
            await inv.get_investigation(scope, item['id'])
    asyncio.run(run())


def test_unknown_keeps_nearby_rule_and_scopes_absence():
    records = evidence('The reminder is scheduled five days after closing.\n\nNo exact clock time is stated.')
    snippets, _ = es.snippets(records)
    request = 'What clock time and time zone apply?'
    brief, _ = v5.make_brief(request, {'id': 'R1', 'request': request}, records, snippets,
                             raw(['U1'], 'unknown', 'clock time and time zone'))
    text = brief['answers'][0]['text']
    assert text.startswith('The selected reviewed statements do not specify: clock time and time zone')
    assert 'five days after closing' in text
    with pytest.raises(ValueError):
        v5.make_brief(request, {'id': 'R1', 'request': request}, records, snippets,
                      raw(['U1'], 'unknown', 'no reminders exist'))


def test_unit_and_visible_budgets_fail_without_clipping(monkeypatch):
    records = evidence('\n\n'.join('**Rule %d.** Complete condition.' % i for i in range(17)))
    prepared = v5.prepare('Which rules?', records)
    assert len(prepared['units']) == 17
    with pytest.raises(ValueError, match='Invalid reviewed passage selection'):
        v5.decode(raw([u['id'] for u in prepared['units']]), prepared)
    records = evidence('A complete long rule. ' * 100)
    snippets, _ = es.snippets(records)
    monkeypatch.setattr(v5, 'MAX_VISIBLE_CHARS', 100)
    with pytest.raises(ValueError, match='display budget exceeded'):
        v5.make_brief('Q?', {'id': 'R1', 'request': 'Q?'}, records, snippets, raw(['U1']))

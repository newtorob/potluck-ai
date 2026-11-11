import json
from copy import deepcopy

import jsonschema
import pytest

from potluck_local.services import investigation_answer_components as v1
from potluck_local.services import investigation_answer_components_v2 as v2
from potluck_local.services import investigation_answer_components_v3 as v3
from potluck_local.services import investigation_answer_presentation as presentation
from potluck_local.services import investigation_aspect_source_answer_v20 as base
from potluck_local.services import investigation_unknown_explanations as unknown
from potluck_local.services import investigation_structural_answer as structural

PLAN = [{'id': 'R1', 'operation': 'answer', 'request': 'What is the policy?'}]
SNIPPETS = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Managers require prior approval.'}]


def schema():
    value = base.response_schema(PLAN, SNIPPETS, [])
    value = unknown.response_schema(value, PLAN, version='cited-unknown-v4')
    return presentation.response_schema(value, PLAN, version=v3.VERSION)


def payload():
    return {'R1': {'supported': [{'focus': 'Approval', 'snippet_ids': ['S1'], 'conditions': [{'snippet_ids': ['S1'], 'text': 'Approval is required before work.'}], 'exceptions': [], 'conclusion': 'Managers require approval.'}], 'unknown': [], 'conflicting': [], 'more': False}}


def test_runtime_schema_admission_and_full_qualification():
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    fmt = {'type': 'json_object', 'schema': schema()}
    assert _structured_response_format(object.__new__(SplitModel), fmt) == fmt
    jsonschema.validate(payload(), schema())
    brief, _, coverage = structural.decode_results(json.dumps(payload()), PLAN, SNIPPETS, [], [], structural_trace={'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v3.VERSION)
    assert 'Approval is required before work.' in brief['answers'][0]['text']
    structural.validate_original(brief, coverage, SNIPPETS, {'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v3.VERSION)


def test_mixed_status_roundtrip_and_replay():
    value = payload(); row = value['R1']
    row['unknown'].append({'focus': 'Date', 'snippet_ids': ['S1'], 'distinctions': [], 'unknown_reason': 'The date is not stated.'})
    row['conflicting'].append({'focus': 'Conflict', 'snippet_ids': ['S1'], 'conflict_reason': 'The sources disagree.'})
    jsonschema.validate(value, schema())
    raw, original = v3.extract(json.dumps(value), PLAN, SNIPPETS)
    groups = json.loads(raw)['R1']['groups']
    assert [g['status'] for g in groups] == ['supported', 'unknown', 'conflicting']
    assert original['R1'][0]['primary_conclusion'] == 'Managers require approval.'
    brief, _, coverage = structural.decode_results(json.dumps(value), PLAN, SNIPPETS, [], [], structural_trace={'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v3.VERSION)
    assert 'The sources disagree.' in brief['answers'][0]['text']
    assert coverage['cited_conclusions']['operations']['R1'][2]['conflict_reason'] == 'The sources disagree.'
    structural.validate_original(brief, coverage, SNIPPETS, {'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v3.VERSION)


@pytest.mark.parametrize('mutate', [
    lambda p: p['R1'].pop('unknown'),
    lambda p: p['R1'].update(extra=[]),
    lambda p: p['R1']['supported'][0].update(distinctions=[]),
    lambda p: p['R1']['unknown'].append({'focus':'A', 'snippet_ids':['S1'], 'distinctions':[], 'unknown_reason':'Unknown.', 'conditions':[]}),
    lambda p: p['R1']['conflicting'].append({'focus':'A', 'snippet_ids':['S1'], 'conflict_reason':'Sources disagree.', 'conclusion':''}),
    lambda p: p['R1'].update(supported=[]),
])
def test_invalid_or_extra_bucket_fields_rejected(mutate):
    value = payload(); mutate(value)
    with pytest.raises(ValueError): v3.extract(json.dumps(value), PLAN, SNIPPETS)


def test_too_many_total_groups_rejected():
    value = payload(); value['R1']['unknown'] = [{'focus': str(i), 'snippet_ids':['S1'], 'distinctions':[], 'unknown_reason':'Unknown.'} for i in range(6)]
    with pytest.raises(ValueError, match='total group'): v3.extract(json.dumps(value), PLAN, SNIPPETS)


def test_prior_modes_keep_their_schema_and_instructions():
    raw = unknown.response_schema(base.response_schema(PLAN, SNIPPETS, []), PLAN, version='cited-unknown-v4')
    for module in (v1, v2):
        assert presentation.response_schema(raw, PLAN, version=module.VERSION) == module.response_schema(raw, PLAN)
        assert presentation.instruction_for(module.VERSION) == module.INSTRUCTION


@pytest.mark.parametrize('status', ['supported', 'unknown', 'conflicting'])
def test_reviewed_service_correction_and_verification_roundtrip(tmp_path, monkeypatch, status):
    import asyncio
    from types import SimpleNamespace
    from potluck_local.services import investigations as inv
    from potluck_local.services import investigation_verification as verification
    from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.source_collections import register_collection, scan_collection
    from potluck_local.services.reviewed_knowledge import propose_claim, approve_claim

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'collection.sqlite'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    async def run():
        await init_memory_db(); folder = tmp_path / 'docs'; folder.mkdir()
        (folder / 'policy.md').write_text('# Policy\nManagers require prior approval before work.\n')
        cid = await register_collection('w', 'test', folder); await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,)); lookup = await lookup_evidence(scope, 'approval', 10, retrieval_version='v2')
        src = lookup['source_chunks'][0]; ref = {k:src[k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')}
        claim = await propose_claim('w', scope.collection_ids, 'approval.rule', 'Managers require prior approval before work.', [ref])
        await approve_claim('w', scope.collection_ids, claim, 'test-reviewer', 'Test only')
        class Runtime:
            def current_model_id(self): return 'model'
            def current_model_sha256(self): return 'a' * 64
            async def validate_investigation_profile(self, *args, **kwargs): return None
            async def measure_investigation_prompt(self, *args, **kwargs): return {'input_tokens':1000, 'context_tokens':8192}
            async def chat(self, messages, **kwargs):
                prompt = json.loads(messages[1]['content'])
                sid = prompt['snippets'][0]['snippet_id']
                row = {'supported': [], 'unknown': [], 'conflicting': [], 'more': False}
                group = {'focus': 'Approval', 'snippet_ids': [sid]}
                group.update({'conditions': [], 'exceptions': [], 'conclusion': 'Managers require prior approval before work.'} if status == 'supported' else {'distinctions': [], 'unknown_reason': 'The approval time is not stated.'} if status == 'unknown' else {'conflict_reason': 'The supplied statements disagree on approval.'})
                row[status].append(group)
                return {'choices': [{'message': {'content': json.dumps({'R1': row})}}]}
        monkeypatch.setattr(inv, 'get_runtime', Runtime)
        item = await inv.create_investigation(scope, 'What approval applies?', 'approval', 'model', template_version='investigation-v21', generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1', reviewed_operations=[{'request':'What approval applies?', 'operation':'answer'}], reliability_mode='bounded-evidence-v1', unknown_mode='cited-unknown-v4', presentation_mode=v3.VERSION, evidence_selection='reviewed-collection-v2')
        assert item['brief']['answers'][0]['status'] == status
        for version in ('draft-verification-v1','draft-verification-v2','draft-verification-v3'):
            verification.original_input(item, version=version)
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'reviewer', 'Test correction', corrected_brief=deepcopy(item['brief']))
        assert reviewed['review']['corrected_brief']['answers'][0]['text'] == item['brief']['answers'][0]['text']
        assert reviewed['review']['corrected_brief']['answers'][0]['source_excerpts'] == item['brief']['answers'][0]['source_excerpts']
        assert (await inv.get_investigation(scope, item['id']))['brief'] == item['brief']
    asyncio.run(run())


@pytest.mark.parametrize('bad', [
    {'focus': 'Conflict', 'snippet_ids': ['S1']},
    {'focus': 'Conflict', 'snippet_ids': [], 'conflict_reason': 'Sources disagree.'},
    {'focus': 'Conflict', 'snippet_ids': ['S1'], 'conflict_reason': 'unfinished'},
    {'focus': 'Conflict', 'snippet_ids': ['S1'], 'conflict_reason': 'X' * 160 + '.'},
    {'focus': 'Conflict', 'snippet_ids': ['S1'], 'conflict_reason': 'Sources disagree [S1].'},
])
def test_invalid_conflict_reason_fails_closed(bad):
    value = payload(); value['R1']['conflicting'] = [bad]
    with pytest.raises(ValueError): v3.extract(json.dumps(value), PLAN, SNIPPETS)


def test_conflict_reason_for_other_status_is_rejected():
    value = payload(); value['R1']['supported'][0]['conflict_reason'] = 'Sources disagree.'
    with pytest.raises(ValueError): v3.extract(json.dumps(value), PLAN, SNIPPETS)


def test_stored_conflict_reason_tampering_rejected():
    value = payload(); value['R1']['supported'] = []
    value['R1']['conflicting'] = [{'focus': 'Approval', 'snippet_ids': ['S1'], 'conflict_reason': 'The sources disagree on approval.'}]
    brief, _, coverage = structural.decode_results(json.dumps(value), PLAN, SNIPPETS, [], [], structural_trace={'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v3.VERSION)
    coverage['cited_conclusions']['operations']['R1'][0]['conflict_reason'] = 'The sources disagree on time.'
    with pytest.raises(ValueError):
        structural.validate_original(brief, coverage, SNIPPETS, {'blocks': []}, unknown_mode='cited-unknown-v4', presentation_mode=v3.VERSION)

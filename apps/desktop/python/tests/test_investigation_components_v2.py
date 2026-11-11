from copy import deepcopy
import json

import pytest

from potluck_local.services import investigation_answer_presentation as presentation
from potluck_local.services import investigation_answer_components_v2 as v2
from potluck_local.services import investigation_revision as revision
from potluck_local.services import investigation_verification, investigations
from test_investigation_answer_components import PLAN, SNIPPETS, TRACE, component, group, decode
from test_investigation_verification import setup

MODE = {'unknown_mode': 'cited-unknown-v4', 'presentation_mode': v2.VERSION}


def long_group():
    value = group()
    value['conclusion'] = 'A' * 150 + '.'
    value['exceptions'] = [component('B' * 150 + '.', 'S2')]
    return value


def test_long_supported_is_opt_in_and_replays():
    value = long_group()
    with pytest.raises(ValueError): decode(deepcopy(value))
    brief, _, coverage = decode(deepcopy(value), mode=MODE)
    from potluck_local.services import investigation_structural_answer as structural
    structural.validate_original(brief, coverage, SNIPPETS, TRACE, **MODE)
    assert len(coverage['cited_conclusions']['operations']['R1'][0]['text']) > 300
    coverage['cited_conclusions']['operations']['R1'][0]['components']['exceptions'][0]['text'] = 'Different.'
    with pytest.raises(ValueError): structural.validate_original(brief, coverage, SNIPPETS, TRACE, **MODE)


def test_supported_limit_and_unknown_limit_are_separate():
    assert len(presentation._conclusion('x' * 899 + '.', 'supported', version=v2.VERSION)) == 900
    with pytest.raises(ValueError): presentation._conclusion('x' * 900 + '.', 'supported', version=v2.VERSION)
    value = dict(group(), status='unknown', snippet_ids=['S3'], exceptions=[], conclusion='',
                 unknown_reason='A' * 150 + '.', distinctions=[component('B' * 150 + '.', 'S3')])
    with pytest.raises(ValueError, match='300-character'): decode(value, mode=MODE)


def test_native_schema_admission_and_v1_prompt_unchanged():
    from potluck_local.services import investigation_aspect_source_answer_v20 as base
    from potluck_local.services import investigation_unknown_explanations as unknowns
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    schema = unknowns.response_schema(base.response_schema(PLAN, SNIPPETS, []), PLAN)
    response = {'type': 'json_object', 'schema': presentation.response_schema(schema, PLAN, version=v2.VERSION)}
    assert _structured_response_format(SplitModel.__new__(SplitModel), response) == response
    assert 'fit 300 characters' in presentation.instruction_for(presentation.COMPONENT_VERSION)
    assert 'fit 900 characters' in presentation.instruction_for(v2.VERSION)


@pytest.mark.asyncio
async def test_real_service_persist_replay_export_and_revision(setup):
    runtime, scope, _, _ = await setup()
    # Build a new original with the same actual source collection and v2 selected.
    async def chat(messages, **kwargs):
        data = json.loads(messages[1]['content'])
        ids = [s['snippet_id'] for s in data['snippets'] if 'Managers' in s['text']]
        answer = {'focus': 'Requirement', 'status': 'supported', 'snippet_ids': ids,
                  'conditions': [{'text': 'C' * 150 + '.', 'snippet_ids': ids}],
                  'exceptions': [{'text': 'E' * 150 + '.', 'snippet_ids': ids}],
                  'distinctions': [], 'conclusion': 'Receipts are required.', 'unknown_reason': ''}
        runtime.calls.append((messages, kwargs))
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'R1': {'groups': [answer], 'more': False}})}}]}
    runtime.chat = chat
    item = await investigations.create_investigation(scope, 'What is required for reimbursement?', 'reimbursement', 'model',
        template_version='investigation-v21', reviewed_operations=[{'request': 'What is required for reimbursement?', 'operation': 'answer'}],
        generation_profile=investigation_verification.PROFILE, prompt_encoding='compact-calculation-v1',
        reliability_mode='bounded-evidence-v1', unknown_mode='cited-unknown-v4', presentation_mode=v2.VERSION)
    stored = await investigations.get_investigation(scope, item['id'])
    assert stored == item
    assert revision.prepare_original(stored)
    from potluck_local.routers.investigations import export
    assert (await export(item['id'], scope.workspace, list(scope.collection_ids)))['markdown']
    sent = json.loads(runtime.calls[0][0][1]['content'])
    assert sent['answer_component_budget']['max_combined_characters_including_labels'] == 900
    assert sent['answer_component_budget']['max_unknown_combined_characters_including_labels'] == 300

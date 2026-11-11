import asyncio
from copy import deepcopy
import json

import pytest
from potluck_local.services import investigation_structural_answer as answer
from potluck_local.services import investigation_unknown_explanations as unknowns
from potluck_local.services import investigations as inv

PLAN = [{'id': 'R1', 'request': 'Which release first shipped this protocol?', 'operation': 'answer'}]
SNIPPETS = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Protocol version is 1.'}]
TRACE = {'blocks': []}
REASON = 'Protocol version 1 identifies the protocol, not the release that first shipped it.'


def response(status='unknown', ids=None, reason=REASON):
    return {'R1': {'groups': [{'focus': 'First release', 'status': status,
        'snippet_ids': ['S1'] if ids is None else ids, 'unknown_reason': reason}], 'more': False}}


def decode(data, snippets=SNIPPETS):
    return answer.decode_results(json.dumps(data), PLAN, snippets, [], [],
        structural_trace=TRACE, unknown_mode=unknowns.VERSION)


@pytest.mark.parametrize('ids', [[], ['S1']])
def test_unknown_is_labeled_and_related_quotes_stay_exact(ids):
    brief, _, coverage = decode(response(ids=ids))
    result = brief['answers'][0]
    assert result['status'] == 'unknown' and REASON in result['text']
    assert unknowns.LABEL in result['text'] and unknowns.SCOPE in result['text']
    record = coverage['unknown_explanations']['operations']['R1'][0]
    assert record['snippet_ids'] == ids
    assert record['source_excerpts'] == (SNIPPETS if ids else [])
    assert all(claim['text'] == SNIPPETS[0]['quote'] for claim in result['parts'][0]['claims'])
    before = deepcopy(coverage)
    answer.validate_original(brief, coverage, SNIPPETS, TRACE, unknown_mode=unknowns.VERSION)
    assert coverage == before


def test_mixed_focuses_explain_only_unknown_and_keep_status():
    data = response()
    data['R1']['groups'] += [dict(focus='Wire identifier', status='supported', snippet_ids=['S1'], unknown_reason='')]
    brief, _, coverage = decode(data)
    assert brief['answers'][0]['status'] == 'unknown'
    assert len(coverage['unknown_explanations']['operations']['R1']) == 1
    for status in ['supported', 'conflicting']:
        b, _, c = decode(response(status, reason=''))
        assert b['answers'][0]['status'] == status
        assert unknowns.LABEL not in b['answers'][0]['text']
        assert c['unknown_explanations']['operations']['R1'] == []


@pytest.mark.parametrize('reason', ['', ' ', 'x'*301, None, 42, 'a\nb', 'a\tb', 'a\x00b', 'a\u2028b', 'a\u200bb', 'reason [S1]', 'reason [S1, S2]'])
def test_invalid_reason_rejected(reason):
    with pytest.raises(ValueError): decode(response(reason=reason))


@pytest.mark.parametrize('change', ['missing', 'extra', 'invalid_status', 'unsupported_ref', 'nonunknown_reason'])
def test_schema_cannot_be_bypassed(change):
    data=response(); group=data['R1']['groups'][0]
    if change == 'missing': del group['unknown_reason']
    if change == 'extra': group['extra']='untrusted'
    if change == 'invalid_status': group['status']='verified'
    if change == 'unsupported_ref': group['snippet_ids']=['S2']
    if change == 'nonunknown_reason': group['status']='supported'
    with pytest.raises(ValueError): decode(data)


def test_duplicate_json_key_rejected():
    raw=json.dumps(response()).replace('"unknown_reason":', '"unknown_reason":"duplicate", "unknown_reason":')
    with pytest.raises(ValueError):
        answer.decode_results(raw, PLAN, SNIPPETS, [], [], structural_trace=TRACE, unknown_mode=unknowns.VERSION)


@pytest.mark.parametrize('field,value', [('text','Altered reason'),('focus','Altered focus'),('snippet_ids',[]),('source_excerpts',[]),('group_index',True),('group_index',1)])
def test_original_tampering_rejected(field,value):
    brief, _, coverage = decode(response())
    coverage['unknown_explanations']['operations']['R1'][0][field]=value
    with pytest.raises(ValueError):
        answer.validate_original(brief, coverage, SNIPPETS, TRACE, unknown_mode=unknowns.VERSION)


@pytest.mark.parametrize('change', ['version', 'missing_entry', 'duplicate_entry', 'missing_operation'])
def test_original_trace_shape_tampering_rejected(change):
    brief, _, coverage=decode(response()); trace=coverage['unknown_explanations']
    if change=='version': trace['version']='future'
    if change=='missing_entry': trace['operations']['R1']=[]
    if change=='duplicate_entry': trace['operations']['R1']*=2
    if change=='missing_operation': trace['operations']={}
    with pytest.raises(ValueError):
        answer.validate_original(brief, coverage, SNIPPETS, TRACE, unknown_mode=unknowns.VERSION)


def test_schema_extension_is_opt_in_and_final_text_still_bounded(monkeypatch):
    base=answer.response_schema(PLAN,SNIPPETS,[]); before=deepcopy(base)
    schema=unknowns.response_schema(base,PLAN)
    assert base==before
    assert 'unknown_reason' not in base['properties']['R1']['properties']['groups']['items']['required']
    import jsonschema
    jsonschema.validate(response(),schema)
    monkeypatch.setattr(answer,'MAX_TEXT',300)
    with pytest.raises(ValueError,match='budget'): decode(response())


@pytest.mark.parametrize('mode,reliability', [('bad', 'bounded-evidence-v1'), (unknowns.VERSION,None)])
def test_invalid_modes_reject_before_inference(mode,reliability):
    from potluck_local.services.evidence_lookup import EvidenceScope
    with pytest.raises(ValueError,match='unknown explanation mode'):
        asyncio.run(inv.create_investigation(EvidenceScope('w',('c',)),'Question','query','model',
            template_version='investigation-v21',prompt_encoding='compact-calculation-v1',
            generation_profile='qwen3.8-27b-native-non-thinking',reliability_mode=reliability,unknown_mode=mode))


@pytest.mark.parametrize('version', unknowns.SUPPORTED_VERSIONS)
def test_each_version_replays_and_preserves_its_schema_order(version):
    schema=unknowns.response_schema(answer.response_schema(PLAN,SNIPPETS,[]),PLAN,version=version)
    order=list(schema['properties']['R1']['properties']['groups']['items']['properties'])
    assert order==(['focus','snippet_ids','status','unknown_reason'] if version==unknowns.LEGACY_VERSION else ['focus','status','unknown_reason','snippet_ids'])
    b,_,c=answer.decode_results(json.dumps(response()),PLAN,SNIPPETS,[],[],structural_trace=TRACE,unknown_mode=version)
    assert c['unknown_explanations']['version']==version
    answer.validate_original(b,c,SNIPPETS,TRACE,unknown_mode=version)


@pytest.mark.parametrize('reason', ['The document does not establish a first-', 'x'*299+'-', 'The requested value is missing'])
def test_unfinished_reasons_reject_in_v4_but_old_records_remain_readable(reason):
    with pytest.raises(ValueError,match='sentence punctuation'): decode(response(reason=reason))
    b,_,c=answer.decode_results(json.dumps(response(reason=reason)),PLAN,SNIPPETS,[],[],structural_trace=TRACE,unknown_mode=unknowns.DISTINCTION_VERSION)
    answer.validate_original(b,c,SNIPPETS,TRACE,unknown_mode=unknowns.DISTINCTION_VERSION)

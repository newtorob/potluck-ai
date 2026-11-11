import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_reviewed_claim_selection_v6 as v6
from potluck_local.services import investigation_reviewed_evidence as es


def evidence(text):
    return [{'kind': 'reviewed_claim', 'citation_id': 'claim:one', 'content': text,
             'sources': [{'citation_id': 'source:one:revision', 'content': text}]}]

def raw(ids, status='supported', quote=''):
    return json.dumps({'unit_ids': ids, 'verdict': 'unknown: ' + quote if status == 'unknown' else status})

def test_schema_and_request_bound_unknown():
    p = v6.prepare('What time and zone?', evidence('A period of five days applies.'))
    schema = v6.response_schema(p)
    assert list(schema['properties']) == ['unit_ids', 'verdict']
    assert schema['properties']['verdict'] == {'type':'string','maxLength':409}
    assert v6.decode(raw(['U1'], 'unknown', 'time and zone'), p)['missing_request_quote'] == 'time and zone'
    for x in [raw(['U1'], 'unknown', ''), raw(['U1'], 'unknown', 'invented'), '{"unit_ids":["U1"],"verdict":"unknown"}', '{"unit_ids":["U1"],"verdict":"unknown:time and zone"}', '{"unit_ids":["U1"],"verdict":"UNKNOWN: time and zone"}', '{"unit_ids":["U1"],"verdict":"supported","missing_request_quote":"time"}', '{"unit_ids":["U1"],"verdict":"supported","status":"supported"}', '{"unit_ids":["U1"],"verdict":"supported","verdict":"conflicting"}', '{"unit_ids":["U1"],"verdict":"supported: time"}']:
        with pytest.raises(ValueError): v6.decode(x, p)

def test_dependency_and_exact_render():
    records = evidence('1. Confirm approval.\n2. Apply change.\n3. Record completion.\n\nUnless suspended, in which case stop.')
    p = v6.prepare('How?', records)
    target = next(u['id'] for u in p['units'] if 'Record completion' in u['text'])
    selected = v6.decode(raw([target]), p)
    assert 'Confirm approval' in ''.join(u['text'] for u in selected['units'])
    assert 'Unless suspended' in ''.join(u['text'] for u in selected['units'])
    snippets, _ = es.snippets(records)
    brief, _ = v6.make_brief('How?', {'id':'q', 'request':'How?'}, records, snippets, raw([target]))
    assert all(u['text'].strip() in brief['answers'][0]['text'] for u in selected['units'])
    broken = deepcopy(snippets); broken[0]['quote'] += 'tamper'
    with pytest.raises(ValueError): v6.make_brief('How?', {'id':'q','request':'How?'}, records, broken, raw([target]))
    with pytest.raises(ValueError): v6.make_brief('How?', {'id':'q','request':'How?'}, records, snippets + snippets, raw([target]))

def test_table_and_bounds():
    unit = {'kind':'table','text':r'| `a|b` | c\|d | final |'}
    assert v6._render_unit(unit) == r'`a|b`: c\|d | final'
    table = '| A | B |\n| - | - |\n| 1 | 2 |'
    assert v6._render_unit({'kind':'table','text':table}) == table
    p = v6.prepare('Q?', evidence('A rule.'))
    for value in [raw(['U1','U1']), raw(['U999']), raw(['U1'], 'unknown', 'x'*401), 'x'*60001]:
        with pytest.raises(ValueError): v6.decode(value,p)

def test_large_unit_keeps_all_citations():
    records = evidence('\n\n'.join('Paragraph %d.' % i for i in range(18)))
    snippets, _ = es.snippets(records)
    brief, _ = v6.make_brief('Q?', {'id':'q','request':'Q?'}, records, snippets, raw(['U1']))
    assert len(brief['answers'][0]['snippet_ids']) == 18

def test_correction_preserves_presentation():
    records = evidence('A complete rule.')
    snippets, _ = es.snippets(records)
    brief, _ = v6.make_brief('Q?', {'id':'q','request':'Q?'}, records, snippets, raw(['U1']))
    modified = deepcopy(brief); modified['answers'][0]['passage_presentation']['answer'] = 'tampered'
    with pytest.raises(ValueError, match='cannot be edited'): v6.corrected_brief(modified, brief, snippets)

def test_authoritative_replay_and_tamper():
    import hashlib
    records = evidence('Default 14 days.\n\nDefault seven days in another section.')
    snippets, _ = es.snippets(records)
    op = {'id':'q','request':'Which default?'}
    reply = raw(['U1'], 'conflicting')
    brief, trace = v6.make_brief(op['request'], op, records, snippets, reply)
    assert 'no authoritative answer' in brief['answers'][0]['text']
    p = v6.prepare(op['request'], records)
    digest = lambda x: hashlib.sha256(json.dumps(x, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    visible = {'model_visible_claim_ids':['C1'], 'support_visibility':'Supporting source excerpts are not in the model prompt.',
               'messages_sha256':digest(v6.messages(p)), 'schema_sha256':digest(v6.response_schema(p))}
    item = {'issue':op['request'], 'evidence':records, 'brief':brief, 'provenance':{
        'reviewed_claim_selection':trace, 'reviewed_claim_selection_input':visible, 'operation_plan':[op]}}
    assert v6.validate_brief(item, snippets) == brief
    forged = deepcopy(item); forged['brief']['answers'][0]['text'] = 'forged'
    with pytest.raises(ValueError): v6.validate_brief(forged, snippets)
    forged = deepcopy(item); forged['evidence'][0]['sources'][0]['content'] += 'changed'
    with pytest.raises(ValueError): v6.validate_brief(forged, es.snippets(forged['evidence'])[0])


def test_unknown_keeps_nearby_rule_and_scopes_absence():
    records = evidence('The reminder is scheduled five days after closing.\n\nNo exact clock time is stated.')
    snippets, _ = es.snippets(records)
    request = 'What clock time and time zone apply?'
    brief, _ = v6.make_brief(request, {'id': 'R1', 'request': request}, records, snippets,
                             raw(['U1'], 'unknown', 'clock time and time zone'))
    text = brief['answers'][0]['text']
    assert text.startswith('The selected reviewed statements do not specify: clock time and time zone')
    assert 'five days after closing' in text
    with pytest.raises(ValueError):
        v6.make_brief(request, {'id': 'R1', 'request': request}, records, snippets,
                      raw(['U1'], 'unknown', 'no reminders exist'))


def test_unit_and_visible_budgets_fail_without_clipping(monkeypatch):
    records = evidence('\n\n'.join('**Rule %d.** Complete condition.' % i for i in range(17)))
    prepared = v6.prepare('Which rules?', records)
    assert len(prepared['units']) == 17
    with pytest.raises(ValueError, match='Invalid reviewed passage selection'):
        v6.decode(raw([u['id'] for u in prepared['units']]), prepared)
    records = evidence('A complete long rule. ' * 100)
    snippets, _ = es.snippets(records)
    monkeypatch.setattr(v6, 'MAX_VISIBLE_CHARS', 100)
    with pytest.raises(ValueError, match='display budget exceeded'):
        v6.make_brief('Q?', {'id': 'R1', 'request': 'Q?'}, records, snippets, raw(['U1']))

def test_real_runtime_schema_admission():
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    p = v6.prepare('What is the limit?', evidence('A sample rule.'))
    formatted = _structured_response_format(object.__new__(SplitModel), {'type':'json_object','schema':v6.response_schema(p)})
    assert formatted

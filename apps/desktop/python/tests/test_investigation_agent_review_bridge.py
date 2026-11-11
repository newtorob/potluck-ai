import copy
import json
import os
import subprocess
import sys

from potluck_local.services import investigation_agent_review_bridge as bridge
from potluck_local.services import investigation_evidence_coverage_v3 as coverage


def fixture():
    text = 'Members may use the archive. Access requires approval.'
    envelope = {'version': 'focused-revision-v6', 'scope': 'Supplied context only', 'single_focus': True,
        'group': {'id': 'G1', 'request': 'How can members use the archive?', 'focus': 'How can members use the archive?', 'status': 'supported', 'draft_sentences': []},
        'sources': [{'id': 'Q1', 'snippet_id': 'S1', 'quote': text, 'context': {'citation_id': 'source:one', 'heading': 'Archive', 'list_introduction': None, 'cited_chunk': {'quote': text, 'selected_start': 0, 'selected_end': len(text)}}}]}
    prepared = coverage.prepare(envelope)
    rows = [{'record': 'answer', 'question': prepared['selection_scope']['focus'], 'verdict': 'source_rule', 'selected_ids': [], 'rule': 'Selected units count.', 'covered_by_exact_binding_ids': []},
            {'record': 'source', 'id': 'C1', 'headings': ['Archive'], 'retained_bindings': []}]
    rows += [{'id': u['id'], 'state': 'supplied_only', 'text': u['quote']} for u in prepared['units']]
    return envelope, '\n'.join(json.dumps(x) for x in rows)


def call(value, cwd=None):
    from pathlib import Path
    package_root = str(Path(bridge.__file__).resolve().parents[2])
    return subprocess.run([sys.executable, '-m', 'potluck_local.services.investigation_agent_review_bridge'], input=value, capture_output=True, cwd=cwd, env={**os.environ, 'PYTHONPATH': package_root, 'PYTHONDONTWRITEBYTECODE': '1'})


def test_request_replays_original_coverage_and_tampering_rejected():
    envelope, packet = fixture()
    binding = bridge.prepare(envelope, packet)
    prepared = binding['prepared']
    assert bridge.request(binding) == {'messages': coverage.messages(prepared), 'response_format': {'type': 'json_object', 'schema': coverage.response_schema(prepared)}}
    altered = copy.deepcopy(binding); altered['prepared']['units'][0]['quote'] = 'Changed.'
    try:
        bridge.request(altered)
        assert False
    except ValueError:
        pass
    try:
        bridge.prepare(envelope, packet.replace('Members may', 'Visitors may'))
        assert False
    except ValueError:
        pass


def test_cli_prepare_decode_identity_and_replay(tmp_path):
    envelope, packet = fixture()
    first = call(json.dumps({'operation': 'prepare', 'envelope_json': json.dumps(envelope), 'packet': packet}).encode())
    assert first.returncode == 0, first.stderr
    data = json.loads(first.stdout); binding = data['binding']
    raw = json.dumps({'verdict': 'source_rule', 'units': {u['id']: 'answer' for u in binding['prepared']['units']}, 'dependencies': [], 'comparisons': []})
    second = call(json.dumps({'operation': 'decode', 'binding': binding, 'raw': raw}).encode(), cwd=tmp_path)
    assert second.returncode == 0, second.stderr
    result = json.loads(second.stdout)
    assert len(data['identity']) == 64 and data['identity'] == result['identity']
    assert result['result'] == bridge.decode(raw, binding)


def test_cli_rejects_duplicate_extra_and_oversized_inputs_without_echo():
    for raw in [b'{"operation":"prepare","operation":"decode"}', b'{"operation":"prepare","secret":"sensitive"}', b'x' * 262145]:
        result = call(raw)
        assert result.returncode == 2 and not result.stdout and b'sensitive' not in result.stderr
    envelope, packet = fixture()
    result = call(json.dumps({'operation': 'prepare', 'envelope': envelope, 'packet': packet + ' ' * 65536}).encode())
    assert result.returncode == 2 and not result.stdout


from copy import deepcopy
import json
import pytest
from potluck_local.services import investigation_evidence_coverage_v3 as coverage
from potluck_local.services import investigation_revision as revision
from potluck_local.services import investigation_agent_review_bridge as bridge


def envelope():
    text = 'Records expire after thirty days. Managers approve access. Blue folders are optional.'
    quote = 'Managers approve access.'; start = text.index(quote)
    return {'version': revision.VERSION, 'scope': 'cited', 'group': {'id': 'G1', 'focus': 'How long are records kept?', 'request': 'How long are records kept?', 'status': 'unknown', 'draft_sentences': ['PRIVATE DRAFT']}, 'sources': [{'id': 'Q1', 'snippet_id': 'S1', 'quote': quote, 'context': {'citation_id': 'CIT1', 'heading': 'Retention', 'list_introduction': None, 'cited_chunk': {'quote': text, 'selected_start': start, 'selected_end': start + len(quote)}}}]}


def rows(e, selected=()):
    p = coverage.prepare(e)
    return [{'record': 'answer', 'question': p['request'], 'verdict': 'source_rule', 'selected_ids': list(selected), 'rule': 'Retained units count.', 'covered_by_exact_binding_ids': []},
            {'record': 'source', 'id': 'C1', 'headings': ['Retention'], 'retained_bindings': []},
            *[{'id': u['id'], 'state': 'selected' if u['id'] in selected else 'supplied_only', 'text': u['quote']} for u in p['units']]]


def packet(rs): return '\n'.join(json.dumps(r) for r in rs)


def answer(p):
    return {'verdict': 'source_rule', 'units': {u['id']: 'answer' if i == 0 else 'background' for i, u in enumerate(p['units'])}, 'dependencies': [], 'comparisons': []}


def test_state_blind_request_and_deterministic_local_coverage():
    e = envelope(); base = rows(e); uid = base[2]['id']
    missing = bridge.prepare(e, packet(base)); complete = bridge.prepare(e, packet(rows(e, [uid])))
    assert bridge.request(missing) == bridge.request(complete)
    raw = json.dumps(answer(missing['prepared']))
    a, b = bridge.decode(raw, missing), bridge.decode(raw, complete)
    assert a['status'] == 'needs_review' and a['findings'] == [{'unit_id': uid, 'quote': base[2]['text']}]
    assert b['findings'] == [] and a['assessment'] == b['assessment'] and a['generated_words'] > 0
    changed = deepcopy(e); changed['group']['draft_sentences'] = ['DIFFERENT PRIVATE DRAFT']
    removed = deepcopy(e); removed['group'].pop('draft_sentences')
    assert bridge.request(bridge.prepare(changed, packet(base))) == bridge.request(bridge.prepare(removed, packet(base))) == bridge.request(missing)
    assert 'PRIVATE DRAFT' not in json.dumps(bridge.request(missing))


@pytest.mark.parametrize('mutation', ['question', 'unit', 'heading', 'group', 'duplicate', 'state', 'binding', 'unknown', 'verdict'])
def test_packet_disagreement_fails(mutation):
    e = envelope(); r = rows(e)
    if mutation == 'question': r[0]['question'] = 'Other question?'
    if mutation == 'unit': r[2]['text'] += ' altered'
    if mutation == 'heading': r[1]['headings'] = ['Other']
    if mutation == 'group': r[1]['id'] = 'C2'
    if mutation == 'duplicate': r.append(deepcopy(r[2]))
    if mutation == 'state': r[2]['state'] = 'selected'
    if mutation == 'binding': r[1]['retained_bindings'] = [{'start': 0, 'end': 7, 'quote': 'forgery'}]
    if mutation == 'unknown': r[0]['selected_ids'] = ['UNKNOWN']
    if mutation == 'verdict': r[0]['verdict'] = 'not_stated'
    with pytest.raises(ValueError): bridge.prepare(e, packet(r))


def test_retained_binding_requires_selected_source_introduction():
    e = envelope(); prepared = coverage.prepare(e); first, second = prepared['units'][:2]
    intro = first['quote'].rstrip(); context = e['sources'][0]['context']
    context['list_introduction'] = {'start': first['start'], 'end': first['start'] + len(intro), 'quote': intro}
    r = rows(e, [second['id']]); r[0]['covered_by_exact_binding_ids'] = [first['id']]; r[2]['state'] = 'retained_binding'
    r[1]['retained_bindings'] = [context['list_introduction']]
    b = bridge.prepare(e, packet(r)); assert bridge.decode(json.dumps(answer(b['prepared'])), b)['findings'] == []
    forged = rows(envelope()); forged[0]['covered_by_exact_binding_ids'] = [first['id']]; forged[2]['state'] = 'retained_binding'
    forged[1]['retained_bindings'] = [context['list_introduction']]
    with pytest.raises(ValueError): bridge.prepare(envelope(), packet(forged))
    r[1]['retained_bindings'][0]['start'] += 1
    with pytest.raises(ValueError): bridge.prepare(e, packet(r))


def test_duplicate_keys_and_decoder_errors_fail():
    e = envelope(); r = rows(e); p = packet(r)
    with pytest.raises(ValueError): bridge.prepare(e, p.replace('"record": "answer"', '"record": "answer", "record": "answer"'))
    b = bridge.prepare(e, p); data = answer(b['prepared'])
    for raw in ['{}', '{"verdict":"source_rule","verdict":"source_rule"}', json.dumps({**data, 'units': {**data['units'], 'UNKNOWN': 'answer'}}), json.dumps({**data, 'verdict': 'not_stated'}), json.dumps({**data, 'units': {**data['units'], next(iter(data['units'])): 'uncertain'}})]:
        with pytest.raises(ValueError): bridge.decode(raw, b)


def with_text(text):
    e = envelope(); e['sources'][0]['quote'] = text
    e['sources'][0]['context']['cited_chunk'] = {'quote': text, 'selected_start': 0, 'selected_end': len(text)}
    return e


def test_unit_count_and_utf8_byte_bounds():
    e = with_text(' '.join(f'Rule {i} applies.' for i in range(17)))
    with pytest.raises(ValueError, match='unit limit'): bridge.prepare(e, packet(rows(e)))
    e = with_text('界' * 342 + '.')
    with pytest.raises(ValueError, match='unit limit'): bridge.prepare(e, packet(rows(e)))


def test_derived_finding_limit_and_word_count_include_keys_and_values():
    e = with_text('First rule. Second rule. Third rule. Fourth rule.')
    b = bridge.prepare(e, packet(rows(e))); data = answer(b['prepared'])
    data['units'] = {uid: 'answer' for uid in data['units']}
    with pytest.raises(ValueError, match='finding limit'): bridge.decode(json.dumps(data), b)
    data['units'] = {uid: 'background' for uid in data['units']}; data['units']['U1'] = 'answer'
    result = bridge.decode(json.dumps(data), b)
    assert result['generated_words'] == bridge._words(data)
    assert result['generated_words'] == sum(len(x.split()) for x in ['verdict', 'source_rule', 'units', *data['units'].keys(), *data['units'].values(), 'dependencies', 'comparisons'])


def test_binding_tampering_rejected_at_both_entrypoints():
    e = envelope(); b = bridge.prepare(e, packet(rows(e))); raw = json.dumps(answer(b['prepared']))
    for key, value in [('selected_ids', ['U1']), ('max_findings', 99), ('max_words', 999), ('version', 'other')]:
        bad = deepcopy(b); bad[key] = value
        with pytest.raises(ValueError): bridge.request(bad)
        with pytest.raises(ValueError): bridge.decode(raw, bad)
    bad = deepcopy(b); bad['prepared']['units'][0]['quote'] = 'forged'
    with pytest.raises(ValueError): bridge.request(bad)


def test_selected_focus_not_full_question():
    e = envelope(); e['single_focus'] = False; e['group']['request'] = 'How long and who approves?'
    r = rows(e); r[0]['question'] = e['group']['focus']
    bridge.prepare(e, packet(r))
    r[0]['question'] = e['group']['request']
    with pytest.raises(ValueError): bridge.prepare(e, packet(r))


def test_invalid_envelope_is_value_error():
    for e in (None, {}, {'group': None}):
        with pytest.raises(ValueError): bridge.prepare(e, '{}')


def test_distinct_contexts_with_same_citation_are_grouped_by_source_ids():
    e = envelope(); other = deepcopy(e['sources'][0]); other.update(id='Q2', snippet_id='S2', quote='Other rule.')
    other['context']['cited_chunk'] = {'quote': 'Other rule.', 'selected_start': 0, 'selected_end': 11}
    e['sources'].append(other); p = coverage.prepare(e)
    assert p['contexts'][0]['citation_id'] == p['contexts'][1]['citation_id']
    r = [{'record': 'answer', 'question': p['selection_scope']['focus'], 'verdict': 'source_rule', 'selected_ids': [], 'rule': 'Retained units count.', 'covered_by_exact_binding_ids': []}]
    for i, context in enumerate(p['contexts'], 1):
        r.append({'record': 'source', 'id': f'C{i}', 'headings': ['Retention'], 'retained_bindings': []})
        r.extend({'id': u['id'], 'state': 'supplied_only', 'text': u['quote']} for u in p['units'] if u['source_ids'] == context['source_ids'])
    bridge.prepare(e, packet(r))
    r[1]['headings'] = ['Wrong']
    with pytest.raises(ValueError): bridge.prepare(e, packet(r))


def test_unexpected_failure_reports_unavailable_without_exception_content(monkeypatch, capsys):
    import io
    class Input:
        buffer = io.BytesIO(b'{"operation":"decode","binding":{},"raw":"{}"}')
    monkeypatch.setattr(bridge.sys, 'stdin', Input())
    def broken(*args):
        raise RuntimeError('sensitive internal details')
    monkeypatch.setattr(bridge, 'decode', broken)
    assert bridge.main() == 1
    captured = capsys.readouterr()
    assert captured.err == 'Bridge unavailable.\n' and not captured.out


def test_cli_rejects_nested_duplicate_envelope_keys():
    envelope, packet = fixture()
    raw = json.dumps(envelope).replace('"status": "supported"', '"status": "supported", "status": "unknown"')
    assert raw != json.dumps(envelope)
    result = call(json.dumps({'operation': 'prepare', 'envelope_json': raw, 'packet': packet}).encode())
    assert result.returncode == 2 and not result.stdout

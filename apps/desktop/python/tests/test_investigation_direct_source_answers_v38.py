import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_part_selection_v38 as v
from potluck_local.services import investigation_fixed_part_answers_v38 as adapter
from potluck_local.services import investigation_reviewed_evidence
from test_investigation_clause_audit_v36 import fixture, proposal

def prepared():
    q, e, plans, plan, _, _ = fixture()
    p = v.prepare(q, e, plan, request=q, unit_plans=plans)
    return (q, e, plans, plan, p, v.authored_inventory(p))

def finding(kind='substantive_rule_or_value', status='established', sources=None):
    if kind == 'supplied_text_yes_no':
        return {'question_kind': kind, 'source_answer': status, 'substantive_finding': 'not_applicable', 'sources': sources or ['C1:rule']}
    return {'question_kind': kind, 'source_answer': 'not_applicable', 'substantive_finding': status, 'sources': sources or ['C1:rule']}

def test_exact_native_partition_and_three_stage_receipts():
    q, e, plans, plan, p, first = prepared()
    clauses = ['Which rule applies, ', 'and what is the exact deadline?']
    c = proposal(*clauses)
    typed = json.dumps({'F1': finding(), 'F2': finding(status='not_established')})
    assert v.decode_clause_proposal(c, p, first)['parts']['P1'] == clauses
    assert v.decode_proposal(typed, p, first, c)['findings']['F2']['substantive_finding'] == 'not_established'
    assert adapter.clause_receipt(p, first)['schema_sha256'] != adapter.review_receipt(p, first, c)['schema_sha256']
    assert adapter.necessity_receipt(p, first, c, typed)['proposal_raw_sha256']
    r = v.decode_review(typed, p, first, c, typed)
    assert r['status'] == 'unknown' and clauses[1] in r['text']
    assert 'Approval is required.' in r['text'] and 'Emergency work is exempt.' in r['text']
    assert r['parts'][0]['passages'][0]['citation_id'] == 'claim:a'
    assert r['review_raw'] == typed and r['typed_findings'] == json.loads(typed)
    assert 'application_derived_clause_audit' in r
    snippets, _ = investigation_reviewed_evidence.snippets(e)
    bundle = json.dumps({'clause_raw': c, 'selection_raw': typed, 'review_raw': typed})
    brief, trace = adapter.make_brief(q, {'id': 'q', 'request': q}, e, snippets, bundle, question_part_plan=plan, unit_plans=plans)
    assert brief['answers'][0]['status'] == 'unknown' and trace['raw'] == bundle
    assert trace['clause_proposal']['raw'] == c and trace['proposal']['raw'] == typed

def test_source_content_no_is_scoped_supported_and_conflict_has_priority():
    _, _, _, _, p, first = prepared()
    clauses = ['Which rule applies, ', 'and what is the exact deadline?']
    c = proposal(*clauses)
    raw = json.dumps({'F1': finding(), 'F2': finding('supplied_text_yes_no', 'no')})
    r = v.decode_review(raw, p, first, c, raw)
    assert r['status'] == 'supported' and r['unresolved_requests'] == []
    assert r['acknowledgements'][0]['answer'] == 'No' and 'selected reviewed statements' in r['text']
    assert r['acknowledgements'][0]['source_unit_ids'] == ['C1:rule']
    conflict = json.dumps({'F1': finding(status='incompatible'), 'F2': finding(status='not_established')})
    r = v.decode_review(conflict, p, first, c, conflict)
    assert r['status'] == 'conflicting' and r['unresolved_requests']

@pytest.mark.parametrize('mutate', [lambda d: d['F1'].update(sources=['unknown']), lambda d: d['F1'].update(sources=[]), lambda d: d['F1'].update(substantive_finding='unknown'), lambda d: d['F1'].update(extra=True), lambda d: d['F1'].update(sources=['C1:rule', 'C1:rule'])])
def test_reject_invalid_typed_findings(mutate):
    _, _, _, _, p, first = prepared()
    c = proposal('Which rule applies, ', 'and what is the exact deadline?')
    d = {'F1': finding(), 'F2': finding()}
    mutate(d)
    with pytest.raises(ValueError):
        v.decode_proposal(json.dumps(d), p, first, c)

def test_unicode_partition_and_binding_tamper():
    q = 'Why café, and when 🕒?'
    _, e, plans, _, _, _ = fixture()
    from potluck_local.services import investigation_request_parts as parts
    plan = parts.prepare(q, [q], origin='fixture_supplied', request=q)
    p = v.prepare(q, e, plan, request=q, unit_plans=plans)
    first = v.authored_inventory(p)
    c = proposal('Why café, ', 'and when 🕒?')
    assert v.decode_clause_proposal(c, p, first)['parts']['P1'] == ['Why café, ', 'and when 🕒?']
    with pytest.raises(ValueError):
        v.decode_clause_proposal(proposal('Why café', 'when 🕒?'), p, first)
    bad = deepcopy(p)
    bad['claims'][0]['text'] += 'tamper'
    with pytest.raises(ValueError):
        v.clause_messages(bad, first)

def test_clause_global_bound_and_changed_partition_rejected():
    _, _, _, _, p, first = prepared()
    with pytest.raises(ValueError):
        v.decode_clause_proposal(proposal('Which ', 'rule ', 'applies, ', 'and ', 'what ', 'is ', 'the exact deadline?'), p, first)
    c = proposal('Which rule applies, ', 'and what is the exact deadline?')
    raw = json.dumps({'F1': finding(), 'F2': finding()})
    changed = proposal('Which rule applies, and ', 'what is the exact deadline?')
    assert adapter.review_receipt(p, first, c)['clause_raw_sha256'] != adapter.review_receipt(p, first, changed)['clause_raw_sha256']


def test_negative_worded_source_question_preserves_direct_yes_polarity():
    from potluck_local.services import investigation_request_parts as parts
    question = 'Does the note not specify a deadline?'
    _, evidence, plans, _, _, _ = fixture()
    plan = parts.prepare(question, [question], origin='fixture_supplied', request=question)
    prepared = v.prepare(question, evidence, plan, request=question, unit_plans=plans)
    first = v.authored_inventory(prepared)
    clauses = proposal(question)
    raw = json.dumps({'F1': finding('supplied_text_yes_no', 'yes')})
    result = v.decode_review(raw, prepared, first, clauses, raw)
    assert result['acknowledgements'][0]['answer'] == 'Yes'
    assert 'Yes: ' + question in result['text']
    assert '- Yes:' in result['text'] and '[1]' in result['text']


def test_instruction_bytes_and_cross_branch_values():
    import hashlib
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel

    assert hashlib.sha256(v.DRAFT.encode()).hexdigest() == 'b9bcb1fed9ffa327fc2f3d9c2da83624aa1e895934dd5a88cb7084f8a9f1b0c0'
    assert hashlib.sha256(v.AUDIT.encode()).hexdigest() == '541364e4c9d7861c2a0fc6564b24d5b11d183f85eb2acecd31bef4bfd722a895'
    _, _, _, _, value, first = prepared()
    c = proposal('Which rule applies, ', 'and what is the exact deadline?')
    fmt = {'type': 'json_object', 'schema': v.review_schema(value, first, c)}
    assert _structured_response_format(object.__new__(SplitModel), fmt) == fmt
    invalid = {'F1': finding(), 'F2': finding('supplied_text_yes_no', 'yes')}
    invalid['F2']['substantive_finding'] = 'established'
    with pytest.raises(ValueError):
        v.decode_proposal(json.dumps(invalid), value, first, c)


@pytest.mark.parametrize('separator, second_group', [('', 1), (' ', 2)])
def test_acknowledgement_numbers_follow_merged_or_separate_source_groups(separator, second_group):
    from potluck_local.services import investigation_request_parts as parts
    from potluck_local.services import reviewed_source_unit_plan as units

    text = 'Alpha.' + separator + 'Beta.'
    first_end = len('Alpha.')
    second_start = first_end + len(separator)
    evidence = [{'kind': 'reviewed_claim', 'citation_id': 'claim:a', 'content': text,
                 'sources': [{'citation_id': 'source:a', 'content': text}]}]
    plans = {'claim:a': {'source_sha256': units.text_hash(text), 'units': [
        {'id': 'a', 'ranges': [[0, first_end]], 'required_unit_ids': []},
        {'id': 'b', 'ranges': [[second_start, len(text)]], 'required_unit_ids': []},
    ]}}
    question = 'Does it say alpha, and does it say beta?'
    plan = parts.prepare(question, [question], origin='fixture_supplied', request=question)
    value = v.prepare(question, evidence, plan, request=question, unit_plans=plans)
    first = v.authored_inventory(value)
    clauses = proposal('Does it say alpha, ', 'and does it say beta?')
    raw = json.dumps({'F1': finding('supplied_text_yes_no', 'yes', ['C1:a']),
                      'F2': finding('supplied_text_yes_no', 'no', ['C1:b'])})
    result = v.decode_review(raw, value, first, clauses, raw)
    assert 'Yes: Does it say alpha,  [1]' in result['text']
    assert f'No: and does it say beta? [{second_group}]' in result['text']
    assert '[1]\n' in result['text']
    assert ('[2]\n' in result['text']) == (second_group == 2)
    assert result['acknowledgements'][0]['source_unit_ids'] == ['C1:a']
    assert result['acknowledgements'][1]['source_unit_ids'] == ['C1:b']

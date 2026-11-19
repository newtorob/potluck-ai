import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_part_selection_v36 as v
from potluck_local.services import investigation_part_selection_v35 as old
from potluck_local.services import investigation_request_parts as parts
from test_investigation_single_review_v28 import prepared as source


def fixture(question='Which rule applies, and what is the exact deadline?'):
    _, evidence, plans, _, _ = source()
    plan = parts.prepare(question, [question], origin='fixture_supplied', request=question)
    p = v.prepare(question, evidence, plan, request=question, unit_plans=plans)
    return question, evidence, plans, plan, p, v.authored_inventory(p)


def proposal(*clauses): return json.dumps({'parts':{'P1':list(clauses)}}, ensure_ascii=False)
def final(clauses, verdicts):
    return json.dumps({'parts':{'P1':{'clauses':[{'text':t,'evidence':['C1:rule'],'background':[],'verdict':z} for t,z in zip(clauses,verdicts)]}},'safeguards':[]}, ensure_ascii=False)


def test_supported_and_mixed_preserve_conditions_citations_and_unknown():
    question, _, _, _, p, first = fixture()
    clauses = ['Which rule applies, ', 'and what is the exact deadline?']; raw = proposal(*clauses)
    assert v.decode_proposal(raw,p,first)['parts']['P1'] == clauses
    assert v.decode_review(final(clauses,['supported','supported']),p,first,raw)['status'] == 'supported'
    result = v.decode_review(final(clauses,['supported','unknown']),p,first,raw)
    assert result['status'] == 'unknown' and clauses[1] in result['text']
    assert 'selected reviewed statements' in result['text'].lower()
    assert 'Approval is required.' in result['text'] and 'Emergency work is exempt.' in result['text']
    assert result['parts'][0]['passages'][0]['citation_id'] == 'claim:a'
    assert question == ''.join(clauses)


def test_unicode_partition_and_exact_schema():
    question, _, _, _, p, first = fixture('Why café, and when 🕒?')
    clauses = ['Why café, ', 'and when 🕒?']; raw = proposal(*clauses)
    assert ''.join(v.decode_proposal(raw,p,first)['parts']['P1']) == question
    schema = v.necessity_schema(p,first,raw)['properties']['parts']['properties']['P1']['properties']['clauses']
    assert schema['minItems'] == schema['maxItems'] == 2
    assert schema['items']['properties']['text']['enum'] == clauses


def test_only_original_separators_can_be_restored():
    question, _, _, _, p, first = fixture()
    raw = proposal('Which rule applies', 'and what is the exact deadline')
    result = v.decode_proposal(raw, p, first)
    assert result['native_parts']['P1'] == ['Which rule applies', 'and what is the exact deadline']
    assert ''.join(result['parts']['P1']) == question
    for missing_word in [proposal('Which rule applies', 'what is the exact deadline'), proposal('Which rule', 'and what is the exact deadline')]:
        with pytest.raises(ValueError): v.decode_proposal(missing_word, p, first)


@pytest.mark.parametrize('clauses', [['Which rule applies, '], ['and what is the exact deadline?', 'Which rule applies, '], [' ', 'Which rule applies, and what is the exact deadline?'], ['Which ', 'rule ', 'applies, ', 'and ', 'what ', 'is ', 'the exact deadline?']])
def test_invalid_proposed_partition_rejected(clauses):
    *_,p,first=fixture()
    with pytest.raises(ValueError): v.decode_proposal(proposal(*clauses),p,first)


def test_final_partition_drift_and_crossversion_replay_rejected():
    question,evidence,plans,plan,p,first=fixture();clauses=['Which rule applies, ', 'and what is the exact deadline?'];raw=proposal(*clauses)
    with pytest.raises(ValueError): v.decode_review(final(clauses[::-1],['supported','unknown']),p,first,raw)
    altered=deepcopy(p);altered['claims'][0]['text']+='changed'
    with pytest.raises(ValueError):v.decode_proposal(raw,altered,first)
    with pytest.raises(ValueError):old.authored_inventory(p)


def test_scoped_negative_remains_supported_without_false_unknown():
    _, _, _, _, p, first = fixture('Which approval rule and is approval required?')
    clauses = ['Which approval rule ', 'and is approval required?']; raw = proposal(*clauses)
    data = json.loads(final(clauses, ['supported', 'supported']))
    data['parts']['P1']['clauses'][1]['evidence'] = ['C1:other']
    result = v.decode_review(json.dumps(data), p, first, raw)
    assert result['status'] == 'supported' and result['unresolved_requests'] == []
    assert 'No approval is required.' in result['text']


def test_unknown_clause_without_visible_scope_is_rejected():
    _, _, _, _, p, first = fixture()
    clauses = ['Which rule applies, ', 'and what is the exact deadline?']; raw = proposal(*clauses)
    data = json.loads(final(clauses, ['supported', 'unknown']))
    data['parts']['P1']['clauses'][1]['evidence'] = []
    with pytest.raises(ValueError): v.decode_review(json.dumps(data), p, first, raw)


@pytest.mark.parametrize('bad_ids', [[{}], [None], ['not-a-source'], ['C1:rule', 'C1:rule']])
def test_invalid_per_clause_ids_rejected_before_aggregation(bad_ids):
    _, _, _, _, p, first = fixture()
    clauses = ['Which rule applies, ', 'and what is the exact deadline?']; raw = proposal(*clauses)
    data = json.loads(final(clauses, ['supported', 'unknown']))
    data['parts']['P1']['clauses'][1]['background'] = bad_ids
    with pytest.raises(ValueError): v.decode_review(json.dumps(data), p, first, raw)

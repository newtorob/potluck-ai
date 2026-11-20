"""Mechanical V44 contract checks; not semantic model qualification."""
import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_source_question_form_v2 as form
from potluck_local.services import investigation_part_selection_v44 as selection
from potluck_local.services import investigation_fixed_part_answers_v44 as adapter
from potluck_local.services import investigation_part_selection_v43 as older
from potluck_local.services import investigation_request_parts as parts
from potluck_local.services import investigation_reviewed_evidence as evidence


def setup(question, texts):
    claims = [{'kind': 'reviewed_claim', 'citation_id': f'claim:{i}', 'content': text,
               'sources': [{'citation_id': f'source:{i}', 'content': text}]}
              for i, text in enumerate(texts, 1)]
    plan = parts.prepare(question, [question], origin='fixture_supplied', request=question)
    prepared = selection.prepare(question, claims, plan, request=question)
    return claims, plan, prepared, selection.authored_inventory(prepared)


@pytest.mark.parametrize('question', [
    'Do these selected tasks specify an exact processing time?',
    'and do these notes give a maximum size?',
    'Does the supplied document state the filing date?',
    'Do those reviewed statements provide a total?',
    'Do these tasks list the required materials?',
    'Do the selected notes not provide a fee?',
])
def test_question_only_form(question):
    row = form.source_availability_frame(question)
    assert row and row['question_only'] and not row['answer_or_polarity_inferred']
    assert 'yes' not in row and 'no' not in row


@pytest.mark.parametrize('question', [
    'Do these tasks provide a fee?', 'Do these tasks provide a service?',
    'Do these notes give permission to enter?', 'Do these notes provide access for a fixed term?',
    'May staff use the equipment?', 'Did the person in the notes sign?',
    'Do the notes list a date and may staff enter?',
    'Do these tasks specify a fee if approved?',
    'Do the notes give “a free pass”?', 'Do the note state a date?',
    'Does the document author provide a date?', 'Do no tasks give a deadline?',
])
def test_ambiguous_or_actual_question_unrestricted(question):
    assert form.source_availability_frame(question) is None


def test_conditional_properties_keep_context_and_independent_decisions():
    q = 'Do these tasks specify a refund period? If not, what exception applies, and what storage rule applies?'
    texts = ['The exception applies only after approval.', 'Storage requires a sealed case.']
    claims, plan, prepared, first = setup(q, texts)
    clauses = ['Do these tasks specify a refund period?', ' If not, what exception applies,', ' and what storage rule applies?']
    partition = json.dumps({'parts': {'P1': clauses}})
    draft = {'F1': {'decision': 'no', 'sources': ['C1:whole', 'C2:whole']},
             'F2': {'decision': 'established', 'sources': ['C1:whole']},
             'F3': {'decision': 'established', 'sources': ['C2:whole']}}
    raw = json.dumps(draft)
    payload = json.loads(selection.review_messages(prepared, first, partition)[1]['content'])
    assert ''.join(f['request'] for f in payload['facts']) == q
    assert 'If not' in payload['parent_context'] and payload['question'] == q
    assert selection.review_schema(prepared, first, partition)['properties']['F1']['properties']['decision']['enum'] == ['yes', 'no', 'incompatible', 'mapping_unclear']
    result = selection.decode_review(raw, prepared, first, partition, raw)
    assert result['status'] == 'supported' and all(t in result['text'] for t in texts)
    assert result['acknowledgements'][0]['answer'] == 'No' and not result['unresolved_requests']
    snippets, _ = evidence.snippets(claims)
    bundle = json.dumps({'clause_raw': partition, 'selection_raw': raw, 'review_raw': raw})
    brief, trace = adapter.make_brief(q, {'id': 'q', 'request': q}, claims, snippets, bundle, question_part_plan=plan)
    assert brief['answers'][0]['status'] == 'supported' and trace['proposal']['raw'] == raw
    with pytest.raises(ValueError):
        adapter.make_brief(q, {'id': 'q', 'request': q}, claims, snippets, bundle, question_part_plan=plan, _contract=older)
    altered = deepcopy(prepared); altered['claims'][0]['text'] += ' altered'
    with pytest.raises(ValueError): selection.review_messages(altered, first, partition)


def test_unknown_actual_occurrence_does_not_erase_known_condition():
    q = 'What must happen first, and did the lead sign?'
    _, _, prepared, first = setup(q, ['A lead must sign before release.'])
    clauses = json.dumps({'parts': {'P1': ['What must happen first,', ' and did the lead sign?']}})
    raw = json.dumps({'F1': {'decision': 'established', 'sources': ['C1:whole']},
                      'F2': {'decision': 'not_established', 'sources': ['C1:whole']}})
    result = selection.decode_review(raw, prepared, first, clauses, raw)
    assert result['status'] == 'unknown'
    assert 'A lead must sign before release.' in result['text']
    assert [r['text'] for r in result['unresolved_requests']] == [' and did the lead sign?']
    assert 'requirement' in selection.clause_messages(prepared, first)[0]['content']


@pytest.mark.parametrize('decision', ['established', 'not_established'])
def test_source_form_schema_and_decoder_enforce_answer_form(decision):
    q = 'and do these notes give a maximum size?'
    _, _, prepared, first = setup(q, ['The record describes a shipping label.'])
    clause = json.dumps({'parts': {'P1': [q]}})
    good = json.dumps({'F1': {'decision': 'no', 'sources': ['C1:whole']}})
    bad = json.dumps({'F1': {'decision': decision, 'sources': ['C1:whole']}})
    assert selection.review_schema(prepared, first, clause) == selection.necessity_schema(prepared, first, clause, good)
    with pytest.raises(ValueError): selection.decode_proposal(bad, prepared, first, clause)
    with pytest.raises(ValueError): selection.decode_review(bad, prepared, first, clause, good)


def test_partition_replay_rejects_word_omission_and_final_mapping_unclear():
    q = 'What is needed, and did it occur?'
    _, _, prepared, first = setup(q, ['Approval is needed.'])
    bad = json.dumps({'parts': {'P1': ['What is needed,', ' did it occur?']}})
    with pytest.raises(ValueError): selection.decode_clause_proposal(bad, prepared, first)
    clause = json.dumps({'parts': {'P1': [q]}})
    proposal = json.dumps({'F1': {'decision': 'established', 'sources': ['C1:whole']}})
    refused = json.dumps({'F1': {'decision': 'mapping_unclear', 'sources': ['C1:whole']}})
    with pytest.raises(ValueError, match='Question mapping needs review'):
        selection.decode_review(refused, prepared, first, clause, proposal)

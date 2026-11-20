from copy import deepcopy
import pytest
from potluck_local.services import investigation_model_packet_v1 as packet


def payload():
    question = 'Do these notes specify a date? If not, what time and fee do they describe?'
    parent = ' If not, what time and fee do they describe?'
    details = {'parent_request': parent, 'answer_form': 'description_request',
               'subject_reference': 'unresolved_pronoun', 'interpretation': packet.INTERPRETATION}
    return {'question': question, 'request': question,
            'parts': [{'id': 'P1', 'request': question}], 'parent_context': packet.PARENT_CONTEXT,
            'facts': [{'id': 'F1', 'part_id': 'P1', 'request': 'Do these notes specify a date?', 'answer_form': 'selected_text_availability_yes_no'},
                      {'id': 'F2', 'part_id': 'P1', 'request': ' If not, what time', **details},
                      {'id': 'F3', 'part_id': 'P1', 'request': ' and fee do they describe?', **details}],
            'sources': [{'id': 'C1:whole', 'text': 'Exact text Ω.\n'}, {'id': 'C2:whole', 'text': 'Other full text.'}],
            'support_visibility': packet.SUPPORT_VISIBILITY}


@pytest.mark.parametrize('with_proposal', [False, True])
def test_round_trip_preserves_full_payload_and_nonmutation(with_proposal):
    original = payload()
    if with_proposal:
        original.update(proposal={k: {'decision': 'established', 'sources': ['C1:whole']} for k in ('F1', 'F2', 'F3')}, proposal_origin=packet.PROPOSAL_ORIGIN)
    saved = deepcopy(original)
    encoded = packet.compact(original)
    assert original == saved and len(encoded['parents']) == 1
    assert encoded['facts'][1]['parent_id'] == encoded['facts'][2]['parent_id']
    assert packet.expand(encoded) == original
    encoded['sources'][0]['text'] = 'changed'
    assert original == saved


def test_clause_multipart_and_qualifiers_round_trip():
    value = {'question': 'A question with qualifications?', 'request': 'A request',
             'parts': [{'id': 'P1', 'request': 'A question with qualifications?'}, {'id': 'P2', 'request': 'Another part'}]}
    assert packet.expand_clause_context(packet.compact_clause_context(value)) == value


@pytest.mark.parametrize('mutate', [
    lambda p: p.update(parent_context='changed'),
    lambda p: p['facts'][1].update(interpretation='changed'),
    lambda p: p['facts'][1].update(unknown='not understood'),
    lambda p: p['sources'].append(deepcopy(p['sources'][0])),
    lambda p: p['facts'][0].update(part_id='missing'),
])
def test_unknown_payload_fails_closed(mutate):
    value = payload(); mutate(value)
    with pytest.raises(ValueError): packet.compact(value)


@pytest.mark.parametrize('mutate', [
    lambda p: p.update(contract='other'),
    lambda p: p['facts'][1].update(parent_id='PARENT2'),
    lambda p: p['parents'][0].update(id='PARENT2'),
    lambda p: p['facts'][1].update(interpretation_v1=False),
    lambda p: p['parts'][0].update(request={'whole_question': False}),
])
def test_tampered_compact_refs_fail_closed(mutate):
    value = packet.compact(payload()); mutate(value)
    with pytest.raises(ValueError): packet.expand(value)


@pytest.mark.parametrize('bad_ref', [{'whole_question': 1}, {'whole_question': 'true'}, {'whole_question': False}])
def test_whole_question_requires_actual_boolean_true(bad_ref):
    from potluck_local.services import investigation_model_packet_v1 as packet
    wire={'contract':packet.CLAUSE_CONTRACT,'question':'Which rule?','request':bad_ref,'parts':[{'id':'P1','request':{'whole_question':True}}]}
    with pytest.raises(ValueError):packet.expand_clause_context(wire)

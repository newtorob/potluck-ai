"""V40 compacts model inputs without clipping source or changing V39."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from potluck_local.services import investigation_part_selection_v39 as old
from potluck_local.services import investigation_part_selection_v40 as new
from potluck_local.services import investigation_fixed_part_answers_v40 as adapter
from potluck_local.services import investigation_reviewed_evidence
from potluck_local.services import investigation_request_parts as parts
from test_investigation_single_review_v28 import prepared as source_fixture


def setup():
    question = 'If café access is approved, what is required and when does it end?'
    _, evidence, plans, _, _ = source_fixture()
    plan = parts.prepare(question, [question], origin='fixture_supplied', request=question)
    prepared = new.prepare(question, evidence, plan, request=question, unit_plans=plans)
    return question, evidence, plans, plan, prepared, new.authored_inventory(prepared)


def outputs(question):
    boundary = question.index('and when')
    clause = json.dumps({'parts': {'P1': [question[:boundary], question[boundary:]]}}, ensure_ascii=False)
    proposal = json.dumps({'F1': {'question_kind':'substantive_rule_or_value','source_answer':'no','substantive_finding':'established','sources':['C1:rule']},
                           'F2': {'question_kind':'substantive_rule_or_value','source_answer':'not_applicable','substantive_finding':'not_established','sources':['C1:rule']}})
    audit = json.dumps({'F1': {'decision':'established','sources':['C1:rule']}, 'F2': {'decision':'not_established','sources':['C1:rule']}})
    return clause, proposal, audit


def test_clause_payload_excludes_catalog_but_hash_still_binds_source():
    q,e,plans,plan,p,first=setup()
    message=new.clause_messages(p,first)
    payload=json.loads(message[1]['content'])
    assert payload == {'question':q,'request':q,'parts':[{'id':'P1','request':q}]}
    assert 'Approval is required.' not in message[1]['content']
    assert adapter.clause_receipt(p,first)['model_visible_claim_ids']==[]
    altered=deepcopy(e); altered[0]['content'] += ' Changed.'
    other=new.prepare(q,altered,plan,request=q)
    assert other['input_sha256'] != p['input_sha256']
    assert new.clause_messages(other,new.authored_inventory(other))==message
    assert adapter.clause_receipt(other,new.authored_inventory(other))['complete_input_sha256']!=adapter.clause_receipt(p,first)['complete_input_sha256']


def test_typed_and_audit_payloads_reconstruct_original_context_and_full_sources():
    q,e,plans,plan,p,first=setup();c,proposal,audit=outputs(q)
    draft=json.loads(new.review_messages(p,first,c)[1]['content'])
    reviewed=json.loads(new.necessity_messages(p,first,c,proposal)[1]['content'])
    expected=[{'id':u['id'],'text':u['text']} for u in p['catalog']['units']]
    assert draft['sources']==reviewed['sources']==expected
    assert draft['question']==draft['request']==q
    assert draft['parts']==[{'id':'P1','request':q}]
    assert all('part_request' not in fact for fact in draft['facts'])
    assert all(next(part['request'] for part in draft['parts'] if part['id']==fact['part_id'])==q for fact in draft['facts'])
    assert ''.join(fact['request'] for fact in draft['facts'])==q
    assert 'qualifiers' in draft['parent_context']
    assert json.loads(new.necessity_messages(p,first,c,proposal)[1]['content'])['proposal']==json.loads(proposal)
    assert ': ' not in new.review_messages(p,first,c)[1]['content']
    result=new.decode_review(audit,p,first,c,proposal)
    assert result['status']=='unknown'
    assert 'Approval is required.' in result['text'] and 'Emergency work is exempt.' in result['text']
    assert result['parts'][0]['passages'][0]['citation_id']=='claim:a'


def test_new_receipts_replay_tamper_and_v39_unchanged():
    q,e,plans,plan,p,first=setup();c,proposal,audit=outputs(q)
    old_p=old.prepare(q,e,plan,request=q,unit_plans=plans);old_first=old.authored_inventory(old_p)
    before=old.clause_messages(old_p,old_first)
    old_file=Path(old.__file__); old_hash=hashlib.sha256(old_file.read_bytes()).hexdigest()
    assert new.review_schema(p,first,c)==old.review_schema(old_p,old_first,c)
    assert new.necessity_schema(p,first,c,proposal)==old.necessity_schema(old_p,old_first,c,proposal)
    assert new.DRAFT==old.DRAFT and new.AUDIT==old.AUDIT
    snippets,_=investigation_reviewed_evidence.snippets(e)
    raw=json.dumps({'clause_raw':c,'selection_raw':proposal,'review_raw':audit})
    brief,trace=adapter.make_brief(q,{'id':'q','request':q},e,snippets,raw,question_part_plan=plan,unit_plans=plans)
    assert trace['raw']==raw and brief['answers'][0]['status']=='unknown'
    assert adapter.necessity_receipt(p,first,c,proposal)['schema_sha256']
    damaged=deepcopy(p);damaged['claims'][0]['text']+='tampered'
    with pytest.raises(ValueError):new.clause_messages(damaged,first)
    with pytest.raises(ValueError):adapter.make_brief(q+' changed',{'id':'q','request':q},e,snippets,raw,question_part_plan=plan,unit_plans=plans)
    assert old.clause_messages(old_p,old_first)==before
    assert hashlib.sha256(old_file.read_bytes()).hexdigest()==old_hash


def test_multiple_parts_preserve_shared_qualifiers_and_order():
    question = 'Only after approval, what applies; and only after approval, when does it end?'
    _, evidence, plans, _, _ = source_fixture()
    requests = ['Only after approval, what applies;', 'and only after approval, when does it end?']
    plan = parts.prepare(question, requests, origin='fixture_supplied', request=question)
    prepared = new.prepare(question, evidence, plan, request=question, unit_plans=plans)
    first = new.authored_inventory(prepared)
    clauses = json.dumps({'parts': {'P1': [requests[0]], 'P2': [requests[1]]}})
    payload = json.loads(new.review_messages(prepared, first, clauses)[1]['content'])
    assert payload['parts'] == [{'id': 'P1', 'request': requests[0]}, {'id': 'P2', 'request': requests[1]}]
    assert [(f['part_id'], f['request']) for f in payload['facts']] == [('P1', requests[0]), ('P2', requests[1])]
    assert payload['question'] == question

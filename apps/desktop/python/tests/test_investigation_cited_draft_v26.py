"""Boundary checks for unverified drafts and exact retained source provenance."""
import json
from copy import deepcopy
import pytest
from test_investigation_single_review_v26 import prepared, wire
from potluck_local.services import investigation_part_selection_v26 as mode
from potluck_local.services import investigation_part_draft_presentation as display


def decode(raw):
    *_,p=prepared()
    return mode.decode_review(raw,p,mode.authored_inventory(p))


def test_draft_is_separate_from_retained_source_and_dependency_basis():
    raw=json.loads(wire('supported',['C1:rule']))
    raw['parts']['P1']['answer']='An explicit approval decision is needed.'
    result=decode(json.dumps(raw))
    assert 'An explicit approval decision is needed.' in result['text']
    assert 'C1:exception' in result['parts'][0]['dependency_only_unit_ids']
    assert all(p['text'] for p in result['parts'][0]['passages'])
    assert result['source_text'] and result['source_text']!=result['text']
    assert result['needs_review'] is True and result['coverage']=='not_verified'
    assert all('text' not in row for row in result['answer_citations'])
    assert {r['unit_id'] for r in result['answer_citations']}==set(result['unit_ids'])
    assert [r['number'] for r in result['answer_citations']]==list(range(1,len(result['answer_citations'])+1))


def test_structural_source_linkage_is_not_entailment_or_semantic_correction():
    raw=json.loads(wire('supported',['C1:rule']))
    raw['parts']['P1']['answer']='No approval is needed.'
    result=decode(json.dumps(raw))
    # A human semantic rubric must fail this contradiction; schema/source replay
    # neither silently rewrites it nor asserts that it is true.
    assert 'No approval is needed.' in result['text']
    assert result['needs_review'] and result['coverage']=='not_verified'
    assert result['parts'][0]['passages']


@pytest.mark.parametrize('answer',['Unknown globally.','The whole corpus contains no deadline.','The unspecified deadline is seven days.','Not established by the selected reviewed statements. '])
def test_unknown_requires_literal_selected_statement_boundary(answer):
    raw=json.loads(wire('unknown',[]));raw['parts']['P1']['answer']=answer
    with pytest.raises(ValueError,match='selected-statement scope'):decode(json.dumps(raw))


@pytest.mark.parametrize('answer',['',' '*4,'x'*1001,'A fake citation [999].','```ts code```','Hidden\x00control','Two\nparagraphs'])
def test_invalid_draft_is_rejected_without_truncation_or_normalization(answer):
    raw=json.loads(wire('supported',['C1:rule']));raw['parts']['P1']['answer']=answer
    with pytest.raises(ValueError,match='Invalid draft answer'):decode(json.dumps(raw))


def test_unmodified_draft_and_citation_map_are_bound_to_replay():
    q,ev,plans,plan,p=prepared();raw=wire('supported',['C1:rule']);result=decode(raw)
    assert mode.validate_result(q,ev,plan,raw,result,unit_plans=plans)==result
    for mutation in ('text','range','summary'):
        bad=deepcopy(result)
        if mutation=='text':bad['text']+=' changed'
        elif mutation=='range':bad['answer_citations'][0]['ranges'][0][0]+=1
        else:bad['parts'][0]['draft_answer']='Changed after generation.'
        with pytest.raises(ValueError,match='replay mismatch'):mode.validate_result(q,ev,plan,raw,bad,unit_plans=plans)


def test_reviewed_statement_visibility_and_actual_native_schema_admission():
    from potluck_local.services import runtime,split_native
    *_,p=prepared();first=mode.authored_inventory(p)
    payload=json.loads(mode.review_messages(p,first)[1]['content'])
    assert 'text is provided' in payload['support_visibility']
    assert any(u['text'] for c in payload['claims'] for u in c['units'])
    schema=mode.review_schema(p,first)
    fmt={'type':'json_object','schema':schema}
    assert runtime._structured_response_format(object.__new__(split_native.SplitModel),fmt)==fmt
    assert list(schema['properties']['parts']['properties']['P1']['properties'])==['evidence','context','verdict','answer']

"""Exact evidence, mandatory closure and retained optional background boundaries."""
import json
from copy import deepcopy
import pytest
from test_investigation_single_review_v28 import prepared,wire
from potluck_local.services import investigation_part_selection_v28 as v
from potluck_local.services import investigation_fixed_part_answers_v28 as adapter
from potluck_local.services import investigation_reviewed_evidence as snippets


def result(raw):
    *_,p=prepared();return v.decode_review(raw,p,v.authored_inventory(p))


def test_required_exception_cannot_be_hidden_by_background_role():
    raw=json.loads(wire('supported',['C1:rule']));raw['parts']['P1']['background']=['C1:exception']
    x=result(json.dumps(raw));row=x['parts'][0];view=x['display_parts'][0]
    *_,p=prepared();texts={u['id']:u['text'] for u in p['catalog']['units']}
    assert texts['C1:exception'] in x['text']
    assert 'C1:exception' in row['background_unit_ids']
    assert 'C1:exception' in row['visible_dependency_unit_ids']
    assert 'C1:exception' in view['dependency_only_unit_ids']
    assert 'C1:exception' not in x['background_only_unit_ids']


def test_optional_background_retained_verbatim_without_answer_credit():
    raw=json.loads(wire('supported',['C1:rule']));raw['parts']['P1']['background']=['C1:other']
    x=result(json.dumps(raw));*_,p=prepared();text=next(u['text'] for u in p['catalog']['units'] if u['id']=='C1:other')
    assert text not in x['text'] and text in x['source_text']
    assert any(p['unit_id']=='C1:other' and p['text']==text for p in x['parts'][0]['passages'])
    assert 'C1:other' in x['background_only_unit_ids']
    assert x['needs_review'] and x['coverage']=='not_verified'


def test_request_wide_safeguard_visible_even_if_also_part_background():
    raw=json.loads(wire('supported',['C1:rule']));raw['parts']['P1']['background']=['C1:other'];raw['safeguards']=['C1:other']
    x=result(json.dumps(raw));*_,p=prepared();text=next(u['text'] for u in p['catalog']['units'] if u['id']=='C1:other')
    assert text in x['text'] and 'Request-wide context:' in x['text']
    assert 'C1:other' in x['request_context']['visible_unit_ids']
    assert 'C1:other' in x['visible_unit_ids'] and 'C1:other' not in x['background_only_unit_ids']


def test_background_alone_cannot_support_a_part():
    raw=json.loads(wire('supported',[]));raw['parts']['P1']['background']=['C1:rule']
    with pytest.raises(ValueError,match='evidence'):result(json.dumps(raw))


def test_unknown_stays_selected_scoped_and_background_is_not_missing_fact_rescue():
    raw=json.loads(wire('unknown',[]));raw['parts']['P1']['background']=['C1:other']
    x=result(json.dumps(raw));assert 'Not established by the selected reviewed statements.' in x['text']
    assert x['parts'][0]['visible_unit_ids']==[] and x['parts'][0]['background_unit_ids']==['C1:other']
    assert x['coverage']=='not_verified'  # This does not prove a requested property is absent.


@pytest.mark.parametrize('target',['display_parts','hidden_background','role','source_text'])
def test_display_and_full_basis_both_replay_bound(target):
    q,ev,plans,plan,p=prepared();raw=json.loads(wire('supported',['C1:rule']));raw['parts']['P1']['background']=['C1:other'];raw=json.dumps(raw)
    x=result(raw);bad=deepcopy(x)
    if target=='display_parts':bad['display_parts'][0]['passages'][0]['text']+=' Changed'
    elif target=='hidden_background':bad['background_only_unit_ids']=[]
    elif target=='role':bad['parts'][0]['background_unit_ids']=[]
    else:bad['source_text']+=' Changed'
    with pytest.raises(ValueError,match='replay mismatch'):v.validate_result(q,ev,plan,raw,bad,unit_plans=plans)


def test_native_schema_has_no_generated_check_or_draft_prose():
    from potluck_local.services import runtime,split_native
    *_,p=prepared();schema=v.review_schema(p,v.authored_inventory(p));item=schema['properties']['parts']['properties']['P1']
    assert list(item['properties'])==item['required']==['evidence','background','verdict']
    assert set(schema['properties'])=={'parts','safeguards'}
    fmt={'type':'json_object','schema':schema};assert runtime._structured_response_format(object.__new__(split_native.SplitModel),fmt)==fmt


def test_adapter_retains_background_source_and_explicit_display_contract():
    q,ev,plans,plan,p=prepared();raw=json.loads(wire('supported',['C1:rule']));raw['parts']['P1']['background']=['C1:other']
    catalog,_=snippets.snippets(ev)
    brief,trace=adapter.make_brief(q,{'id':'q','request':q},ev,catalog,json.dumps({'selection_raw':json.dumps(raw),'review_raw':json.dumps(raw)}),question_part_plan=plan,unit_plans=plans)
    a=brief['answers'][0];assert a['passage_presentation']['presentation_kind']=='exact-evidence-with-optional-background-retained'
    assert any(p['unit_id']=='C1:other' for p in a['passage_presentation']['passages'])
    assert any(p['unit_id']=='C1:exception' for p in a['passage_presentation']['display_parts'][0]['passages'])
    assert a['text']==trace['review']['text'] and trace['review']['background_only_unit_ids']==['C1:other']

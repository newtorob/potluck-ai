from copy import deepcopy
import json
import pytest
from potluck_local.services import investigation_evidence_comparisons as ev
from potluck_local.services import investigation_evidence_dependencies as prior
from test_investigation_evidence_revision import sample


def prepared():
    e=sample();e['single_focus']=True;e['group']['request']='What is the software release?'
    text='The protocol version is two. Approval is required. Directors may defer requests.'
    s=e['sources'][0];s['quote']=text
    s['context']['cited_chunk']={'quote':text,'selected_start':0,'selected_end':len(text)}
    return ev.prepare(e)


def answer(p):
    return {'verdict':'not_stated','units':{u['id']:'background' for u in p['units']},'dependencies':[],'comparisons':[]}


def test_source_limited_absence_does_not_invent_selected_support():
    p=prepared();before=deepcopy(p);raw=answer(p);out=ev.decode(json.dumps(raw),p)
    assert out['selected']==[] and out['background']==p['units'] and out['needs_review'] is True
    assert out['absence_scope']=={'kind':'not_stated_in_supplied_records','question':p['selection_scope']['focus'],
      'supplied_source_ids':['Q1'],'supplied_citation_ids':['C1'],'collection_wide_search_verified':False}
    assert p==before
    raw['verdict']='source_rule'
    with pytest.raises(ValueError,match='requires related evidence'):ev.decode(json.dumps(raw),p)
    old=deepcopy(p);old['version']=prior.VERSION
    old_raw=answer(p);old_raw.pop('comparisons');old_raw['distinctions']=[]
    with pytest.raises(ValueError,match='requires related evidence'):prior.decode(json.dumps(old_raw),old)


def test_reference_comparison_preserves_source_and_raw_roles():
    p=prepared();raw=answer(p);raw['comparisons']=[{'kind':'not_requested_fact','unit_ids':['U1','U2']}]
    out=ev.decode(json.dumps(raw),p)
    assert [u['quote'] for u in out['selected']]==[u['quote'] for u in p['units'][:2]]
    assert out['model_classifications']==raw['units']
    assert out['classifications']['U1']=='distinction'
    assert out['comparisons'][0]['text']==ev.COMPARISONS['not_requested_fact']
    assert out['absence_scope']['collection_wide_search_verified'] is False
    raw['units']['U3']='exception';raw['dependencies']=[{'unit_id':'U3','requires':['U2']},{'unit_id':'U2','requires':['U1']},{'unit_id':'U1','requires':['U3']}]
    assert len(ev.decode(json.dumps(raw),p)['selected'])==3


@pytest.mark.parametrize('comparison',[
 {'kind':'fabricated','unit_ids':['U1']},
 {'kind':'distinct_entities','unit_ids':['U999']},
 {'kind':'distinct_entities','unit_ids':[]},
 {'kind':'distinct_entities','unit_ids':['U1','U1']},
 {'kind':'distinct_entities','unit_ids':['U1'],'text':'made up'},
 {'kind':'distinct_entities','unit_ids':['U1'],'value':'invented'},
 {'kind':'distinct_entities','unit_ids':[{}]},
 {'kind':'distinct_entities','unit_ids':'U1'},
])
def test_invalid_or_invented_comparisons_rejected(comparison):
    p=prepared();raw=answer(p);raw['comparisons']=[comparison]
    with pytest.raises(ValueError):ev.decode(json.dumps(raw),p)


def test_duplicate_comparison_and_supported_absence_relation_rejected():
    p=prepared();raw=answer(p);raw['comparisons']=[{'kind':'distinct_entities','unit_ids':['U1','U2']},{'kind':'distinct_entities','unit_ids':['U2','U1']}]
    with pytest.raises(ValueError,match='Repeated'):ev.decode(json.dumps(raw),p)
    raw['comparisons']=[{'kind':'not_requested_fact','unit_ids':['U1']}];raw['verdict']='source_rule'
    with pytest.raises(ValueError,match='absent-fact'):ev.decode(json.dumps(raw),p)
    raw['comparisons']=[{'kind':'distinct_entities','unit_ids':['U1','U2']}];raw['units']['U1']='answer'
    out=ev.decode(json.dumps(raw),p)
    assert out['absence_scope'] is None and out['classifications']['U1']=='answer'


@pytest.mark.parametrize('edges',[
 [{'unit_id':'U2','requires':['U1']}],
 [{'unit_id':'U1','requires':['U1']}],
 [{'unit_id':'U1','requires':['U999']}],
 [{'unit_id':'U1','requires':['U2','U2']}],
 [{'unit_id':'U1','requires':['U2']},{'unit_id':'U1','requires':['U3']}],
 [{'unit_id':'U1','requires':[]}],
])
def test_dependency_validation_stays_strict(edges):
    p=prepared();raw=answer(p);raw['units']['U1']='answer';raw['dependencies']=edges
    with pytest.raises(ValueError):ev.decode(json.dumps(raw),p)


def test_transitive_dependency_closure_and_scope_are_preserved():
    p=prepared();raw=answer(p);raw['verdict']='source_rule';raw['units']['U3']='exception'
    raw['dependencies']=[{'unit_id':'U3','requires':['U2']},{'unit_id':'U2','requires':['U1']}]
    out=ev.decode(json.dumps(raw),p)
    assert [u['classification'] for u in out['selected']]==['dependency','dependency','exception']
    e=sample();e['single_focus']=False;e['group']['request']='Can someone apply?';e['group']['focus']='Exceptions'
    p=ev.prepare(e);raw=answer(p);raw['verdict']='no';raw['units']['U1']='answer'
    with pytest.raises(ValueError,match='question form'):ev.decode(json.dumps(raw),p)
    assert p['selection_scope']['focus']=='Exceptions'


def test_prompt_version_no_draft_leak_schema_and_duplicate_json():
    p=prepared();messages=ev.messages(p);payload=json.loads(messages[1]['content'])
    assert payload['version']==ev.VERSION and payload['question']==p['selection_scope']['focus']
    assert 'PRIVATE DRAFT' not in json.dumps(messages)
    assert 'status' not in payload
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    response={'type':'json_object','schema':ev.response_schema(p)}
    assert _structured_response_format(SplitModel.__new__(SplitModel),response)==response
    raw=json.dumps(answer(p)).replace('"verdict": "not_stated"','"verdict": "source_rule", "verdict": "not_stated"')
    with pytest.raises(ValueError):ev.decode(raw,p)

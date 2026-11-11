from copy import deepcopy
import pytest
from potluck_local.services.investigation_part_shared_presentation import render

def part(id,verdict,answer=(),alternative=(),context=(),deps=()):
    roles=list(answer)+list(alternative)+list(context);ids=roles+list(deps)
    return {'id':id,'request':'Question '+id,'verdict':verdict,'answer_unit_ids':list(answer),'alternative_unit_ids':list(alternative),'context_unit_ids':list(context),'requested_unit_ids':roles,'dependency_only_unit_ids':list(deps),'unit_ids':ids,'passages':[{'unit_id':u,'citation_id':'claim:'+u,'ranges':[[0,3]],'text':'text '+u} for u in ids]}

def test_shared_role_refs_context_and_dependencies_preserve_identity():
    rows=[part('P1','supported',['A'],deps=['B']),part('P2','unknown',context=['A'])];ctx={'unit_ids':['B'],'passages':[rows[0]['passages'][1]]};before=deepcopy((rows,ctx))
    r=render(rows,ctx);assert r['text'].count('text A')==r['text'].count('text B')==1
    assert 'P1: Question P1' in r['text'] and 'P2: Question P2' in r['text'] and 'Dependencies: [2]' in r['text'] and 'Context: [1]' in r['text']
    assert (rows,ctx)==before and r['parts']==rows

def test_conflict_and_equal_text_distinct_sources():
    row=part('P1','conflicting',['A'],['B']);row['passages'][1]['text']=row['passages'][0]['text']
    r=render([row],{'unit_ids':[],'passages':[]});assert r['text'].count('text A')==2 and 'no authoritative winner' in r['text'] and len(r['units'])==2

def test_tamper_and_budget_fail_without_clipping():
    a=part('P1','supported',['A']);b=part('P2','supported',['A']);b['passages'][0]['citation_id']='other'
    with pytest.raises(ValueError):render([a,b],{'unit_ids':[],'passages':[]})
    with pytest.raises(ValueError):render([a],{'unit_ids':[],'passages':[]},max_visible=5)
    a['dependency_only_unit_ids']=['B']
    with pytest.raises(ValueError):render([a],{'unit_ids':[],'passages':[]})


def test_status_tamper_and_check_metadata_never_visible():
    row=part('P1','supported',['A']);row['evidence_check']='not source'
    assert 'not source' not in render([row],{'unit_ids':[],'passages':[]})['text']
    row['verdict']='unknown'
    with pytest.raises(ValueError):render([row],{'unit_ids':[],'passages':[]})


@pytest.mark.parametrize('question', ['Is approval optional?', 'Is approval required?'])
def test_supported_group_does_not_invent_yes_for_polar_question(question):
    row=part('P1','supported',['A']);row['request']=question
    row['passages'][0]['text']='Approval is required.'
    output=render([row],{'unit_ids':[],'passages':[]})
    heading=output['text'].split('\n',1)[0]
    assert heading=='P1: '+question
    assert output['parts'][0]['verdict']=='supported'
    assert output['text'].count('Approval is required.')==1


def test_conflict_lead_names_selected_scope_without_global_authority_claim():
    row=part('P1','conflicting',['A'],['B'])
    result=render([row],{'unit_ids':[],'passages':[]})
    assert 'Selected reviewed statements have unresolved incompatible alternatives' in result['text']
    assert result['parts'][0]['alternative_unit_ids']==['B']

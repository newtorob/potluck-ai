from potluck_local.services.investigation_part_relation_presentation import render
import pytest

def test_adjacent_exact_source_ranges_join_without_bridging_gaps():
    entries=[{'unit_id':'A','citation_id':'claim:a','ranges':[[0,3]],'text':'abc'}, {'unit_id':'B','citation_id':'claim:a','ranges':[[3,6]],'text':'def'}, {'unit_id':'C','citation_id':'claim:a','ranges':[[7,10]],'text':'ghi'}, {'unit_id':'D','citation_id':'claim:b','ranges':[[10,13]],'text':'jkl'}]
    row={'id':'P1','request':'Compare?','verdict':'conflicting','answer_unit_ids':['A','B','C','D'],'alternative_unit_ids':[],'context_unit_ids':[],'requested_unit_ids':['A','B','C','D'],'dependency_only_unit_ids':[],'unit_ids':['A','B','C','D'],'passages':entries}
    result=render([row],{'unit_ids':[],'passages':[]})
    assert '[1]\nabcdef' in result['text'] and '[2]\nghi' in result['text'] and '[3]\njkl' in result['text']
    assert 'no authoritative winner' in result['text'] and result['parts']==[row]


def test_neutral_polar_heading_unknown_scope_and_distinct_identity():
    from copy import deepcopy
    entry={'unit_id':'A','citation_id':'claim:a','ranges':[[0,3]],'text':'same'}
    other={'unit_id':'B','citation_id':'claim:b','ranges':[[0,3]],'text':'same'}
    def row(id,request,verdict,answer,context,passages):
        ids=answer+context
        return {'id':id,'request':request,'verdict':verdict,'answer_unit_ids':answer,'alternative_unit_ids':[],'context_unit_ids':context,'requested_unit_ids':ids,'dependency_only_unit_ids':[],'unit_ids':ids,'passages':passages}
    parts=[row('P1','Is this allowed?','supported',['A'],[],[entry]),row('P2','Is this prohibited?','unknown',[],['B'],[other])]
    before=deepcopy(parts);r=render(parts,{'unit_ids':[],'passages':[]})
    assert 'P1: Is this allowed?\nAnswer:' in r['text'] and '— Supported' not in r['text']
    assert 'Not established by the selected reviewed statements' in r['text'] and r['text'].count('same')==2 and parts==before


def test_budget_and_identity_tamper_reject():
    import pytest
    entry={'unit_id':'A','citation_id':'claim:a','ranges':[[0,3]],'text':'abc'}
    row={'id':'P1','request':'Question?','verdict':'supported','answer_unit_ids':['A'],'alternative_unit_ids':[],'context_unit_ids':[],'requested_unit_ids':['A'],'dependency_only_unit_ids':[],'unit_ids':['A'],'passages':[entry]}
    with pytest.raises(ValueError):render([row],{'unit_ids':[],'passages':[]},max_visible=1)
    with pytest.raises(ValueError):render([row],{'unit_ids':['A'],'passages':[{**entry,'citation_id':'claim:other'}]})


def make_row(entries,question='Which responsibility?',verdict='supported'):
    ids=[x['unit_id'] for x in entries]
    return {'id':'P1','request':question,'verdict':verdict,'answer_unit_ids':ids,
            'alternative_unit_ids':[],'context_unit_ids':[],'requested_unit_ids':ids,
            'dependency_only_unit_ids':[],'unit_ids':ids,'passages':entries}


def test_adjacent_table_header_and_row_have_no_interleaved_display_label():
    header='| Role | Owner |\n|---|---|\n';row='| Review | Assigned reviewer |\n'
    entries=[{'unit_id':'H','citation_id':'claim:roles','ranges':[[0,len(header)]],'text':header},
             {'unit_id':'R','citation_id':'claim:roles','ranges':[[len(header),len(header)+len(row)]],'text':row}]
    result=render([make_row(entries)],{'unit_ids':[],'passages':[]})
    assert '[1]\n'+header+row in result['text']
    assert '[2]' not in result['text'] and len(result['units'])==2
    assert result['parts'][0]['answer_unit_ids']==['H','R']


@pytest.mark.parametrize('question',['Is approval optional?','Is approval required?'])
def test_opposite_polar_questions_keep_neutral_heading_and_exact_evidence(question):
    text='Approval is required.'
    entry={'unit_id':'R','citation_id':'claim:rule','ranges':[[0,len(text)]],'text':text}
    result=render([make_row([entry],question)],{'unit_ids':[],'passages':[]})
    assert result['text'].split('\n',1)[0]=='P1: '+question
    assert result['text'].count(text)==1

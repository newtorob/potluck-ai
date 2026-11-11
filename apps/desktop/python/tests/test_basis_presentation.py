from potluck_local.services.investigation_part_basis_presentation import render

def test_adjacent_exact_source_ranges_join_without_bridging_gaps():
    entries=[{'unit_id':'A','citation_id':'claim:a','ranges':[[0,3]],'text':'abc'}, {'unit_id':'B','citation_id':'claim:a','ranges':[[3,6]],'text':'def'}, {'unit_id':'C','citation_id':'claim:a','ranges':[[7,10]],'text':'ghi'}, {'unit_id':'D','citation_id':'claim:b','ranges':[[10,13]],'text':'jkl'}]
    row={'id':'P1','request':'Compare?','verdict':'conflicting','evidence_unit_ids':['A','B','C','D'],'context_unit_ids':[],'requested_unit_ids':['A','B','C','D'],'dependency_only_unit_ids':[],'unit_ids':['A','B','C','D'],'passages':entries}
    result=render([row],{'unit_ids':[],'passages':[]})
    assert '[1]\nabcdef' in result['text'] and '[2]\nghi' in result['text'] and '[3]\njkl' in result['text']
    assert 'no authoritative winner' in result['text'] and result['parts']==[row]


def test_neutral_polar_heading_unknown_scope_and_distinct_identity():
    from copy import deepcopy
    entry={'unit_id':'A','citation_id':'claim:a','ranges':[[0,3]],'text':'same'}
    other={'unit_id':'B','citation_id':'claim:b','ranges':[[0,3]],'text':'same'}
    def row(id,request,verdict,answer,context,passages):
        ids=answer+context
        return {'id':id,'request':request,'verdict':verdict,'evidence_unit_ids':answer,'context_unit_ids':context,'requested_unit_ids':ids,'dependency_only_unit_ids':[],'unit_ids':ids,'passages':passages}
    parts=[row('P1','Is this allowed?','supported',['A'],[],[entry]),row('P2','Is this prohibited?','unknown',[],['B'],[other])]
    before=deepcopy(parts);r=render(parts,{'unit_ids':[],'passages':[]})
    assert 'P1: Is this allowed?\nEvidence:' in r['text'] and '— Supported' not in r['text']
    assert 'Not established by the selected reviewed statements' in r['text'] and r['text'].count('same')==2 and parts==before


def test_budget_and_identity_tamper_reject():
    import pytest
    entry={'unit_id':'A','citation_id':'claim:a','ranges':[[0,3]],'text':'abc'}
    row={'id':'P1','request':'Question?','verdict':'supported','evidence_unit_ids':['A'],'context_unit_ids':[],'requested_unit_ids':['A'],'dependency_only_unit_ids':[],'unit_ids':['A'],'passages':[entry]}
    with pytest.raises(ValueError):render([row],{'unit_ids':[],'passages':[]},max_visible=1)
    with pytest.raises(ValueError):render([row],{'unit_ids':['A'],'passages':[{**entry,'citation_id':'claim:other'}]})


def test_literal_adjacent_table_rows_coalesce_without_losing_source():
    blocks=['| Key | Value |\n','| --- | --- |\n','| A | `x|y` |\n']
    entries=[];offset=0
    for i,text in enumerate(blocks):
        entries.append({'unit_id':f'U{i}','citation_id':'claim:a','ranges':[[offset,offset+len(text)]],'text':text});offset+=len(text)
    ids=[entry['unit_id'] for entry in entries]
    row={'id':'P1','request':'What values?','verdict':'supported','evidence_unit_ids':ids,'context_unit_ids':[],'requested_unit_ids':ids,'dependency_only_unit_ids':[],'unit_ids':ids,'passages':entries}
    result=render([row],{'unit_ids':[],'passages':[]})
    assert ''.join(blocks) in result['text'] and result['text'].count('\n\n[1]\n')==1

import asyncio
from copy import deepcopy
import json

import pytest
from potluck_local.services import investigation_structural_answer as a
from potluck_local.services import investigation_answer_presentation as p
from potluck_local.services import investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope

PLAN=[{'id':'R1','operation':'answer','request':'Does exactly six months qualify?'}]
SNIPPETS=[{'snippet_id':'S1','citation_id':'C1','quote':'Requires more than six months.'}]
TRACE={'blocks':[]}
MODE={'unknown_mode':'cited-unknown-v4','presentation_mode':p.VERSION}

def response():
    return {'R1':{'groups':[{'focus':'Eligibility','status':'supported','unknown_reason':'',
        'conclusion':'No. Exactly six months does not satisfy more than six months.','snippet_ids':['S1']}],'more':False}}

def decode(data):
    return a.decode_results(json.dumps(data),PLAN,SNIPPETS,[],[],structural_trace=TRACE,**MODE)

def test_conclusions_are_distinct_from_exact_evidence_and_rebuild_without_mutation():
    brief,_,coverage=decode(response()); before=deepcopy(coverage)
    answer=brief['answers'][0]
    assert 'No. Exactly' in answer['text'] and 'Eligibility:' in answer['text']
    assert answer['source_excerpts']==SNIPPETS
    assert answer['parts'][0]['claims'][0]['text']==SNIPPETS[0]['quote']
    a.validate_original(brief,coverage,SNIPPETS,TRACE,**MODE)
    assert coverage==before

@pytest.mark.parametrize('change',['text','quote','citation','version','missing','mode','group'])
def test_changed_original_conclusion_or_source_rejects(change):
    brief,_,coverage=decode(response()); mode=MODE.copy()
    trace=coverage['cited_conclusions']; entry=trace['operations']['R1'][0]
    if change=='text': entry['text']='Yes. Exactly six months qualifies.'
    if change=='quote': entry['source_excerpts'][0]['quote']='At least six months.'
    if change=='citation': entry['source_excerpts'][0]['citation_id']='C2'
    if change=='version': trace['version']='other'
    if change=='missing': del coverage['cited_conclusions']
    if change=='mode': mode['presentation_mode']=None
    if change=='group': entry['group_index']=True
    with pytest.raises(ValueError):a.validate_original(brief,coverage,SNIPPETS,TRACE,**mode)

def test_selection_schema_and_total_text_limits_still_apply(monkeypatch):
    raw=response(); raw['R1']['groups'][0]['snippet_ids']=['S2']
    with pytest.raises(ValueError):decode(raw)
    raw=response(); raw['R1']['groups'][0]['focus']='Forged\nheading'
    with pytest.raises(ValueError):decode(raw)
    monkeypatch.setattr(a,'MAX_TEXT',350)
    with pytest.raises(ValueError,match='budget'):decode(response())

@pytest.mark.parametrize('mode,unknown', [('wrong','cited-unknown-v4'),(p.VERSION,None),(p.VERSION,'cited-unknown-v3')])
def test_invalid_modes_reject_before_runtime(mode,unknown):
    with pytest.raises(ValueError,match='presentation mode'):
        asyncio.run(inv.create_investigation(EvidenceScope('w',('c',)),'Question','query','model',
            template_version='investigation-v21',prompt_encoding='compact-calculation-v1',
            generation_profile='qwen3.8-27b-native-non-thinking',reliability_mode='bounded-evidence-v1',
            unknown_mode=unknown,presentation_mode=mode))


@pytest.mark.parametrize('version', [v for v in p.SUPPORTED_VERSIONS if v not in p.COMPONENT_VERSIONS])
def test_presentation_versions_keep_original_contracts_replayable(version):
    mode=dict(MODE,presentation_mode=version)
    brief,_,coverage=a.decode_results(json.dumps(response()),PLAN,SNIPPETS,[],[],structural_trace=TRACE,**mode)
    assert coverage['cited_conclusions']['version']==version
    a.validate_original(brief,coverage,SNIPPETS,TRACE,**mode)
    assert ('nearby passages' in p.instruction_for(version)) == (version not in (p.LEGACY_VERSION, *p.COMPLETE_VERSIONS))


def test_overlong_focus_rejected_without_trimming_in_new_mode():
    raw = response()
    raw['R1']['groups'][0]['focus'] = 'x' * 61
    with pytest.raises(ValueError, match='focus label'):
        decode(raw)
    old_mode = dict(MODE, presentation_mode=p.CONCISE_REASON_VERSION)
    brief, _, coverage = a.decode_results(json.dumps(raw), PLAN, SNIPPETS, [], [], structural_trace=TRACE, **old_mode)
    assert coverage['focus_groups']['R1']['groups'][0]['focus'] == 'x' * 61
    a.validate_original(brief, coverage, SNIPPETS, TRACE, **old_mode)

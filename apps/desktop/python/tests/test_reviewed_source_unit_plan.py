from copy import deepcopy
import pytest
from potluck_local.services.reviewed_source_unit_plan import prepare,render,text_hash

def claim(text):return [{'id':'C','text':text,'citations':[{'id':'S','revision':1}]}]
def plan(text,units):return {'C':{'source_sha256':text_hash(text),'units':units}}
def unit(id,ranges,deps=()):return {'id':id,'ranges':ranges,'required_unit_ids':list(deps)}
def test_prerequisite_exception_and_cycle_visible():
 t='Approval required.\nMembers enter.\nExcept suspended.';a=t.index('Members');b=t.index('Except');p=plan(t,[unit('pre',[[0,a]]),unit('rule',[[a,b]],['pre','exception']),unit('exception',[[b,len(t)]],['rule'])]);c=prepare(claim(t),p);r=render(c,['C:rule']);assert r['text'].replace('\n\n','\n')==t and r['full_claims']==claim(t)
def test_table_header_shared_ranges_and_unicode():
 t='| Name | Rule |\n| --- | --- |\n| café | `x | y` |\n| B | denied |\n';a=t.index('| café');b=t.index('| B |');p=plan(t,[unit('one',[[0,a],[a,b]]),unit('two',[[0,a],[b,len(t)]])]);c=prepare(claim(t),p);assert render(c,['C:one'])['text']==t[:b];assert 'denied' not in render(c,['C:one'])['text']
def test_missing_plan_whole_fallback():
 t='First.\n\nExcept second.';c=prepare(claim(t),{});assert render(c,['C:whole'])['text']==t and c['units'][0]['fallback']=='missing plan'
def test_tamper_and_invalid_plan_rejected():
 t='Alpha beta';p=plan(t,[unit('one',[[0,len(t)]])]);c=prepare(claim(t),p);assert render(c,['C:one'])==render(prepare(claim(t),p),['C:one'])
 for mutate in ('text','range','hash'):
  bad=deepcopy(c)
  if mutate=='text':bad['units'][0]['text']='invented'
  elif mutate=='range':bad['units'][0]['ranges'][0][0]=True
  else:bad['plans']['C']['source_sha256']='0'*64
  with pytest.raises(ValueError):render(bad,['C:one'])
 for bad in (plan(t,[unit('a',[[0,5]])]),plan(t,[unit('a',[[True,5]])]),plan(t,[unit('a',[[0,10]],['missing'])]),plan(t,[unit('a',[[0,10]]),unit('a',[[0,10]])])):
  with pytest.raises(ValueError):prepare(claim(t),bad)
 with pytest.raises(ValueError):render(c,['C:unknown'])
def test_caps_fail_without_clipping():
 t='x'*8001;c=prepare(claim(t),{})
 with pytest.raises(ValueError,match='visible'):render(c,['C:whole'])
 t='x'*73;p=plan(t,[unit('U'+str(i),[[i,i+1]]) for i in range(73)])
 with pytest.raises(ValueError,match='catalog'):prepare(claim(t),p)
 t='x'*17;p=plan(t,[unit('U'+str(i),[[i,i+1]],['U'+str(i-1)] if i else []) for i in range(17)]);c=prepare(claim(t),p)
 with pytest.raises(ValueError,match='closed'):render(c,['C:U16'])


def test_source_bounds_and_whitespace_only_unit_rejected():
 for claims in ([],[{'id':'C'+str(i),'text':'x','citations':['S']} for i in range(17)],claim('x'*16001)):
  with pytest.raises(ValueError):prepare(claims,{})
 t='A  B';p=plan(t,[unit('a',[[0,1]]),unit('blank',[[1,3]]),unit('b',[[3,4]])])
 with pytest.raises(ValueError,match='empty source unit'):prepare(claim(t),p)

def test_reverse_overlapping_ranges_duplicate_selection_and_unknown_plan_fail():
 t='Alpha beta'
 for ranges in ([[6,10],[0,5]],[[0,7],[6,10]]):
  with pytest.raises(ValueError):prepare(claim(t),plan(t,[unit('a',ranges)]))
 cat=prepare(claim(t),{})
 with pytest.raises(ValueError):render(cat,['C:whole','C:whole'])
 with pytest.raises(ValueError):prepare(claim(t),{'other':{'source_sha256':text_hash(t),'units':[unit('a',[[0,len(t)]])]}})

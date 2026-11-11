from copy import deepcopy
from decimal import Decimal
import pytest
from potluck_local.services import investigation_calculations as calc
from potluck_local.services import investigation_source_formulas as f


def case():
    snippets = [{'snippet_id': 'S1', 'citation_id': 'source:a', 'quote': 'weight = count * 2^(-age / half_life)'},
                {'snippet_id': 'S2', 'citation_id': 'source:a', 'quote': 'Half-life is 12 h.'}]
    quantities = calc.quantity_catalog(snippets, version='v4')
    issue = 'Compare count 8 at 12 hours and count 3 at 36 hours.'
    plan = [{'id': 'R1', 'request': 'Calculate the older weight as a percentage using the formula.', 'operation': 'percentage'}]
    catalog = f.prepare(issue, plan, snippets, quantities)
    assert [u['value'] for u in catalog['user_inputs']] == ['8', '12', '3', '36']
    def inputs(count, age):
        return [{'variable':'count','input_id':count}, {'variable':'age','input_id':age}, {'variable':'half_life','input_id':'N1'}]
    proposal = {'R1': {'formula_id': 'F1', 'part_inputs': inputs('U3','U4'), 'total_inputs': inputs('U1','U2')}}
    return issue, plan, snippets, quantities, catalog, proposal


def test_ratio_uses_source_formula_and_keeps_scenario_provenance():
    issue,plan,snippets,quantities,catalog,proposal=case()
    answers,state=f.decode(proposal,catalog,snippets,quantities)
    assert state['traces'][0]['computed']=={'part':'0.375','total':'4'}
    assert state['traces'][0]['result']=='9.375'
    assert '0.375 / 4 × 100 = 9.375%' in answers[0]['text']
    assert 'user scenario' in answers[0]['text'] and answers[0]['snippet_ids']==['S1','S2']
    assert answers[0]['source_excerpts']==snippets
    item={'issue':issue,'brief':{'answers':answers},'provenance':{'operation_plan':plan,'quantity_catalog':quantities,'formula_calculations':state}}
    f.validate_original(item,snippets)
    changed=deepcopy(item);changed['brief']['answers'][0]['text']='9.375% and an invented claim'
    with pytest.raises(ValueError):f.validate_original(changed,snippets)
    changed=deepcopy(item);changed['provenance']['formula_calculations']['traces'][0]['result']='99'
    with pytest.raises(ValueError):f.validate_original(changed,snippets)


@pytest.mark.parametrize('expression', ['__import__("os").system("id")','x.real','x[0]','[x for x in y]',
    'x if y else z','True','1e300','9**999','0**-1','1 / 0','2**0.5','2**-64', '"text"'])
def test_rejects_code_and_unbounded_or_unsupported_math(expression):
    with pytest.raises((ValueError,SyntaxError)):
        f.evaluate(expression, {n: '1' for n in ('x','y','z') if n in expression})


def test_decimal_and_precedence_are_exact():
    assert f.evaluate('x + 0.1 * 3',{'x':'0.2'}) == Decimal('0.5')
    assert f.evaluate('-2^2',{}) == -4
    assert f.evaluate('2^3^2',{}) == 512
    assert f.evaluate('x / 2',{'x':'1'}) == Decimal('0.5')


@pytest.mark.parametrize('mutation',['duplicate','unknown_id','wrong_variable','unit_mismatch','empty','extra'])
def test_rejects_invalid_bindings(mutation):
    _,_,s,q,c,p=case();value=p['R1']
    if mutation=='duplicate':value['part_inputs'][1]=deepcopy(value['part_inputs'][0])
    if mutation=='unknown_id':value['part_inputs'][0]['input_id']='U99'
    if mutation=='wrong_variable':value['part_inputs'][0]['variable']='invented'
    if mutation=='unit_mismatch':value['part_inputs'][0]['input_id']='U2'
    if mutation=='empty':value['part_inputs']=[]
    if mutation=='extra':value['extra']='payload'
    with pytest.raises(ValueError):f.decode(p,c,s,q)


def test_no_formula_request_keeps_direct_operation_path():
    _,_,s,q,_,_=case()
    assert f.prepare('What percentage?', [{'id':'R1','operation':'percentage','request':'Compare direct values.'}],s,q) is None


def test_unavailable_is_explicit_and_does_not_invent_a_value():
    _,_,s,q,c,_=case()
    a,state=f.decode({'R1':{'formula_id':'unavailable','part_inputs':[],'total_inputs':[]}},c,s,q)
    assert a[0]['status']=='unknown' and not a[0]['source_excerpts']
    assert state['traces'][0]['status']=='unknown'


def test_formula_and_quantity_tampering_are_detected():
    issue,plan,s,q,c,_=case()
    q[0]['snippet_text']='Half-life is 999 h.'
    with pytest.raises(ValueError):f.prepare(issue,plan,s,q)
    malicious=[{'snippet_id':'S1','citation_id':'a','quote':'x = __import__("os").system("id")'}]
    assert f.prepare(issue,plan,malicious,[])['formulas']==[]


def test_zero_denominator_is_a_saved_unknown_not_a_request_failure():
    _,_,s,q,c,p=case()
    c['user_inputs'][0]['value']='0'
    answers,state=f.decode(p,c,s,q)
    assert answers[0]['status']=='unknown'
    assert 'denominator is zero' in answers[0]['text']
    assert state['traces'][0]['status']=='unknown'

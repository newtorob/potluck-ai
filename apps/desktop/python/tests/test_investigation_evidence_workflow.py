import asyncio
from copy import deepcopy
import json
import pytest

from potluck_local.services import investigation_evidence_workflow as workflow
from potluck_local.services import investigation_evidence_coverage as coverage
from potluck_local.services import investigation_evidence_comparisons as sources
from potluck_local.services import investigation_evidence_comparisons_v2 as v2
from potluck_local.services import investigation_evidence_comparisons_service as bounded
from potluck_local.services import investigation_value_inventory as inventory
from potluck_local.services import investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.source_collections import scan_collection
from test_investigation_evidence_revision import sample
from test_investigation_verification import setup


def prepared(technical=False):
    envelope = sample()
    if technical:
        envelope['group'].update(request='Which software release first supported this?', focus='First software release')
        envelope['sources'][0]['quote'] = 'The protocol version is 4.2. Another rule applies.'
        envelope['sources'][0]['context'].pop('cited_chunk')
    return workflow.prepare(envelope)


def context_raw(p, verdict='source_rule'):
    return {'verdict': verdict,
            'units': {u['id']: ('answer' if i == 0 and verdict != 'not_stated' else 'background')
                      for i, u in enumerate(p['units'])}, 'dependencies': [], 'comparisons': []}


def parts():
    p = prepared(True)
    c = coverage.decode(json.dumps(context_raw(p, 'not_stated')), p)
    v = sources.prepare(p['original'])
    s = inventory.decode('{"facts":[{"unit_id":"U1","value":"4.2"}],"type_context_units":[]}', v)
    m = inventory.match_decode('{"F1":"different_fact"}', inventory.prepare_match(v, s))
    return p, c, s, m


def test_coverage_changes_only_instruction_and_version():
    p = prepared(); before = deepcopy(p)
    previous = v2.prepare(p['original'])
    old, new = v2.messages(previous), coverage.messages(p)
    a, b = json.loads(old[1]['content']), json.loads(new[1]['content'])
    a.pop('version'); b.pop('version')
    assert a == b and new[0]['content'] == old[0]['content'] + coverage.COVERAGE_INSTRUCTION
    assert coverage.response_schema(p) == v2.response_schema(previous)
    assert p == before and 'PRIVATE DRAFT' not in json.dumps(new)


@pytest.mark.parametrize('key', ['quote', 'start', 'scope'])
def test_prepared_source_tampering_rejected(key):
    p = prepared()
    if key == 'quote': p['units'][0]['quote'] = 'Forged source'
    if key == 'start': p['units'][0]['start'] = False
    if key == 'scope': p['selection_scope']['focus'] = 'Different question'
    with pytest.raises(ValueError): coverage.messages(p)


def test_composition_replays_and_preserves_source_limited_absence():
    p, c, s, m = parts(); originals = deepcopy((p, c, s, m))
    r = workflow.compose(p, c, s, m)
    assert r['version'] == workflow.VERSION and r['needs_review'] and not r['semantic_coverage_verified']
    assert r['comparisons'][0]['kind'] == 'not_requested_fact'
    assert [u['id'] for u in r['selected']] == ['U1']
    assert r['selected'][0]['quote'] == p['units'][0]['quote']
    assert r['absence_scope']['collection_wide_search_verified'] is False
    assert r['dependencies'] == c['dependencies'] and (p, c, s, m) == originals


@pytest.mark.parametrize('part', ['context', 'inventory', 'match'])
def test_tampered_stage_cannot_be_composed(part):
    p, c, s, m = parts()
    target = {'context': c, 'inventory': s, 'match': m}[part]
    target['input_sha256'] = 'different-source'
    with pytest.raises(ValueError): workflow.compose(p, c, s, m)


def test_absent_fact_conflicts_with_requested_fact_and_missing_stage():
    p, c, s, m = parts()
    with pytest.raises(ValueError): workflow.compose(p, c)
    with pytest.raises(ValueError): workflow.compose(p, c, s)
    m = inventory.match_decode('{"F1":"requested_fact"}', inventory.prepare_match(sources.prepare(p['original']), s))
    with pytest.raises(ValueError, match='conflicts'): workflow.compose(p, c, s, m)


def test_dependencies_survive_composition_and_outside_scope_is_explicit():
    p = prepared(); raw = context_raw(p)
    raw['dependencies'] = [{'unit_id': 'U1', 'requires': ['U2']}]
    c = coverage.decode(json.dumps(raw), p)
    r = workflow.compose(p, c)
    assert r['dependencies'] == raw['dependencies']
    assert r['classifications']['U2'] == 'dependency'
    assert r['comparison_skip_reason'] == 'outside_scope'
    with pytest.raises(ValueError): workflow.compose(p, c, {})


class Runtime:
    def __init__(self, mode='ok', empty=False):
        self.mode = mode; self.empty = empty; self.calls = []; self.entered = asyncio.Event()
    def current_model_id(self):
        return 'changed' if self.mode == 'changed' and self.calls else 'model'
    def current_model_sha256(self): return 'sha'
    async def validate_investigation_profile(self, *a, **kw): pass
    async def measure_investigation_prompt(self, *a, **kw):
        return {'input_tokens': 1000, 'context_tokens': 8192 if self.mode != 'capacity' else 1200}
    async def chat(self, messages, **kwargs):
        payload = json.loads(messages[1]['content']); self.calls.append((payload, kwargs))
        self.entered.set()
        if self.mode == 'cancel': await asyncio.Event().wait()
        if self.mode == 'slow': await asyncio.sleep(0.05)
        version = payload['version']
        if version == coverage.VERSION:
            technical = 'release' in payload['question']
            raw = {'verdict': 'not_stated' if technical else 'source_rule',
                   'units': {u['id']: ('answer' if i == 0 and not technical else 'background')
                             for i, u in enumerate(payload['units'])}, 'dependencies': [], 'comparisons': []}
        elif version == inventory.VERSION:
            raw = {'facts': [] if self.empty else [{'unit_id': 'U1', 'value': '4.2'}], 'type_context_units': []}
            if self.mode == 'bad_inventory': raw['facts'][0]['value'] = '9.9'
        else:
            raw = {'F1': 'different_fact'}
            if self.mode == 'bad_match': raw = {}
        return {'choices': [{'finish_reason': 'length' if self.mode == 'length' else 'stop',
                             'message': {'content': json.dumps(raw)}}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(('technical','empty','calls','skip'), [(False,False,1,'outside_scope'),(True,True,2,'empty_inventory'),(True,False,3,None)])
async def test_joined_workflow_and_exact_call_accounting(technical, empty, calls, skip):
    p = prepared(technical); before = deepcopy(p); runtime = Runtime(empty=empty)
    r = await workflow.generate(p, runtime, model='model', model_sha='sha')
    assert r['status'] == 'proposal' and r['generation_calls'] == calls == len(runtime.calls)
    assert sum(s['generation_calls'] for s in r['stages']) == calls
    assert r['result']['comparison_skip_reason'] == skip and r['failed_stage'] is None
    assert all(s['settings']['timeout_seconds'] <= 180 for s in r['stages'])
    assert r['elapsed_ms'] >= 0 and p == before


@pytest.mark.asyncio
@pytest.mark.parametrize(('mode','stage','calls'), [('bad_inventory','inventory',2),('bad_match','match',3),('capacity','context',0),('changed','context',1),('length','context',1)])
async def test_failed_required_stage_never_returns_partial_proposal(mode, stage, calls):
    runtime = Runtime(mode); r = await workflow.generate(prepared(True), runtime, model='model', model_sha='sha')
    assert r['status'] == 'not_generated' and r['failed_stage'] == stage and 'result' not in r
    assert r['generation_calls'] == calls == len(runtime.calls) and r['reason']


@pytest.mark.asyncio
async def test_deadline_and_cancellation(monkeypatch):
    runtime = Runtime('cancel')
    task = asyncio.create_task(workflow.generate(prepared(True), runtime, model='model', model_sha='sha'))
    await runtime.entered.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    monkeypatch.setattr(workflow, 'TIMEOUT_SECONDS', 0.01)
    r = await workflow.generate(prepared(True), Runtime('slow'), model='model', model_sha='sha')
    assert r['reason'] == 'timeout' and r['generation_calls'] == 1 and len(r['stages']) == 1
    assert r['stages'][0]['settings']['timeout_seconds'] <= .01


@pytest.mark.asyncio
async def test_concurrent_workflows_do_not_replace_legacy_contract():
    old = bounded.contract
    a, b = await asyncio.gather(workflow.generate(prepared(), Runtime(), model='model', model_sha='sha'),
                                workflow.generate(prepared(True), Runtime(), model='model', model_sha='sha'))
    assert a['generation_calls'] == 1 and b['generation_calls'] == 3 and bounded.contract is old


@pytest.mark.asyncio
@pytest.mark.parametrize('race', ['none', 'review', 'rescan'])
async def test_real_scoped_draft_stays_detached_and_rechecks_changes(setup, race):
    runtime, scope, original, path = await setup()
    with pytest.raises(LookupError): await workflow.propose(EvidenceScope('other', scope.collection_ids), original['id'], 'G1')
    assert runtime.calls == []
    async def chat(messages, **kwargs):
        runtime.calls.append((messages, kwargs)); payload = json.loads(messages[1]['content'])
        if race == 'review': await inv.review_investigation(scope, original['id'], 'accept', 'owner', 'Reviewed.')
        if race == 'rescan':
            path.write_text('# Changed\n\nDifferent source.\n'); await scan_collection('w', scope.collection_ids[0])
        raw = {'verdict': 'source_rule', 'units': {u['id']: ('answer' if i == 0 else 'background')
               for i, u in enumerate(payload['units'])}, 'dependencies': [], 'comparisons': []}
        return {'choices': [{'finish_reason':'stop','message':{'content':json.dumps(raw)}}]}
    runtime.chat = chat
    if race == 'none':
        r = await workflow.propose(scope, original['id'], 'G1')
        assert r['attempt']['status'] == 'proposal'
        assert await inv.get_investigation(scope, original['id']) == original
    else:
        with pytest.raises((ValueError, LookupError)): await workflow.propose(scope, original['id'], 'G1')
    assert len(runtime.calls) == 1

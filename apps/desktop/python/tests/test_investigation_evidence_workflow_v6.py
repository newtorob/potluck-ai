import json
from copy import deepcopy

import pytest

from potluck_local.services import investigation_evidence_workflow as workflow
from potluck_local.services import investigation_evidence_comparisons as sources
from potluck_local.services import investigation_evidence_coverage_v3 as coverage
from potluck_local.services import investigation_value_inventory_v2 as old_v2
from potluck_local.services import investigation_value_inventory_v3 as old_v3
from potluck_local.services import investigation_value_inventory_v4 as inventory
from test_investigation_evidence_workflow import Runtime
from test_investigation_evidence_workflow_v4 import prepared as v4_prepared


def parts():
    envelope = v4_prepared()['context']['original']
    p = workflow.prepare(envelope, version=workflow.V6_VERSION)
    ctx = p['context']
    raw = {'verdict': 'not_stated', 'units': {u['id']: 'background' for u in ctx['units']},
           'dependencies': [], 'comparisons': []}
    c = coverage.decode(json.dumps(raw), ctx)
    source = sources.prepare(ctx['original'])
    scan = inventory.decode('{"classifications":{"C1":"protocol_version"},"type_context_units":[]}', source)
    return p, c, source, scan


def test_v6_explicit_dispatch_and_prior_versions_unchanged():
    envelope = v4_prepared()['context']['original']
    assert workflow.prepare(envelope) == workflow.prepare(envelope, version=workflow.VERSION)
    assert workflow.prepare(envelope) == workflow.coverage.prepare(envelope)
    for version, scanner in [(workflow.VERSION, workflow.inventory), (workflow.V2_VERSION, workflow.inventory),
                             (workflow.V3_VERSION, workflow.inventory), (workflow.V4_VERSION, old_v2),
                             (workflow.V5_VERSION, old_v3), (workflow.V6_VERSION, inventory)]:
        p = workflow.prepare(envelope, version=version)
        actual, _ = workflow._prepared_contract(p)
        assert actual == version and workflow._inventory_contract(actual) is scanner
    assert workflow.prepare(envelope, version=workflow.V6_VERSION)['version'] == workflow.V6_INPUT_VERSION
    assert workflow.prepare(envelope, version=workflow.V5_VERSION)['version'] == workflow.V5_INPUT_VERSION


@pytest.mark.parametrize('old', [old_v2, old_v3])
def test_v6_rejects_cross_version_inventory(old):
    p, c, source, _ = parts()
    scan = old.decode('{"classifications":{"C1":"protocol_version"},"type_context_units":[]}', source)
    with pytest.raises(ValueError): workflow.compose(p, c, scan)


def test_all_requested_facts_retained_without_invented_comparison():
    p, _, source, scan = parts(); ctx = p['context']
    raw = {'verdict': 'source_rule', 'units': {u['id']: ('answer' if u['id'] == 'U1' else 'background') for u in ctx['units']}, 'dependencies': [], 'comparisons': []}
    c = coverage.decode(json.dumps(raw), ctx)
    matched = inventory.match_decode('{"F1":"requested_fact"}', inventory.prepare_match(source, scan))
    result = workflow.compose(p, c, scan, matched)
    assert matched['facts'] == scan['facts'] and matched['comparison_kind'] == 'none'
    assert result['comparisons'] == [] and result['selected'][0]['quote'] == source['units'][0]['quote']


class InvalidRuntime(Runtime):
    async def chat(self, messages, **kwargs):
        payload = json.loads(messages[1]['content']); self.calls.append((payload, kwargs))
        if payload['version'] == coverage.VERSION:
            raw = {'verdict': 'not_stated', 'units': {u['id']: 'background' for u in payload['units']}, 'dependencies': [], 'comparisons': []}
        elif payload['version'] == inventory.VERSION:
            raw = {'classifications': {'C1': 'protocol_version'}, 'type_context_units': []}
        else:
            raw = {'F1': 'invalid_fact'}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(raw)}}]}


@pytest.mark.asyncio
async def test_invalid_fact_rejects_entire_workflow_without_retry():
    p, _, _, _ = parts(); before = deepcopy(p); runtime = InvalidRuntime()
    result = await workflow.generate(p, runtime, model='model', model_sha='sha')
    assert result['status'] == 'not_generated' and result['failed_stage'] == 'match'
    assert result['generation_calls'] == len(runtime.calls) == 3 and 'result' not in result
    assert p == before

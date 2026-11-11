import json
from copy import deepcopy

import pytest

from potluck_local.services import investigation_evidence_workflow as workflow
from potluck_local.services import investigation_evidence_comparisons as sources
from potluck_local.services import investigation_evidence_coverage_v3 as coverage
from potluck_local.services import investigation_value_inventory_v4 as old_inventory
from potluck_local.services import investigation_value_inventory_v5 as inventory
from test_investigation_evidence_revision import sample
from test_investigation_evidence_workflow import Runtime


def parts():
    envelope = sample(); envelope['group'].update(request='Which software release?', focus='Which software release?')
    text = 'Protocol revision 7 is enabled.\n\nThe protocol revision identifies the wire format.\n\nThe product release is not stated.'
    envelope['sources'][0]['quote'] = text
    envelope['sources'][0]['context']['cited_chunk'] = {'quote': text, 'selected_start': 0, 'selected_end': len(text)}
    p = workflow.prepare(envelope, version=workflow.V7_VERSION); ctx = p['context']; source = sources.prepare(ctx['original'])
    raw = {'verdict': 'not_stated', 'units': {u['id']: 'background' for u in ctx['units']}, 'dependencies': [], 'comparisons': []}
    c = coverage.decode(json.dumps(raw), ctx)
    scan = inventory.decode(json.dumps({'classifications': {'C1': 'protocol_version'}, 'context_classifications': {'U2': 'typed_value_definition', 'U3': 'explicit_typed_absence'}}), source)
    matched = inventory.match_decode('{"F1":"different_fact"}', inventory.prepare_match(source, scan))
    return p, c, source, scan, matched


def test_v7_dispatch_and_prior_defaults():
    p, _, _, _, _ = parts(); envelope = p['context']['original']
    assert workflow.prepare(envelope) == workflow.prepare(envelope, version=workflow.VERSION) == workflow.coverage.prepare(envelope)
    for version in (workflow.VERSION, workflow.V2_VERSION, workflow.V3_VERSION, workflow.V4_VERSION, workflow.V5_VERSION, workflow.V6_VERSION, workflow.V7_VERSION):
        prepared = workflow.prepare(envelope, version=version)
        assert workflow._prepared_contract(prepared)[0] == version
    assert p['version'] == workflow.V7_INPUT_VERSION
    assert workflow._inventory_contract(workflow.V7_VERSION) is inventory
    assert workflow._inventory_contract(workflow.V6_VERSION) is old_inventory


def test_v7_rejects_old_inventory_and_tampered_context_replay():
    p, c, source, scan, matched = parts()
    old = old_inventory.decode('{"classifications":{"C1":"protocol_version"},"type_context_units":["U2","U3"]}', source)
    with pytest.raises(ValueError): workflow.compose(p, c, old, matched)
    bad = deepcopy(scan); bad['context_classifications']['U3'] = 'background'
    with pytest.raises(ValueError): workflow.compose(p, c, bad, matched)


def test_v7_composes_definition_and_absence_exactly_and_rejects_bad_bounds():
    p, c, source, scan, matched = parts(); result = workflow.compose(p, c, scan, matched)
    assert scan['type_context_units'] == matched['type_context_units'] == ['U2', 'U3']
    selected = {u['id']: u for u in result['selected']}
    assert all(selected[u]['quote'] == next(x['quote'] for x in source['units'] if x['id'] == u) for u in ('U1', 'U2', 'U3'))
    bad = deepcopy(scan); bad['selected'][1]['start'] = -1
    with pytest.raises(ValueError): workflow.compose(p, c, bad, matched)


class MissingContextRuntime(Runtime):
    async def chat(self, messages, **kwargs):
        payload = json.loads(messages[1]['content']); self.calls.append((payload, kwargs))
        if payload['version'] == coverage.VERSION:
            raw = {'verdict': 'not_stated', 'units': {u['id']: 'background' for u in payload['units']}, 'dependencies': [], 'comparisons': []}
        else:
            raw = {'classifications': {'C1': 'protocol_version'}, 'context_classifications': {'U2': 'typed_value_definition'}}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(raw)}}]}


@pytest.mark.asyncio
async def test_missing_source_context_decision_fails_without_partial_result():
    p, _, _, _, _ = parts(); runtime = MissingContextRuntime()
    result = await workflow.generate(p, runtime, model='model', model_sha='sha')
    assert result['status'] == 'not_generated' and result['failed_stage'] == 'inventory'
    assert result['generation_calls'] == len(runtime.calls) == 2 and 'result' not in result

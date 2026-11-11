import json
import pytest
from potluck_local.services import investigation_evidence_workflow as workflow
from potluck_local.services import investigation_evidence_coverage_v3 as coverage
from test_investigation_evidence_workflow import prepared, Runtime


class V3Runtime(Runtime):
    async def chat(self, messages, **kwargs):
        payload = json.loads(messages[1]['content'])
        if payload['version'] == coverage.VERSION:
            self.calls.append((payload, kwargs))
            technical = 'release' in payload['question']
            result = {'verdict': 'not_stated' if technical else 'source_rule', 'units': {u['id']: ('answer' if i == 0 and not technical else 'background')
                      for i, u in enumerate(payload['units'])}, 'dependencies': [], 'comparisons': []}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}
        return await super().chat(messages, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize('technical', [False, True])
async def test_v3_opt_in_stages_preserve_version_and_source_binding(technical):
    original = prepared(technical); p = workflow.prepare(original['original'], version=workflow.V3_VERSION)
    assert workflow.prepare(original['original']) == original
    runtime = V3Runtime(); attempt = await workflow.generate(p, runtime, model='model', model_sha='sha')
    assert attempt['status'] == 'proposal'
    assert attempt['version'] == attempt['result']['version'] == workflow.V3_VERSION
    assert attempt['result']['component_versions']['context'] == coverage.VERSION
    assert attempt['result']['needs_review'] and attempt['result']['semantic_coverage_verified'] is False
    assert attempt['generation_calls'] == (3 if technical else 1)
    assert workflow.compose(p, attempt['stages'][0]['result'],
        *(stage['result'] for stage in attempt['stages'][1:])) == attempt['result']

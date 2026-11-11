import asyncio
from copy import deepcopy
import json

import pytest

from potluck_local.services import investigation_draft_verification_v2 as v2
from potluck_local.services import investigation_verification as service
from potluck_local.services import investigations as inv
from test_investigation_draft_verification import draft
from test_investigation_verification import setup  # noqa: F401


def clear(envelope):
    return {'draft_omissions': {x['id']: {'verdict': 'no_omission', 'omitted_quote': '', 'reason': ''}
                               for x in envelope['source_checks']},
            'claim_support': {x['id']: {'verdict': 'supported', 'reason': ''} for x in envelope['claims']},
            'draft_wording': {x['id']: {'verdict': 'clear', 'reason': ''} for x in envelope['groups']}}


def test_group_context_preserves_every_draft_part_and_whole_source():
    item = draft(); before = deepcopy(item); envelope = v2.prepare(item)
    assert item == before and envelope['version'] == v2.VERSION
    texts = [c['text'] for c in envelope['claims']]
    assert envelope['groups'][0]['draft_sentences'] == texts
    assert all(s['draft_sentences'] == texts for s in envelope['source_checks'])
    assert [s['quote'] for s in envelope['source_checks']] == [s['quote'] for s in item['provenance']['snippet_catalog']]
    assert v2.decode(json.dumps(clear(envelope)), envelope)['status'] == 'no_issue_found'


def test_omission_finding_is_bound_to_its_own_source():
    envelope = v2.prepare(draft()); response = clear(envelope)
    row = response['draft_omissions']['Q1']; row.update(verdict='omitted_from_draft',
        omitted_quote='Managers may make exceptions.', reason='The draft omits manager discretion.')
    assert v2.decode(json.dumps(response), envelope)['status'] == 'needs_review'
    for quote in ['', 'Parking is exempt below $10.', '   ']:
        row['omitted_quote'] = quote
        with pytest.raises(ValueError): v2.decode(json.dumps(response), envelope)


@pytest.mark.parametrize('mutation', [
    lambda x: x['draft_omissions']['Q1'].update(reason='Unnecessary prose.'),
    lambda x: x['draft_omissions']['Q1'].update(omitted_quote='Receipts are required.'),
    lambda x: x['claim_support']['C1'].update(verdict='uncertain', reason=''),
    lambda x: x['claim_support']['C1'].update(verdict='unsupported', reason='This explanation is cut'),
    lambda x: x['draft_wording']['G1'].update(verdict='unclear', reason='x'*120+'.'),
    lambda x: x['draft_wording']['G1'].update(verdict='unclear', reason='Hidden\nheading.'),
    lambda x: x['claim_support'].pop('C1'),
    lambda x: x.update(extra={}),
])
def test_inventory_and_finding_bounds_reject(mutation):
    envelope = v2.prepare(draft()); response = clear(envelope); mutation(response)
    with pytest.raises(ValueError): v2.decode(json.dumps(response), envelope)


def test_presentation_finding_cannot_be_hidden_by_supported_claims():
    envelope = v2.prepare(draft()); response = clear(envelope)
    response['draft_wording']['G1'].update(verdict='unclear', reason='The answer is a noun fragment.')
    assert v2.decode(json.dumps(response), envelope)['status'] == 'needs_review'
    import jsonschema
    jsonschema.validate(response, v2.response_schema(envelope))


def test_mixed_versions_survive_review_and_export_without_reinterpreting_v1(setup):
    async def run():
        runtime, scope, item, _ = await setup()
        first = await service.verify_investigation(scope, item['id'])
        original_report = deepcopy(first['provenance']['draft_verifications'][0])
        runtime.mode = 'omission'
        second = await service.verify_investigation(scope, item['id'], version=v2.VERSION)
        reports = service.validate_reports(second)
        assert reports[0] == original_report and reports[1]['result']['version'] == v2.VERSION
        assert reports[1]['result']['status'] == 'needs_review' and second['brief'] == item['brief']
        reviewed = await inv.review_investigation(scope, item['id'], 'accept', 'owner', 'Fixture only.')
        assert service.validate_reports(reviewed) == reports
        from potluck_local.routers.investigations import export
        markdown = (await export(item['id'], scope.workspace, list(scope.collection_ids)))['markdown']
        assert 'Managers may deny reimbursement' in markdown and 'Local draft checks (unverified)' in markdown
        forged = deepcopy(second); forged['provenance']['draft_verifications'][0]['input']['version'] = v2.VERSION
        with pytest.raises(ValueError): service.validate_reports(forged)
    asyncio.run(run())


@pytest.mark.parametrize('mode,reason', [('capacity','capacity'),('timeout','timeout'),('malformed','invalid_output'),
                                      ('truncated','invalid_output'),('transport','inference_failed')])
def test_v2_failures_keep_their_version_and_original_draft(setup, mode, reason):
    async def run():
        runtime, scope, item, _ = await setup(); runtime.mode = mode
        checked = await service.verify_investigation(scope, item['id'], version=v2.VERSION)
        result = service.validate_reports(checked)[0]['result']
        assert result['version'] == v2.VERSION and result['reason'] == reason and result['status'] == 'not_checked'
        assert checked['brief'] == item['brief'] and checked['status'] == 'draft'
    asyncio.run(run())


def test_explicit_http_opt_in_and_unknown_version_reject(setup):
    async def run():
        import httpx
        from fastapi import FastAPI
        from potluck_local.routers.investigations import router
        runtime, scope, item, _ = await setup(); app = FastAPI(); app.include_router(router)
        payload = {'workspace': scope.workspace, 'collection_ids': list(scope.collection_ids), 'version': v2.VERSION}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            url = '/company-brain/investigations/' + item['id'] + '/draft-verification'
            assert (await client.post(url, json={**payload, 'version': 'unknown'})).status_code == 422
            result = await client.post(url, json=payload)
            assert result.status_code == 200, result.text
            assert result.json()['provenance']['draft_verifications'][0]['result']['version'] == v2.VERSION
            assert len(runtime.calls) == 1
    asyncio.run(run())

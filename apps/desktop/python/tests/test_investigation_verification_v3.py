import asyncio
from copy import deepcopy
import json

import pytest

from potluck_local.services import investigation_draft_verification_v3 as v3
from potluck_local.services import investigation_verification as service
from potluck_local.services import investigations as inv
from test_investigation_draft_verification import draft
from test_investigation_verification import setup  # noqa: F401


def with_context():
    item = draft()
    item['evidence'] = [{'kind': 'source_chunk', 'citation_id': s['citation_id'],
        'heading': 'Expenses', 'presented_content': s['quote']} for s in item['provenance']['snippet_catalog']]
    item['provenance']['structural_trace'] = {'offsets': {s['snippet_id']: {
        'citation_id': s['citation_id'], 'start': 0, 'end': len(s['quote'])}
        for s in item['provenance']['snippet_catalog']}}
    return item


def clear(envelope):
    return {key: {x['id']: {'verdict': verdict, 'reason': ''} for x in envelope[field]}
            for key, field, verdict in [('clause_coverage', 'clauses', 'covered'),
                                       ('claim_support', 'claims', 'supported'),
                                       ('draft_wording', 'groups', 'clear')]}


@pytest.mark.parametrize('text', [
    'Receipts required. Managers may deny claims. All reports need approval.',
    'Cost is $1.20. Approval follows.', 'Ask Dr. Smith. Approval follows.',
    '1. Submit expenses. Managers review them.', 'Protocol v1.2 uses CLI 0.3.',
    '- [Read this](https://example.test/a.b)\n', '```\nvalue = 1.0\n```\n',
    '\nFirst sentence.\nSecond sentence.\n',
])
def test_units_preserve_every_original_character(text):
    spans = v3.source_units(text)
    assert ''.join(text[a:b] for a, b in spans) == text
    assert all(a < b for a, b in spans)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    if 'Dr.' in text: assert text[slice(*spans[0])] == 'Ask Dr. Smith. '
    if text.startswith('1.'): assert text[slice(*spans[0])] == '1. Submit expenses. '


def test_exact_own_source_inventory_prevents_quote_copying():
    item = with_context(); before = deepcopy(item); envelope = v3.prepare(item)
    assert item == before
    assert [c['text'] for c in envelope['clauses']] == [
        'Receipts are required. ', 'Managers may make exceptions.', 'Parking is exempt below $10.']
    response = clear(envelope)
    response['clause_coverage']['Q1.2'].update(verdict='material_omission', reason='The draft omits discretion.')
    result = v3.decode(json.dumps(response), envelope)
    assert result['status'] == 'needs_review'
    import jsonschema
    jsonschema.validate(response, v3.response_schema(envelope))
    response['clause_coverage']['Q1.2']['omitted_quote'] = 'Parking is exempt below $10.'
    with pytest.raises(ValueError): v3.decode(json.dumps(response), envelope)
    with pytest.raises(jsonschema.ValidationError): jsonschema.validate(response, v3.response_schema(envelope))


def test_source_context_is_bound_to_same_citation_offsets_and_exact_list_introduction():
    item = with_context(); s = item['provenance']['snippet_catalog'][0]
    s['quote'] = '- Conference travel\n'
    item['provenance']['answer_coverage']['cited_conclusions']['operations']['R1'][0]['source_excerpts'][0] = deepcopy(s)
    prefix = 'Unrelated text.\n\nThis budget covers:\n\n- Books\n'
    item['evidence'][0].update(presented_content=prefix+s['quote'], heading='Training budget')
    offset = item['provenance']['structural_trace']['offsets']['S1']
    offset.update(start=len(prefix), end=len(prefix+s['quote']))
    envelope = v3.prepare(item); context = envelope['source_checks'][0]['context']
    intro = context['list_introduction']
    assert intro['quote'] == 'This budget covers:\n'
    assert item['evidence'][0]['presented_content'][intro['start']:intro['end']] == intro['quote']
    assert context['heading'] == 'Training budget' and context['citation_id'] == 'C1'
    offset['start'] -= 1
    with pytest.raises(ValueError, match='offsets'): v3.prepare(item)


@pytest.mark.parametrize('heading,codes', [
    ('Budget (employees)', []), ('Budget (employees', ['heading_unbalanced_brackets']),
    ('Budget... (', ['heading_ellipsis', 'heading_unbalanced_brackets']),
    ('Budget ([)]', ['heading_unbalanced_brackets']), ('3.5% allowance', []),
])
def test_mechanical_warnings_cannot_be_cleared_by_model(heading, codes):
    envelope = v3.prepare(with_context()); envelope['groups'][0]['focus'] = heading
    result = v3.decode(json.dumps(clear(envelope)), envelope)
    assert [w['code'] for w in result['mechanical_warnings']] == codes
    assert result['status'] == ('needs_review' if codes else 'no_issue_found')


@pytest.mark.parametrize('mutation', [
    lambda r: r['clause_coverage'].pop('Q1.2'),
    lambda r: r['clause_coverage'].update({'Q2.2': {'verdict': 'covered', 'reason': ''}}),
    lambda r: r['clause_coverage']['Q1.2'].update(verdict='material_omission', reason=''),
    lambda r: r['clause_coverage']['Q1.2'].update(verdict='not_relevant', reason='Unrequested reason.'),
    lambda r: r['draft_wording']['G1'].update(verdict='unclear', reason='Clipped reason'),
    lambda r: r.update(mechanical_warnings=[]),
])
def test_invalid_or_forged_inventory_rejected(mutation):
    envelope = v3.prepare(with_context()); response = clear(envelope); mutation(response)
    with pytest.raises(ValueError): v3.decode(json.dumps(response), envelope)


def test_all_versions_survive_correction_export_and_tamper_checks(setup):
    async def run():
        runtime, scope, item, _ = await setup()
        first = await service.verify_investigation(scope, item['id'])
        second = await service.verify_investigation(scope, item['id'], version='draft-verification-v2')
        runtime.mode = 'omission'
        third = await service.verify_investigation(scope, item['id'], version=v3.VERSION)
        reports = service.validate_reports(third)
        assert reports[:2] == second['provenance']['draft_verifications']
        assert reports[0] == first['provenance']['draft_verifications'][0]
        assert reports[2]['result']['status'] == 'needs_review'
        assert third['brief'] == item['brief'] and third['status'] == 'draft'
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'fixture', 'Test only.', item['brief'])
        assert service.validate_reports(reviewed) == reports
        from potluck_local.routers.investigations import export
        markdown = (await export(item['id'], scope.workspace, list(scope.collection_ids)))['markdown']
        assert 'Omitted source unit' in markdown and 'Managers may deny reimbursement' in markdown
        for kind in ('heading', 'offset', 'warning'):
            forged = deepcopy(third); report = forged['provenance']['draft_verifications'][2]
            if kind == 'heading': report['input']['source_checks'][0]['context']['heading'] = 'Invented'
            if kind == 'offset': report['input']['clauses'][0]['start'] = 1
            if kind == 'warning': report['result']['mechanical_warnings'] = [{'group_id': 'G1', 'code': 'invented'}]
            report['input_sha256'] = v3.digest(report['input']); report['result_sha256'] = v3.digest(report['result'])
            with pytest.raises(ValueError): service.validate_reports(forged)
    asyncio.run(run())


@pytest.mark.parametrize('mode,reason', [('capacity','capacity'),('timeout','timeout'),('malformed','invalid_output'),
                                      ('truncated','invalid_output'),('transport','inference_failed')])
def test_v3_failure_keeps_original_and_deterministic_warning_contract(setup, mode, reason):
    async def run():
        runtime, scope, item, _ = await setup(); runtime.mode = mode
        checked = await service.verify_investigation(scope, item['id'], version=v3.VERSION)
        result = service.validate_reports(checked)[0]['result']
        assert result == {'version': v3.VERSION, 'status': 'not_checked', 'reason': reason,
                          'scope': v3.SCOPE, 'mechanical_warnings': []}
        assert checked['brief'] == item['brief'] and checked['status'] == 'draft'
    asyncio.run(run())

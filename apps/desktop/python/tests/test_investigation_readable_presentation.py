"""Display-only compaction preserves conditions, authority and original output."""
from copy import deepcopy
import re
import pytest
from potluck_local.services import investigation_readable_presentation as view

PREFIX = '# Internal draft\n\nDraft June 26. Approval required unless emergency work is authorized.\n\n'
HEADER = '| Record | Deadline | Enforcement |\n|---|---|---|\n'
ROW = '| Payload | 30 days after fulfilment, not uninstall. | TODO; retain opt-outs. `a|b` and A \\| B |'


def fixture():
    bodies = [PREFIX + 'Deletion after 48 hours excludes residual requests.', PREFIX + ROW,
              PREFIX + 'Cleanup can retain email indefinitely. Do not attest enforcement.']
    evidence, passages = [], []
    for i, body in enumerate(bodies, 1):
        cid = f'claim:{i}'
        evidence.append({'kind': 'reviewed_claim', 'citation_id': cid, 'sources': [
            {'collection_id': 'w', 'document_id': 'doc', 'revision_id': 'rev',
             'content': HEADER + ROW if i == 2 else body}]})
        passages.append({'unit_id': f'C{i}:whole', 'citation_id': cid, 'text': body})
    text = '\n\n'.join([f'P{i}: Question {i}\nEvidence: [{i}]' for i in (1, 2, 3)] +
                        [f'[{i}]\n{body}' for i, body in enumerate(bodies, 1)])
    answer = {'question_id': 'R1', 'status': 'supported', 'text': text, 'snippet_ids': ['S1', 'S2', 'S3'],
              'source_excerpts': [{'snippet_id': 'S1', 'citation_id': 'claim:1', 'quote': bodies[0]}],
              'passage_presentation': {'version': 'fixed-part-selection-v32', 'answer': text, 'passages': passages}}
    return {'issue': 'What happens?', 'brief': {'answers': [answer]}, 'evidence': evidence,
            'review': None, 'id': 'investigation', 'model_slug': 'model'}, answer, evidence


def test_shared_context_conditions_and_citations_survive_with_original_unchanged():
    item, answer, evidence = fixture(); original = deepcopy(item)
    value = view.project(answer, evidence)
    assert value['shared_context']
    assert value['markdown'].count('Approval required unless emergency work is authorized.') == 1
    assert 'Applies to evidence [1], [2], [3].' in value['markdown']
    for phrase in ('48 hours excludes residual requests', '30 days after fulfilment, not uninstall',
                   'TODO; retain opt-outs', 'email indefinitely', 'Do not attest enforcement'):
        assert phrase in value['markdown']
    assert [x['citation_id'] for x in value['source_groups']] == ['claim:1', 'claim:2', 'claim:3']
    assert item == original and value['original_text'] == answer['text']
    assert view.decorate(item)['brief'] == original['brief']


def test_source_table_labels_keep_escaped_and_code_pipes():
    _, answer, evidence = fixture(); value = view.project(answer, evidence)['markdown']
    assert '- **Deadline:** 30 days after fulfilment, not uninstall.' in value
    assert '- **Enforcement:** TODO; retain opt-outs. `a|b` and A \\| B' in value
    assert len(view.cells(ROW)) == 3
    assert view.cells('| Broken | `unclosed|code |') is None


@pytest.mark.parametrize('key', ['revision_id', 'document_id'])
def test_identical_words_do_not_combine_different_source_authority(key):
    _, answer, evidence = fixture(); evidence[2]['sources'][0][key] = 'other'
    value = view.project(answer, evidence)
    assert not value['shared_context'] and value['markdown'].count('Draft June 26.') == 3


@pytest.mark.parametrize('ambiguous', [False, True])
def test_missing_or_ambiguous_header_keeps_row_without_guessing(ambiguous):
    _, answer, evidence = fixture()
    if ambiguous:
        evidence[1]['sources'].append({**evidence[1]['sources'][0],
            'content': HEADER.replace('Deadline', 'Other clock') + ROW})
    else:
        evidence[1]['sources'][0]['content'] = 'No matching source row.'
    value = view.project(answer, evidence)['markdown']
    assert ROW in value and '- **Deadline:**' not in value and '- **Other clock:**' not in value


@pytest.mark.parametrize('body', ['```text\n' + ROW + '\n```', HEADER + ROW])
def test_fenced_code_and_complete_tables_are_not_reinterpreted(body):
    _, answer, evidence = fixture()
    answer['text'] = answer['text'].replace(PREFIX + ROW, PREFIX + body)
    answer['passage_presentation']['passages'][1]['text'] = PREFIX + body
    assert body in view.project(answer, evidence)['markdown']


@pytest.mark.parametrize('lead', [
    'The selected statements do not establish P1: Question 1\nEvidence: [1]',
    'Unresolved incompatible alternatives for P1: Question 1. No authoritative winner.\nEvidence: [1]',
])
def test_unknown_and_conflict_scope_leads_survive(lead):
    _, answer, evidence = fixture()
    answer['text'] = answer['text'].replace('P1: Question 1\nEvidence: [1]', lead)
    assert lead in view.project(answer, evidence)['markdown']


def test_ambiguous_mapping_missing_authority_or_older_contract_falls_back():
    _, answer, evidence = fixture()
    bad = deepcopy(answer); bad['passage_presentation']['passages'][1]['text'] = 'unmatched'
    assert view.project(bad, evidence) is None and view.project(answer, evidence[:1]) is None
    answer['passage_presentation']['version'] = 'fixed-part-selection-v28'
    assert view.project(answer, evidence) is None


def test_export_preserves_original_excerpts_and_defers_human_corrections():
    item, answer, _ = fixture()
    escape = lambda s: re.sub(r'([\\`*_{}\[\]()#+.!<>|~-])', r'\\\1', str(s))
    markdown = view.export(item, escape)
    assert answer['text'] in markdown and answer['source_excerpts'][0]['quote'] in markdown
    assert 'claim:1' in markdown and '**Deadline:**' in markdown and 'P1: Question 1\nEvidence:' in markdown
    item['review'] = {'decision': 'correct'}
    assert view.export(item, escape) is None


def test_actual_service_get_review_and_both_export_layouts(tmp_path, monkeypatch):
    import asyncio
    from test_investigation_single_review_v28 import wire
    import test_investigation_fixed_part_answers_service as shared
    from potluck_local.services import investigations as inv
    from potluck_local.routers import company_brain_pilot as pilot
    class Runtime(shared.FixedRuntime):
        async def chat(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            return {'choices': [{'message': {'content': wire('supported', ['C1:whole'])}, 'finish_reason': 'stop'}]}
    async def run():
        _, scope, *_ = await shared.setup(tmp_path, monkeypatch, approve_plan=False)
        monkeypatch.setattr(pilot, '_privacy', lambda: None)
        runtime = Runtime(); monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        item = await pilot.ask_reviewed(pilot.AskReviewed(workspace=scope.workspace,
            collection_ids=list(scope.collection_ids), question=shared.QUESTION, model_slug='model',
            pilot_confirmed=True, question_parts=['What is required?', 'Which expiry?'], question_parts_confirmed=True))
        assert 'readable_answers' in item and len(runtime.calls) == 2
        reopened = await shared.routes.get(item['id'], scope.workspace, list(scope.collection_ids))
        assert reopened['brief'] == item['brief']
        assert (await inv.get_investigation(scope, item['id']))['brief'] == item['brief']
        rendered = await shared.routes.export(item['id'], scope.workspace, list(scope.collection_ids))
        original = await shared.routes.export(item['id'], scope.workspace, list(scope.collection_ids), layout='original')
        assert 'Original answer and exact excerpts' in rendered['markdown']
        assert 'Original answer and exact excerpts' not in original['markdown']
        reviewed = await shared.routes.review(item['id'], shared.routes.ReviewRequest(workspace=scope.workspace,
            collection_ids=list(scope.collection_ids), decision='accept', reviewer='test', reason='Synthetic transport check'))
        assert reviewed['readable_answers'] == reopened['readable_answers'] and len(runtime.calls) == 2
    asyncio.run(run())

"""Source conditions and historical review survive the opt-in task display."""
from copy import deepcopy

import pytest

from potluck_local.services import investigation_readable_presentation as view
from test_investigation_readable_task_statement import fixture


def record():
    text, sources = fixture()
    question = 'What must happen before the kit can be used?'
    answer = {'question_id': 'R1', 'status': 'supported',
              'text': 'P1: ' + question + '\nEvidence: [1]\n\n[1]\n' + text,
              'snippet_ids': ['S1'],
              'source_excerpts': [{'snippet_id': 'S1', 'citation_id': 'claim:1', 'quote': text}],
              'passage_presentation': {'version': 'fixed-part-selection-v43',
                  'parts': [{'id': 'P1', 'request': question, 'verdict': 'supported'}],
                  'passages': [{'unit_id': 'C1:whole', 'citation_id': 'claim:1', 'text': text}]}}
    return {'id': 'case', 'issue': question, 'model_slug': 'test', 'provenance': {'unchanged': True},
            'brief': {'questions': [{'id': 'R1', 'text': question}], 'answers': [answer]},
            'evidence': [{'kind': 'reviewed_claim', 'status': 'approved', 'citation_id': 'claim:1',
                          'content': text, 'sources': list(reversed(sources))}],
            'review': {'decision': 'reject', 'reviewer': 'ASSISTANT', 'reason': 'Earlier layout unclear.'}}


def test_body_conditions_stay_before_actions_and_metadata_moves_after_every_body():
    item = record(); frozen = deepcopy(item)
    value = view.decorate(item)['readable_answers']['R1']; markdown = value['markdown']
    assert value['version'] == view.TASK_VERSION
    assert item == frozen
    assert item['brief']['answers'][0]['text'] == value['original_text']
    assert markdown.index('Before use, obtain approval.') < markdown.index('**Fix.**')
    assert markdown.index('**Fix.**') < markdown.index('Exception: wait when approval is unavailable.')
    assert markdown.index('Exception:') < markdown.index('```yaml')
    assert 'P1: ' not in markdown and 'status: open' in markdown
    assert 'claim:1' == value['source_groups'][0]['citation_id']
    source = item['evidence'][0]['content']
    spans = value['source_groups'][0]['task_statement_projection']['segments']
    assert ''.join(source[s['start']:s['end']] for s in spans) == source
    assert view.export(item, str).count(item['brief']['answers'][0]['text']) == 1
    assert item['review'] == frozen['review'] and item['provenance'] == frozen['provenance']


@pytest.mark.parametrize('version', range(32, 43))
def test_legacy_contracts_keep_their_existing_projection(version):
    item = record(); a = item['brief']['answers'][0]
    a['passage_presentation']['version'] = f'fixed-part-selection-v{version}'
    current = view.project(a, item['evidence'], question=item['issue'])
    assert current == view.project(a, item['evidence'], allow_task_layout=False)
    assert current['version'] == view.VERSION


@pytest.mark.parametrize('status', ['unknown', 'conflicting'])
def test_non_supported_verdicts_keep_original_leads(status):
    item = record(); a = item['brief']['answers'][0]; a['status'] = status
    assert view.decorate(item)['readable_answers']['R1']['version'] == view.VERSION


def test_correction_and_extra_verdict_or_scope_text_are_not_suppressed():
    item = record(); item['review']['decision'] = 'correct'
    assert view.decorate(item)['readable_answers']['R1']['version'] == view.VERSION
    assert view.export(item, str) is None
    for prefix in ['No.\n', 'Only the selected statements establish this scope.\n']:
        item = record(); a = item['brief']['answers'][0]; a['text'] = prefix + a['text']
        value = view.decorate(item)['readable_answers']['R1']
        assert value['version'] == view.VERSION and prefix.rstrip() in value['markdown']


def test_unknown_metadata_falls_back_without_hiding_a_condition():
    item = record(); a = item['brief']['answers'][0]
    old = item['evidence'][0]['content']; new = old.replace('type: task', 'type: task\nrequires: signed approval')
    item['evidence'][0]['content'] = new
    for s in item['evidence'][0]['sources']:
        s['content'] = s['content'].replace('type: task', 'type: task\nrequires: signed approval')
    a['text'] = a['text'].replace(old, new); a['passage_presentation']['passages'][0]['text'] = new
    value = view.decorate(item)['readable_answers']['R1']
    assert value['version'] == view.VERSION and 'requires: signed approval' in value['markdown']


def scoped_record(kind):
    item = record(); a = item['brief']['answers'][0]; p = a['passage_presentation']
    question = item['issue']; lead, body = a['text'].split('\n\n[1]\n', 1)
    if kind == 'direct':
        p['acknowledgements'] = [{'part_id': 'P1', 'answer': 'No', 'clause': question,
                                  'source_unit_ids': ['C1:whole']}]
        notice = 'Direct answers (selected reviewed statements; unverified):\n- No: ' + question + ' [1]'
    else:
        clause = 'before the kit can be used?'; start = question.index(clause)
        p['unresolved_requests'] = [{'part_id': 'P1', 'text': clause,
                                    'start': start, 'end': len(question)}]
        a['status'] = p['parts'][0]['verdict'] = 'unknown'
        lead = 'P1: ' + question + ' — Not established by the selected reviewed statements.\nScope basis: [1]'
        notice = ('Not established by the selected reviewed statements (model assessment; review required):'
                  '\n- P1: ' + clause)
    a['text'] = notice + '\n\n' + lead + '\n\n[1]\n' + body
    return item, notice


@pytest.mark.parametrize('kind', ['direct', 'unknown'])
def test_bound_decisions_stay_visible_beside_task_body_without_duplicate_intro(kind):
    item, notice = scoped_record(kind); frozen = deepcopy(item)
    a = item['brief']['answers'][0]
    value = view.decorate(item)['readable_answers']['R1']; markdown = value['markdown']
    assert value['version'] == view.TASK_SCOPED_VERSION
    assert markdown.startswith(notice) and item == frozen
    assert value['original_text'] == a['text'] and value['original_status'] == a['status']
    assert markdown.count(item['issue']) == (1 if kind == 'direct' else 0)
    assert markdown.index('Before use, obtain approval.') < markdown.index('**Fix.**')
    assert markdown.index('Exception: wait when approval is unavailable.') < markdown.index('```yaml')
    assert 'status: open' in markdown
    if kind == 'unknown':
        assert 'Scope basis: [1]' in markdown
    exported = view.export(item, str)
    assert notice in exported and a['text'] in exported
    assert value['source_groups'][0]['citation_id'] == 'claim:1'


@pytest.mark.parametrize('mutation', ['extra-prefix', 'wrong-clause', 'wrong-source', 'bad-span',
                                      'bool-span', 'wrong-part', 'wrong-verdict', 'missing-scope'])
def test_mismatched_scoped_decisions_fall_back_losslessly(mutation):
    item, _ = scoped_record('direct' if mutation in ('wrong-clause', 'wrong-source') else 'unknown')
    a = item['brief']['answers'][0]; p = a['passage_presentation']
    if mutation == 'extra-prefix': a['text'] = 'Only if approval is signed.\n' + a['text']
    elif mutation == 'wrong-clause': p['acknowledgements'][0]['clause'] = 'Different request'
    elif mutation == 'wrong-source': p['acknowledgements'][0]['source_unit_ids'] = ['C9:whole']
    elif mutation == 'bad-span': p['unresolved_requests'][0]['start'] += 1
    elif mutation == 'bool-span': p['unresolved_requests'][0]['start'] = True
    elif mutation == 'wrong-part': p['unresolved_requests'][0]['part_id'] = 'P2'
    elif mutation == 'wrong-verdict': p['parts'][0]['verdict'] = 'supported'
    elif mutation == 'missing-scope': a['text'] = a['text'].replace('Scope basis: [1]', 'Scope basis: [9]')
    result = view.decorate(item)['readable_answers']['R1']
    assert result['version'] == view.VERSION
    assert result['original_text'] == a['text']
    assert 'Before use, obtain approval.' in result['markdown']
    assert 'Exception: wait when approval is unavailable.' in result['markdown']


def test_literal_property_candidate_keeps_the_same_complete_task_layout():
    older = record()
    candidate = deepcopy(older)
    candidate['brief']['answers'][0]['passage_presentation']['version'] = 'fixed-part-selection-v44'
    frozen = deepcopy(candidate)
    result = view.decorate(candidate)['readable_answers']['R1']
    assert result == view.decorate(older)['readable_answers']['R1']
    assert candidate == frozen
    assert result['original_text'] == candidate['brief']['answers'][0]['text']

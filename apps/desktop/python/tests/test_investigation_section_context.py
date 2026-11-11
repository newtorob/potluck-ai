from copy import deepcopy
import json
import pytest
from potluck_local.services import investigation_evidence_coverage_v3 as contract
from test_investigation_evidence_revision import sample


def prepared(text, aliases=False):
    envelope = sample(); src = envelope['sources'][0]; src['quote'] = text
    src['context']['cited_chunk'] = {'quote': text, 'selected_start': 0, 'selected_end': len(text)}
    if aliases:
        other = deepcopy(src); other['id'] = 'Q2'; other['snippet_id'] = 'S2'; envelope['sources'].append(other)
    return contract.prepare(envelope)


def select(p, fragment):
    return {'verdict':'source_rule', 'units':{u['id']:('answer' if fragment in u['quote'] else 'background')
            for u in p['units']}, 'dependencies':[], 'comparisons':[]}


def test_nested_ancestry_retained_without_siblings_and_raw_classification_preserved():
    p = prepared('# Root\n\n## Branch A\n\n### Exception\n\nSelected fact.\n\n## Branch B\n\nUnrelated fact.')
    raw = select(p, 'Selected fact.'); result = contract.decode(json.dumps(raw), p)
    added = [u['quote'].strip() for u in result['selected'] if u['id'] in result['structural_context_units']]
    assert added == ['# Root', '## Branch A', '### Exception']
    assert all('Branch B' not in u['quote'] for u in result['selected'])
    assert all(result['model_classifications'][uid] == 'background' and result['classifications'][uid] == 'dependency'
               for uid in result['structural_context_units'])
    assert result['needs_review'] is True
    assert contract.decode(json.dumps(raw), p) == result


def test_same_level_and_root_sibling_stop_previous_ancestry():
    p = prepared('# First\n\n## Old\n\nOld fact.\n\n# Second\n\n## New\n\nSelected fact.')
    result = contract.decode(json.dumps(select(p, 'Selected fact.')), p)
    assert [u['quote'].strip() for u in result['selected'] if u['id'] in result['structural_context_units']] == ['# Second', '## New']


def test_fenced_headings_ignored_but_parent_retained():
    p = prepared('# Governing\n\n```text\n## Not a heading\nSelected fact.\n```\n\nTail.')
    result = contract.decode(json.dumps(select(p, 'Selected fact.')), p)
    assert [u['quote'].strip() for u in result['selected'] if u['id'] in result['structural_context_units']] == ['# Governing']


def test_aliases_keep_one_exact_heading_and_all_source_ids():
    p = prepared('# Governing\n\nSelected fact.', aliases=True)
    result = contract.decode(json.dumps(select(p, 'Selected fact.')), p)
    unit = next(u for u in result['selected'] if u['id'] in result['structural_context_units'])
    assert unit['source_ids'] == ['Q1','Q2']
    context = p['contexts'][0]
    assert context['text'][unit['start']:unit['end']] == unit['quote']


def test_nonisolated_governing_heading_fails_closed():
    p = prepared('# Heading\nSelected fact.\n')
    # A unit containing the selected fact already contains the heading; no promotion is needed.
    result = contract.decode(json.dumps(select(p, 'Selected fact.')), p)
    assert result['structural_context_units'] == []
    p = prepared('# Heading\nOther text.\n\nSelected fact.')
    with pytest.raises(ValueError, match='not an isolated'): contract.decode(json.dumps(select(p, 'Selected fact.')), p)

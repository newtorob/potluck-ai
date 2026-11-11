from copy import deepcopy
import pytest
from potluck_local.services.investigation_part_group_presentation import render


def part(key, verdict='supported', text='Exact prerequisite; exact exception.'):
    return {'id': key, 'request': 'What is required?', 'verdict': verdict,
            'source_text': text, 'answer_unit_ids': ['U1'], 'alternative_unit_ids': [],
            'context_unit_ids': [], 'unit_ids': ['U1', 'U2'],
            'passages': [{'unit_id': 'U1', 'text': text}], 'dependency_only_unit_ids': ['U2']}


def test_identical_adjacent_evidence_once_all_labels_in_order_original_unchanged():
    rows = [part('P1'), part('P2')]
    original = deepcopy(rows)
    result = render(rows)
    assert result['text'].count(rows[0]['source_text']) == 1
    assert result['text'].index('P1:') < result['text'].index('P2:') < result['text'].index('Exact')
    assert result['groups'][0]['part_ids'] == ['P1', 'P2']
    assert rows == original
    assert result['parts'] == original
    result['parts'][0]['passages'].clear()
    assert rows == original


@pytest.mark.parametrize('field,value', [('verdict', 'unknown'), ('answer_unit_ids', ['U2']),
    ('context_unit_ids', ['U3']), ('unit_ids', ['U1']), ('dependency_only_unit_ids', []),
    ('source_text', 'Different exact source.'), ('passages', [{'unit_id': 'U9'}])])
def test_different_status_role_or_provenance_never_grouped(field, value):
    rows = [part('P1'), part('P2')]
    rows[1][field] = value
    assert len(render(rows)['groups']) == 2


def test_unknown_conflict_leads_keep_scope_and_no_winner():
    rows = [part('P1', 'unknown', ''), part('P2', 'conflicting')]
    result = render(rows)
    assert 'selected reviewed statements do not establish P1:' in result['text']
    assert 'unresolved incompatible alternatives for P2:' in result['text']
    assert 'No authoritative winner is established.' in result['text']


def test_nonadjacent_blocks_keep_order_and_repeated_context():
    rows = [part('P1'), part('P2', text='Other prerequisite.'), part('P3')]
    result = render(rows)
    assert len(result['groups']) == 3
    assert result['text'].count(rows[0]['source_text']) == 2


def test_budget_rejects_without_clipping_and_duplicate_parts_reject():
    with pytest.raises(ValueError, match='budget'):
        render([part('P1')], max_visible=10)
    with pytest.raises(ValueError, match='Duplicate'):
        render([part('P1'), part('P1')])

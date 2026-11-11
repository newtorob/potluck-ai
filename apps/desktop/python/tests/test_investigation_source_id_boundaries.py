"""Primary-reviewed local-agent suggestions; no native inference or network."""
from copy import deepcopy
import pytest
from potluck_local.services import investigation_part_selection as selection
from potluck_local.services import reviewed_source_unit_plan as units


def prepared(count):
    return {'catalog': {'units': [{'id': f'C1:u{i:02}'} for i in range(count)]}}


def test_source_id_results_and_caller_inputs_cannot_alias():
    source = prepared(2)
    before = deepcopy(source)
    requested = ['C1:u00']
    result = selection._ids(source, requested)
    assert result == requested and result is not requested
    result.append('C1:u01')
    assert requested == ['C1:u00']
    requested[0] = 'C1:u01'
    assert result == ['C1:u00', 'C1:u01']
    assert source == before


def test_source_id_count_limit_isolated_from_foreign_id_rejection():
    source = prepared(units.MAX_CLOSED + 1)
    ids = [unit['id'] for unit in source['catalog']['units']]
    assert selection._ids(source, ids[:-1]) == ids[:-1]
    before = deepcopy(source)
    with pytest.raises(ValueError, match='Invalid source unit IDs'):
        selection._ids(source, ids)
    assert source == before

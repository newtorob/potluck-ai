import json
from copy import deepcopy
import pytest
from test_investigation_part_selection_v21 import setup, v8
from potluck_local.services import runtime, split_native


def test_numeric_both_context_and_schema_admission():
    *_, prepared, _ = setup()
    raw = json.dumps({'inventory': [[3, 0], [0, 2], [0, 0]], 'context': [True, False, False]})
    result = v8.decode_selection(raw, prepared)
    assert result['inventory']['C1:rule'] == ['P1:evidence', 'P1:qualifier', 'request_context']
    assert result['inventory']['C1:exception'] == ['P2:qualifier']
    fmt = {'type':'json_object', 'schema':v8.response_schema(prepared)}
    assert runtime._structured_response_format(object.__new__(split_native.SplitModel), fmt) == fmt
    assert v8.review_messages(prepared, result)


@pytest.mark.parametrize('bad', [True, False, -1, 4, 1.0, '1', None])
def test_invalid_cell_rejected(bad):
    *_, prepared, _ = setup()
    with pytest.raises(ValueError):
        v8.decode_selection(json.dumps({'inventory': [[bad,0],[0,0],[0,0]], 'context': [False]*3}), prepared)


def test_old_compact_wire_and_ragged_and_input_tamper_rejected():
    *_, p, _ = setup()
    for data in ({'inventory':['en0','nn0','nn0']}, {'inventory':[[1],[0,0],[0,0]],'context':[False]*3}):
        with pytest.raises(ValueError): v8.decode_selection(json.dumps(data), p)
    bad=deepcopy(p);bad['catalog']['units'].reverse()
    with pytest.raises(ValueError):v8.messages(bad)

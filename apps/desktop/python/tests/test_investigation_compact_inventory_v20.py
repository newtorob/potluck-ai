import importlib.util
import json
import sys
from pathlib import Path

import pytest
from potluck_local.services import investigation_request_parts, reviewed_source_unit_plan, runtime, split_native

from potluck_local.services import investigation_part_selection_v20 as module


def fixture():
    question = 'What rule and date?'
    text = 'Approval is required.'
    evidence = [{'kind': 'reviewed_claim', 'citation_id': 'claim:a', 'content': text,
                 'sources': [{'citation_id': 'source:a', 'content': text}]}]
    plan = investigation_request_parts.prepare(question, ['What rule?', 'What date?'], origin='fixture_supplied')
    return module.prepare(question, evidence, plan)


def test_compact_codes_bind_both_and_context_and_admit_runtime():
    p = fixture()
    assert runtime._structured_response_format(object.__new__(split_native.SplitModel), {'type':'json_object','schema':module.response_schema(p)})
    raw = json.dumps({'inventory':['bq1']})
    s = module.decode_selection(raw, p)
    assert next(iter(s['inventory'].values())) == ['P1:evidence', 'P1:qualifier', 'P2:qualifier', 'request_context']
    assert module.review_messages(p, s)
    with pytest.raises(ValueError):
        module.review_messages(p, {**s, 'raw': json.dumps({'inventory':['en0']})})


@pytest.mark.parametrize('wire', [[], ['x'], ['nn2'], ['nx0'], [True], [['none','none']], ['nn0','nn0']])
def test_invalid_codes_fail(wire):
    with pytest.raises(ValueError):
        module.decode_selection(json.dumps({'inventory':wire}), fixture())


def test_old_wire_and_duplicate_key_reject():
    p = fixture()
    for raw in ('{"inventory":[["none","none"]],"context":[false]}', '{"inventory":["nn0"],"inventory":["nn0"]}'):
        with pytest.raises(ValueError):
            module.decode_selection(raw, p)


def test_unknown_basis_stays_scoped_and_check_not_rendered():
    p = fixture()
    first = module.decode_selection(json.dumps({'inventory':['en0']}), p)
    uid = p['catalog']['units'][0]['id']
    review = json.dumps({'parts': {'P1': {'check':'private marker', 'verdict':'unknown', 'evidence':[uid], 'context':[]},
                                   'P2': {'check':'missing date', 'verdict':'unknown', 'evidence':[], 'context':[]}}, 'context':[]})
    result = module.decode_review(review, p, first)
    assert result['parts'][0]['verdict'] == 'unknown'
    assert result['parts'][0]['evidence_unit_ids'] == [uid]
    assert 'private marker' not in result['text']
    assert runtime._structured_response_format(object.__new__(split_native.SplitModel), {'type':'json_object','schema':module.review_schema(p, first)})


def test_version_and_prepared_tampering_reject():
    from copy import deepcopy
    p = fixture()
    for field, value in [('version','fixed-part-selection-v19'), ('question','other')]:
        bad = deepcopy(p); bad[field] = value
        with pytest.raises(ValueError):
            module.messages(bad)

"""Independent boundary tests for reviewed claim selection; synthetic data only."""
from copy import deepcopy
import json
import pytest
from potluck_local.services import investigation_reviewed_claim_selection as selection
from potluck_local.services import investigation_reviewed_evidence


def data():
    evidence = [{'kind': 'reviewed_claim', 'citation_id': 'claim:a', 'content': 'First paragraph.\n\nSecond paragraph.',
                 'sources': [{'citation_id': 'source:a:r1', 'content': 'First support paragraph.\n\nSecond support paragraph.'}]}]
    snippets, _ = investigation_reviewed_evidence.snippets(evidence)
    return evidence, snippets


def test_full_exact_evidence_and_scoped_empty():
    evidence, snippets = data(); op = {'id': 'R1', 'request': 'Rule?', 'operation': 'answer'}
    brief, _ = selection.make_brief('Rule?', op, evidence, snippets, '{"status":"supported","claim_ids":["C1"]}')
    text = brief['answers'][0]['text']
    assert evidence[0]['content'] in text
    assert all(s['quote'] in text for s in snippets if s['citation_id'].startswith('source:'))
    empty, _ = selection.make_brief('Rule?', op, evidence, snippets, '{"status":"unknown","claim_ids":[]}')
    assert 'does not establish absence from the collection' in empty['answers'][0]['text']


def test_authoritative_replay_rejects_rehashed_catalog_and_support_changes():
    evidence, _ = data(); prepared = selection.prepare('Rule?', evidence); raw = '{"status":"supported","claim_ids":["C1"]}'
    stored = selection.decode(raw, prepared)
    forged = deepcopy(prepared); forged['claims'][0]['text'] = 'Invented.'
    forged['input_sha256'] = selection._digest({k: v for k, v in forged.items() if k != 'input_sha256'})
    forged_result = selection.decode(raw, forged)
    with pytest.raises(ValueError): selection.validate_original('Rule?', evidence, raw, forged_result)
    altered = deepcopy(evidence); altered[0]['sources'][0]['citation_id'] = 'source:a:r2'
    with pytest.raises(ValueError): selection.validate_original('Rule?', altered, raw, stored)


@pytest.mark.parametrize('raw', ['{"status":"supported","claim_ids":["C1","C1"]}', '{"status":"supported","claim_ids":["C9"]}', '{"status":"conflicting","claim_ids":["C1"]}', '{"status":"unknown","status":"unknown","claim_ids":[]}'])
def test_bad_selection_is_rejected(raw):
    evidence, _ = data()
    with pytest.raises(ValueError): selection.decode(raw, selection.prepare('Rule?', evidence))


def test_partial_support_catalog_is_not_a_complete_support():
    evidence, snippets = data(); support = next(s for s in snippets if s['citation_id'].startswith('source:')); support['quote'] = 'First support paragraph.'
    op = {'id': 'R1', 'request': 'Rule?', 'operation': 'answer'}
    with pytest.raises(ValueError): selection.make_brief('Rule?', op, evidence, snippets, '{"status":"supported","claim_ids":["C1"]}')

import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_source_answer as v18


def original(more):
    plan = [{'id': 'R1', 'question_id': 'Q1', 'request': 'Explain.', 'operation': 'answer'}]
    snippets = [{'snippet_id': 'S1', 'citation_id': 'c1', 'quote': 'An exact passage. \n'}]
    raw = {'R1': {'snippet_ids': ['S1'], 'status': 'supported', 'more': more}}
    brief, _, _ = v18.decode_results(json.dumps(raw), plan, snippets, [], [])
    return brief, snippets


def test_status_only_and_whitespace_only_cannot_clear_incomplete():
    brief, snippets = original(True)
    for suffix in ('', '  \n'):
        correction = deepcopy(brief)
        correction['answers'][0]['status'] = 'supported'
        correction['answers'][0]['text'] += suffix
        with pytest.raises(ValueError):
            v18.corrected_brief(correction, brief, snippets)
    assert brief['answers'][0]['parts'][0]['claims'][0]['text'] == 'An exact passage. \n'


def test_unchanged_correction_keeps_incomplete_and_original_passage():
    brief, snippets = original(True)
    corrected = v18.corrected_brief(deepcopy(brief), brief, snippets)
    assert corrected['answers'][0]['status'] == 'incomplete'
    assert brief['answers'][0]['parts'][0]['claims'][0]['text'] == 'An exact passage. \n'


def test_real_edit_can_clear_but_never_mutates_original():
    brief, snippets = original(True)
    original_bytes = json.dumps(brief, ensure_ascii=False)
    correction = deepcopy(brief)
    correction['answers'][0]['status'] = 'supported'
    correction['answers'][0]['text'] = 'Owner corrected the answer.'
    result = v18.corrected_brief(correction, brief, snippets)
    assert result['answers'][0]['status'] == 'supported'
    assert result['model_uncertainties'] == [v18.WARNING]
    assert json.dumps(brief, ensure_ascii=False) == original_bytes

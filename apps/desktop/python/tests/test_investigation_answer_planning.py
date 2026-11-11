import json

import pytest

from potluck_local.services import investigation_answers as answers


def plan(parts, issue):
    return answers.decode_span_plan(json.dumps({'questions': parts}), issue)


def test_original_spans_keep_case_and_multipart_operation():
    issue = 'What restored service, and what was the difference and percentage change in latency?'
    parts = ['What restored service', 'what was the difference and percentage change in latency?']
    assert plan(parts, issue) == [{'id': 'Q1', 'text': parts[0]}, {'id': 'Q2', 'text': parts[1]}]


@pytest.mark.parametrize('parts', [
    ['What restored service', 'What restored service'],
    ['What restored service', 'restored service'],
    ['what was the difference', 'What restored service'],
    ['What was restored service'],
    ['What restored service?'],
    [''],
])
def test_rejects_duplicate_overlap_reordered_and_foreign_spans(parts):
    issue = 'What restored service, and what was the difference?'
    with pytest.raises(ValueError, match='Invalid model format'):
        plan(parts, issue)


def test_v6_planner_still_accepts_paraphrased_questions():
    raw = json.dumps({'questions': ['What was restored?']})
    assert answers.decode_plan(raw) == [{'id': 'Q1', 'text': 'What was restored?'}]
    with pytest.raises(ValueError, match='Invalid model format'):
        answers.decode_span_plan(raw, 'what restored service?')


@pytest.mark.parametrize('data', [
    [],
    {'questions': 'text'},
    {'questions': [None]},
    {'questions': []},
    {'questions': ['x'] * 9},
    {'questions': ['x' * 501]},
    {'questions': ['x'], 'extra': True},
])
def test_rejects_malformed_or_unbounded_plans(data):
    with pytest.raises(ValueError, match='Invalid model format'):
        answers.decode_span_plan(json.dumps(data), 'x' * 600)

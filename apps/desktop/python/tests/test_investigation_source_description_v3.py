import pytest
from potluck_local.services.investigation_source_question_form_v3 import (
    source_availability_frame, source_description_frame, refine_source_description_clause,
    description_request_frame, refine_description_request_clause,
)

@pytest.mark.parametrize('clause', [
    ' If not, what import test and token-deadline exception do they describe?',
    ' If not, what archive-window and billing discrepancies do they describe?',
    ', and what import validation and custom-app exception do they describe?',
    'what archive-window and billing discrepancies do these selected notes describe?',
    'what timing and review rules does the supplied note state?',
])
def test_description_structure_keeps_exact_text(clause):
    parent = 'Do these notes state a maximum size?' + clause if 'they' in clause else None
    frame = description_request_frame(clause, parent)
    assert frame and frame['target'] == 'reported_description' and not frame['known_or_truth_inferred']
    refined = refine_description_request_clause(clause, parent)
    assert len(refined) == 2 and ''.join(refined) == clause
    assert refined[1].lstrip().lower().startswith('and ')
    if 'they' in clause:
        assert frame['subject_reference'] == 'unresolved_pronoun' and frame['source_reference_resolved'] is False
        assert source_description_frame(clause, parent) is None
        assert refine_source_description_clause(clause, parent) == [clause]
    else:
        assert source_description_frame(clause)

@pytest.mark.parametrize('clause', [
    'Did the team perform the test?', 'What did they actually complete?',
    'What tests do they perform?', 'What permission do they grant?',
    'What test and exception did they describe?', 'What tests do the note authors describe?',
    'If approved, what test and exception do they describe?',
    'What test and exception do they describe if approved?',
    'What test and exception do they not describe?',
    'What "test" and exception do they describe?',
    'What test and (special exception) do they describe?',
    'What test or exception do they describe?',
    'What test and exception and fee do they describe?',
    'What rule applies if they describe an exception?',
])
def test_ambiguous_actual_or_conditional_scope_unmodified(clause):
    parent = 'Do these notes specify a fee? ' + clause
    assert description_request_frame(clause, parent) is None
    assert refine_description_request_clause(clause, parent) == [clause]


def test_unresolved_pronoun_requires_unique_parent_and_never_becomes_source():
    clause = ' If not, what coupons and discounts do they describe?'
    parent = 'Do these notes specify a fee for customers?' + clause
    assert source_description_frame(clause, parent) is None
    assert refine_source_description_clause(clause, parent) == [clause]
    frame = description_request_frame(clause, parent)
    assert frame['subject_reference'] == 'unresolved_pronoun'
    assert frame['source_reference_resolved'] is False and frame['parent_question'] == parent
    assert ''.join(refine_description_request_clause(clause, parent)) == clause
    assert len(refine_description_request_clause(clause, parent)) == 2
    assert description_request_frame(clause) is None
    assert description_request_frame(clause, parent + clause) is None


def test_trailing_punctuation_and_existing_availability():
    for clause in ['What shared-inbox access rule do these notes describe,',
                   'what access rule do these notes describe']:
        assert source_description_frame(clause)
        assert refine_description_request_clause(clause) == [clause]
    assert source_availability_frame('Do these selected tasks specify an exact fee?')
    assert source_availability_frame('May they use the equipment?') is None

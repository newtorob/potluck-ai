"""Question-form and decoder tests; no factual model-accuracy credit."""
import json
import pytest
from potluck_local.services.investigation_source_question_form import source_availability_frame as frame


@pytest.mark.parametrize('question', [
    'Do the supplied notes specify a replacement fee for a lost drill, ',
    'Does the collection note specify when a seed sample may be released, ',
    'Do the supplied notes specify a calibration-record retention period,',
    'Do the supplied notes not specify a required packing material for the framed print, and ',
    'Does the quarterly safety note specify a temperature limit?',
    'Do these reviewed documents specify a storage period?',
    'Does the selected text specify whether the coordinator signed?',
    ' and does the reviewed note specify a period?',
])
def test_narrow_document_content_question_selects_only_form(question):
    value = frame(question)
    assert value['question_only'] and value['answer_or_polarity_inferred'] is False
    assert 'yes' not in value and 'no' not in value


@pytest.mark.parametrize('question', [
    'May visitors photograph the control panel?',
    'Under current guidance, may pilot demos run in the cloud,',
    'Did the person named in the notes sign?',
    'Does the person in the note specify a fee?',
    'Does the author of the note specify a fee?',
    "Does the note's author specify a fee?",
    'Do the supplied notes specify a fee and may visitors borrow it?',
    'Do the supplied notes specify a fee or permit borrowing?',
    'Do the notes specify a fee, and did the lead sign?',
    'Do the supplied notes specify a fee if the lead signs?',
    'Do the notes specify a fee unless exempt?',
    'Do the notes specify “do not specify a fee”?',
    'Do the notes state that they do not specify a fee?',
    'Do the supplied rules prohibit picking flowers?',
    'and is a recording retention period specified?',
    'Do a note specify a fee?',
    'Does the notes specify a fee?',
    'Do the note specify a fee?',
    'Does the person who reviewed the note specify a fee?',
    'Does no note specify a fee?',
    'Did the supplied note specify a fee?',
])
def test_actual_ambiguous_or_unrecognized_questions_remain_unrestricted(question):
    assert frame(question) is None

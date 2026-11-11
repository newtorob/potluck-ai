from copy import deepcopy
import pytest
from potluck_local.services import investigation_fixed_part_answers as fixed


def test_blank_snippet_is_normalized_without_changing_original():
    original, snippets = fixture()
    snippets[0]['quote'] = '   '
    original['answers'][0]['source_excerpts'][0]['quote'] = '   '
    before = deepcopy(original)
    corrected = fixed.corrected_brief(deepcopy(original), original, snippets)
    assert all(row['quote'].strip() for row in corrected['answers'][0]['source_excerpts'])
    assert original == before
    assert fixed.corrected_brief(corrected, original, snippets) == corrected


def fixture(count=18):
    snippets = [{'snippet_id': f'S{i}', 'citation_id': 'claim:x', 'quote': f'quote {i}'} for i in range(count)]
    answer = {'question_id': 'R1', 'status': 'supported', 'text': 'A correction',
              'snippet_ids': [s['snippet_id'] for s in snippets], 'source_excerpts': deepcopy(snippets),
              'passage_presentation': {'version': fixed.VERSION}}
    brief = {'questions': [{'id': 'R1', 'text': 'Question?'}], 'answers': [answer],
             'facts': [], 'prior_fixes': [], 'hypotheses': [], 'recommended_checks': [], 'model_uncertainties': []}
    return brief, snippets


def test_more_than_sixteen_and_maximum_catalog():
    for count in (18, 100):
        original, snippets = fixture(count)
        assert len(fixed.corrected_brief(original, original, snippets)['answers'][0]['snippet_ids']) == count
    original, snippets = fixture(101)
    with pytest.raises(ValueError): fixed.corrected_brief(original, original, snippets)


def test_forged_blank_excerpt_is_rejected_before_normalization():
    original, snippets = fixture(2)
    snippets[0]['quote'] = '\n'
    original['answers'][0]['source_excerpts'][0]['quote'] = '\n'
    submitted = deepcopy(original)
    submitted['answers'][0]['source_excerpts'][0]['quote'] = ' \n'
    with pytest.raises(ValueError, match='source excerpts'):
        fixed.corrected_brief(submitted, original, snippets)


@pytest.mark.parametrize('status', ['supported', 'conflicting'])
def test_blank_only_citations_do_not_support_a_correction(status):
    original, snippets = fixture(1)
    snippets[0]['quote'] = '\n\t'
    original['answers'][0]['source_excerpts'] = deepcopy(snippets)
    original['answers'][0]['status'] = status
    with pytest.raises(ValueError, match='requires source excerpts'):
        fixed.corrected_brief(original, original, snippets)


def test_text_boundary_and_credential_redaction():
    original, snippets = fixture(1)
    submitted = deepcopy(original)
    submitted['answers'][0]['text'] = 'x' * fixed.MAX_VISIBLE_CHARS
    assert len(fixed.corrected_brief(submitted, original, snippets)['answers'][0]['text']) == fixed.MAX_VISIBLE_CHARS
    submitted['answers'][0]['text'] = 'sk-' + 'a' * 40
    corrected = fixed.corrected_brief(submitted, original, snippets)
    assert 'sk-' not in corrected['answers'][0]['text']
    assert submitted['answers'][0]['text'].startswith('sk-')


def test_legacy_correction_validator_retains_its_strict_contract():
    from potluck_local.services import investigation_reviewed_claim_selection_v6 as legacy
    original, snippets = fixture(2)
    snippets[0]['quote'] = '\n'
    original['answers'][0]['source_excerpts'] = deepcopy(snippets)
    with pytest.raises(ValueError):
        legacy.corrected_brief(original, original, snippets)
    assert len(fixed.corrected_brief(original, original, snippets)['answers'][0]['snippet_ids']) == 1


@pytest.mark.parametrize('mutation', ['duplicate', 'unknown', 'forged', 'presentation', 'question', 'section', 'empty', 'overflow', 'blank_only'])
def test_reject_invalid_correction(mutation):
    original, snippets = fixture()
    raw = deepcopy(original)
    answer = raw['answers'][0]
    if mutation == 'duplicate': answer['snippet_ids'].append(answer['snippet_ids'][0])
    elif mutation == 'unknown': answer['snippet_ids'][0] = 'missing'
    elif mutation == 'forged': answer['source_excerpts'][0]['quote'] = 'forged'
    elif mutation == 'presentation': answer['passage_presentation']['version'] = 'changed'
    elif mutation == 'question': raw['questions'][0]['text'] = 'changed'
    elif mutation == 'section': raw['facts'] = ['changed']
    elif mutation == 'empty': answer['text'] = ' '
    elif mutation == 'overflow': answer['text'] = 'x' * 8001
    elif mutation == 'blank_only':
        snippets[0]['quote'] = ' '
        answer['snippet_ids'] = ['S0']
        answer['source_excerpts'] = [deepcopy(snippets[0])]
    with pytest.raises(ValueError): fixed.corrected_brief(raw, original, snippets)

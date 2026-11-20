import pytest
from potluck_local.services.investigation_literal_context import coalesce_context_prefixes as coalesce


def test_observed_conditional_stub_keeps_exact_order_and_offsets():
    parts = ['Do notes specify a fee?', ' If not,', ' what archive-window and billing discrepancies do they describe?']
    result = coalesce(parts)
    assert result['clauses'] == [parts[0], parts[1] + parts[2]]
    assert [x['index'] for x in result['mapping'][1]['originals']] == [1, 2]
    original = ''.join(parts)
    for row in result['mapping']:
        assert original[row['start']:row['end']] == result['clauses'][row['output_index']]
        for source in row['originals']:
            assert original[source['start']:source['end']] == parts[source['index']]


def test_standalone_prefix_chains_and_unicode():
    parts = ['¿Fee?', ' If so,', ' and', ' what', ' café details apply?']
    result = coalesce(parts)
    assert result['clauses'] == [parts[0], ''.join(parts[1:])]
    assert ''.join(result['clauses']) == ''.join(parts)
    assert result['offset_unit'] == 'unicode_codepoints'


@pytest.mark.parametrize('literal', ['If not verified', 'If not, explain why?', 'What?',
                                     'and what happens?', '"If not,"', 'what is needed',
                                     'If so verified', 'or paid', 'which policy?'])
def test_actual_or_ambiguous_properties_not_merged(literal):
    assert coalesce([literal, ' Next question?'])['clauses'] == [literal, ' Next question?']


@pytest.mark.parametrize('parts', [['First?', ' If not,'], ['First?', ' and', ' what'], ['but']])
def test_terminal_prefix_refuses_without_rewriting(parts):
    with pytest.raises(ValueError, match='Question mapping needs review'):
        coalesce(parts)


def test_ordinary_clauses_and_input_are_unchanged():
    parts = ['Under approval, what applies,', ' and did the event occur?']
    assert coalesce(parts)['clauses'] == parts
    assert parts == ['Under approval, what applies,', ' and did the event occur?']


@pytest.mark.parametrize('prefix', [' If not, what', ' If so, and which', ' and why', ' or who'])
def test_compound_context_heads_merge_without_losing_words(prefix):
    parts=['First?',prefix,' requirement applies?']
    result=coalesce(parts)
    assert result['clauses']==[parts[0],prefix+parts[2]]
    assert ''.join(result['clauses'])==''.join(parts)


@pytest.mark.parametrize('literal', ['andwhat','if notwhat','orwhich','butwhy'])
def test_single_words_do_not_become_connective_prefixes(literal):
    assert coalesce([literal,' another field?'])['clauses']==[literal,' another field?']

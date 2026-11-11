import pytest

from potluck_local.services.investigation_calculations import calculate, extraction_notices, quantity_catalog


def catalog(text, **kwargs):
    return quantity_catalog([{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': text}], version='v5', **kwargs)


def test_benchmark_exact_spans_and_difference():
    text = '**146.2 tok/s** and **96.8 tok/s**'
    quantities = catalog(text)
    assert [(q['source_text'], q['start'], q['end'], q['unit']) for q in quantities] == [
        ('146.2 tok/s', 2, 13, 'token/s'), ('96.8 tok/s', 22, 32, 'token/s')]
    result = calculate('difference', ['N2', 'N1'], quantities, version='v5')
    assert (result['status'], result['result'], result['unit']) == ('computed', '49.4', 'token/s')
    assert result['algorithm_version'] == 'cited-calculations-v5'
    assert result['evaluated_expression']


def test_aliases_and_incompatible_numerators():
    quantities = catalog('5 tokens per second; 3 tok/sec; 4 requests/s; 2 req per s')
    assert [q['unit'] for q in quantities] == ['token/s', 'token/s', 'request/s', 'request/s']
    assert calculate('difference', ['N1', 'N2'], quantities, version='v5')['result'] == '-2'
    assert calculate('difference', ['N1', 'N3'], quantities, version='v5')['status'] == 'unknown'


@pytest.mark.parametrize('text', [
    '**10 tok/s', '$**10 tok/s', 'between 5 tok/s and 8 tok/s', 'from 5 tok/s to 8 tok/s', 'not 5 tok/s', 'not **5 tok/s**', 'at least 5 tok/s', 'approximately 5 tok/s', 'between 5 and 8 tok/s',
    '1,000 tok/s', '1, 000 tok/s', '1e3 tok/s', '1/2 tok/s', '5 tok/s/foo',
    '$**10 tok/s**', '2/**10 tok/s**', '1e**3 tok/s**', '**10 tok/s**/worker',
    '10 tok/s**', '10–20 tok/s', '10 tok/s per worker', '5 tok/s per user', '5 tok/min', '5 reqs/s', '5 tok/s2', '5 to 8 tok/s',
])
def test_ambiguous_rates_are_not_extracted(text):
    assert not [q for q in catalog(text) if q['dimension'] == 'rate']


def test_notices_validation_and_context():
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': '5 tok/s; 1e3 tok/s'}]
    quantities = quantity_catalog(snippets, version='v5', source_context={'C1': snippets[0]['quote']})
    assert len(quantities) == 1
    assert extraction_notices(snippets, quantities, version='v5')
    quantities[0]['value'] = '9'
    with pytest.raises(ValueError):
        calculate('difference', ['N1'], quantities, version='v5')
    with pytest.raises(ValueError):
        extraction_notices(snippets, quantities, version='v5')


def test_context_rejects_truncated_rate():
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': '5 tok/s'}]
    assert quantity_catalog(snippets, version='v5', source_context={'C1': '5 tok/s/foo'}) == []


def test_notice_limit_with_scientific_notation():
    text = ' '.join(['123 unsupported'] * 51) + ' 1e3 tok/s'
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': text}]
    quantities = quantity_catalog(snippets, version='v5')
    assert len(extraction_notices(snippets, quantities, version='v5')) == 50

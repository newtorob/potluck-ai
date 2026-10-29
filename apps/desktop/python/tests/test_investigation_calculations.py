import copy
import json

import pytest

from potluck_local.services.investigation_calculations import calculate, quantity_catalog


def catalog(*quotes):
    return quantity_catalog([{'snippet_id': f'S{i}', 'citation_id': f'C{i}', 'quote': q}
                             for i, q in enumerate(quotes, 1)])


def test_source_spans_and_time_difference():
    quantities = catalog('Before: 2 minutes.', 'After: 90000ms.')
    assert [q['source_text'] for q in quantities] == ['2 minutes', '90000ms']
    for q in quantities:
        assert q['snippet_text'][q['start']:q['end']] == q['source_text']
    result = calculate('difference', ['N1', 'N2'], quantities)
    assert (result['status'], result['result'], result['unit']) == ('computed', '-30', 's')
    assert result['formula'] == 'after - before'
    assert result['inputs'] == quantities
    json.dumps(result)


def test_percent_operations_are_distinct():
    quantities = catalog('Before 20%.', 'After 30%.')
    pp = calculate('percentage_point_difference', ['N1', 'N2'], quantities)
    change = calculate('percent_change', ['N1', 'N2'], quantities)
    assert (pp['result'], pp['unit']) == ('10', 'percentage points')
    assert (change['result'], change['unit']) == ('50', '%')
    generic = calculate('difference', ['N1', 'N2'], quantities)
    assert (generic['result'], generic['unit']) == ('10', 'percentage points')


def test_part_total_rounding_and_copies():
    quantities = catalog('1 request.', '6 requests.')
    original = copy.deepcopy(quantities)
    result = calculate('percentage', ['N1', 'N2'], quantities)
    assert result['result'] == '16.666667'
    assert result['rounding'] == {'mode': 'ROUND_HALF_UP', 'max_fractional_digits': 6}
    result['inputs'][0]['value'] = '999'
    assert quantities == original


def test_unknown_cases():
    q = catalog('0 requests.', '2 requests.', '2 errors.', '4 seconds.')
    assert calculate('percent_change', ['N1', 'N2'], q)['reason'] == 'The baseline is zero.'
    assert calculate('percentage', ['N2', 'N1'], q)['reason'] == 'The total is zero.'
    assert calculate('difference', ['N1'], q)['status'] == 'unknown'
    assert calculate('difference', ['N2', 'N3'], q)['status'] == 'unknown'
    assert calculate('difference', ['N2', 'N4'], q)['status'] == 'unknown'
    assert calculate('percentage_point_difference', ['N1', 'N2'], q)['status'] == 'unknown'


def test_reject_spoofed_duplicate_and_extra_inputs():
    q = catalog('1 second.', '2 seconds.')
    for operands in (['N1', 'N1'], ['N1', 'N9'], ['N1', 'N2', 'N1']):
        with pytest.raises(ValueError):
            calculate('difference', operands, q)
    fake = copy.deepcopy(q)
    fake[0]['unit'] = 'h'
    with pytest.raises(ValueError):
        calculate('difference', ['N1', 'N2'], fake)
    with pytest.raises(ValueError):
        calculate('difference', ['N1', 'N2'], q + [q[0]])
    with pytest.raises(ValueError):
        quantity_catalog([{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': '1'},
                          {'snippet_id': 'S1', 'citation_id': 'C2', 'quote': '2'}])


@pytest.mark.parametrize('text', [
    '1e6', '1,234', '1.2.3', '10/20', '$10', '10 widgets', '10 GiB', '10ms/s',
    '1234567890123456789', '2024-01-01', 'abc123', '10-20', '10.123456789012345678', '$ 10', '10 °C', '10 ms / s', '10 per second', '10:20', '10 -20',
])
def test_unsupported_formats_are_not_partially_extracted(text):
    assert catalog(text) == []


def test_dimensionless_and_signed_numbers():
    q = catalog('from -1.25 to +2.5')
    assert [x['value'] for x in q] == ['-1.25', '2.5']
    assert calculate('difference', ['N1', 'N2'], q)['result'] == '3.75'


@pytest.mark.parametrize('space', ['\n', '\r\n', '\u00a0', '\t'])
def test_whitespace_does_not_hide_units(space):
    quantities = catalog(f'10{space}ms')
    assert len(quantities) == 1
    assert quantities[0]['unit'] == 'ms'
    assert quantities[0]['source_text'] == f'10{space}ms'
    assert catalog(f'10{space}GiB') == []


@pytest.mark.parametrize('text', [
    '10 seconds per request', '10ms / s', '10 seconds·requests', '1, 234',
    '1 234', '1\u00a0234', '€ 10', '10 to 20 widgets', '10 ms / 2 seconds',
])
def test_unsupported_composites_are_not_dimensionless(text):
    quantities = catalog(text)
    # An unambiguous separate first operand in a phrase may remain usable.
    if text == '10 to 20 widgets':
        assert [q['value'] for q in quantities] == ['10']
    else:
        assert quantities == []


def test_trace_records_conversion_roles_and_substitution():
    q = catalog('Before 2 minutes.', 'After 165 seconds.')
    result = calculate('difference', ['N1', 'N2'], q)
    assert result['algorithm_version'] == 'cited-calculations-v1'
    assert result['normalized_inputs'] == [
        {'id': 'N1', 'role': 'before', 'value': '120', 'unit': 's',
         'original_value': '2', 'original_unit': 'min', 'conversion_factor': '60'},
        {'id': 'N2', 'role': 'after', 'value': '165', 'unit': 's',
         'original_value': '165', 'original_unit': 's', 'conversion_factor': '1'},
    ]
    assert result['evaluated_expression'] == '165 s - 120 s = 45 s (rounded)'
    pct = calculate('percentage', ['N1', 'N2'], q)
    assert [item['role'] for item in pct['normalized_inputs']] == ['part', 'total']
    assert pct['evaluated_expression'] == '120 s / 165 s × 100 = 72.727273 % (rounded)'
    zero = calculate('percent_change', ['N1', 'N2'], catalog('0 s', '10 s'))
    assert zero['status'] == 'unknown' and zero['evaluated_expression'] is None
    assert zero['normalized_inputs'][0]['role'] == 'before'


def test_full_source_excludes_500_character_split_number():
    source = 'x' * 499 + '125 ms and 300 ms'
    snippets = [
        {'snippet_id': 'S1', 'citation_id': 'C1', 'quote': source[:500]},
        {'snippet_id': 'S2', 'citation_id': 'C1', 'quote': source[500:]},
    ]
    assert [q['source_text'] for q in quantity_catalog(snippets)] == ['25 ms', '300 ms']
    q = quantity_catalog(snippets, source_context={'C1': source})
    assert [item['source_text'] for item in q] == ['300 ms']
    assert all(item['snippet_text'] == source[500:] for item in q)


def test_full_source_excludes_6000_character_presentation_cutoff():
    source = 'x' * 5997 + ' 125ms and 300ms'
    presented = source[:6000]
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': presented}]
    assert [q['source_text'] for q in quantity_catalog(snippets)] == ['12']
    assert quantity_catalog(snippets, source_context={'C1': source}) == []


def test_full_source_excludes_unit_prefix_and_cross_snippet_span():
    prefix = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': '10 m'}]
    assert quantity_catalog(prefix, source_context={'C1': '10 ms'}) == []
    split = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': '10 '},
             {'snippet_id': 'S2', 'citation_id': 'C1', 'quote': 'ms'}]
    assert quantity_catalog(split, source_context={'C1': '10 ms'}) == []


def test_source_context_fail_closed_and_preserves_source_privacy():
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': '10 ms'}]
    for source_context in ({}, {'C1': '20 ms'}, {'C1': 'x' * 2_097_153}):
        with pytest.raises(ValueError):
            quantity_catalog(snippets, source_context=source_context)
    result = quantity_catalog(snippets, source_context={'C1': '10 ms. Private tail'})
    assert result[0]['source_text'] == '10 ms'
    assert 'Private tail' not in json.dumps(result)


@pytest.mark.parametrize('text', ['USD 10', 'EUR 10', 'usd\n10', 'GBP 10', 'JPY\u00a010'])
def test_currency_prefix_rejected(text):
    assert catalog(text) == []


def test_extractor_stops_before_validating_unpresented_rejected_numbers():
    from potluck_local.services.investigation_calculations import _extract

    class TrackedSource(str):
        slices = []

        def __getitem__(self, key):
            if isinstance(key, slice):
                self.slices.append(key)
            return super().__getitem__(key)

    source = TrackedSource('10 ms. ' + ('123 widgets ' * 10000))
    assert list(_extract(source, max_start=7)) == [(0, 5, '10', 'ms', 'time')]
    assert all(item.stop is None or item.stop <= 7 for item in source.slices)


@pytest.mark.parametrize('symbol', ['$', '€', '£', '¥', '₹', '₩', '₽', '₺', '₿', '₪', '₫', '₴', '₦', '₱'])
@pytest.mark.parametrize('spacing', ['', ' ', '\u00a0', '\n'])
def test_unicode_currency_symbols_are_not_dimensionless(symbol, spacing):
    import unicodedata

    assert unicodedata.category(symbol) == 'Sc'
    for source in (f'{symbol}{spacing}10', f'10{spacing}{symbol}'):
        snippet = {'snippet_id': 'S1', 'citation_id': 'C1', 'quote': source}
        assert quantity_catalog([snippet], source_context={'C1': source}) == []


def test_shared_explicit_suffix_has_exact_unit_provenance():
    source = 'Capacity rose from 240 to 315 concurrent sessions.'
    snippet = {'snippet_id': 'S1', 'citation_id': 'C1', 'quote': source}
    q = quantity_catalog([snippet], source_context={'C1': source})
    assert [(item['source_text'], item['value'], item['unit']) for item in q] == [
        ('240', '240', 'session'), ('315 concurrent sessions', '315', 'session')]
    for item in q:
        assert source[item['unit_source_start']:item['unit_source_end']] == 'concurrent sessions'
        assert item['unit_source_text'] == 'concurrent sessions'
        assert source[item['expression_start']:item['expression_end']] == 'from 240 to 315 concurrent sessions'
    assert calculate('difference', ['N1', 'N2'], q)['result'] == '75'
    forged = copy.deepcopy(q)
    forged[0]['unit_source_text'] = 'errors'
    with pytest.raises(ValueError):
        calculate('difference', ['N1', 'N2'], forged)


def test_shared_time_unit_and_independent_conflicting_units():
    q = catalog('Latency rose from 100 to 200ms.')
    assert [(item['value'], item['unit']) for item in q] == [('100', 'ms'), ('200', 'ms')]
    assert calculate('difference', ['N1', 'N2'], q)['result'] == '0.1'
    q = catalog('from 100 ms to 200 seconds')
    assert [(item['value'], item['unit']) for item in q] == [('100', 'ms'), ('200', 's')]
    q = catalog('from 100 errors to 200 requests')
    assert calculate('difference', ['N1', 'N2'], q)['status'] == 'unknown'


@pytest.mark.parametrize('cut', [8, 12, 17, 20, 23])
def test_shared_expression_requires_complete_presented_span(cut):
    source = 'from 240 to 315 concurrent sessions'
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': source[:cut]}]
    assert quantity_catalog(snippets, source_context={'C1': source}) == []


def test_shared_expression_requires_one_snippet_and_rejects_composites():
    source = 'from 240 to 315 sessions'
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': source[:12]},
                {'snippet_id': 'S2', 'citation_id': 'C1', 'quote': source[12:]}]
    assert quantity_catalog(snippets, source_context={'C1': source}) == []
    # Neither a rate nor a hyphen range licenses a shared unit.
    assert catalog('from 100 to 200 ms/s') == []
    assert catalog('from 100 to 200 widgets') == []
    assert catalog('100-200ms') == []


@pytest.mark.parametrize('suffix', ['₹', ' ₹', '\u00a0₩', '*'])
def test_shared_unit_rejects_unsupported_trailing_boundary(suffix):
    source = 'from 10 to 20 seconds' + suffix
    snippet = {'snippet_id': 'S1', 'citation_id': 'C1', 'quote': source}
    assert quantity_catalog([snippet], source_context={'C1': source}) == []


def test_generic_percent_difference_is_points_not_relative_change():
    q = catalog('Before 8%.', 'After 6%.')
    difference = calculate('difference', ['N1', 'N2'], q)
    assert (difference['result'], difference['unit']) == ('-2', 'percentage points')
    assert difference['text'] == 'Signed percentage-point difference: -2 percentage points.'
    assert difference['evaluated_expression'] == '6 % - 8 % = -2 percentage points (rounded)'
    relative = calculate('percent_change', ['N1', 'N2'], q)
    assert (relative['result'], relative['unit']) == ('-25', '%')

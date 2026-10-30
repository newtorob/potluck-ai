import copy
import pytest
from potluck_local.services.investigation_calculations import quantity_catalog, calculate, extraction_notices


def snippets(*texts):
    return [{'snippet_id': f'S{i}', 'citation_id': f'C{i}', 'quote': t}
            for i, t in enumerate(texts, 1)]


def test_versioned_generic_units_and_word_numbers():
    s = snippets('There were twenty-five tickets.', 'Now 30 tickets.')
    assert quantity_catalog(s) == []
    q = quantity_catalog(s, version='v2')
    assert [(x['value'], x['unit'], x['source_text']) for x in q] == [
        ('25', 'tickets', 'twenty-five tickets'), ('30', 'tickets', '30 tickets')]
    result = calculate('difference', ['N1', 'N2'], q, version='v2')
    assert (result['result'], result['unit'], result['algorithm_version']) == ('5', 'tickets', 'cited-calculations-v2')
    with pytest.raises(ValueError):
        calculate('difference', ['N1', 'N2'], q)


def test_v2_shared_units_provenance_and_source_cut():
    source = 'There were from eleven to twenty widgets.'
    s = snippets(source)
    q = quantity_catalog(s, source_context={'C1': source}, version='v2')
    assert [x['value'] for x in q] == ['11', '20']
    assert all(x['unit_source_text'] == 'widgets' for x in q)
    assert calculate('difference', ['N1', 'N2'], q, version='v2')['result'] == '9'
    s[0]['quote'] = source[:-5]
    assert quantity_catalog(s, source_context={'C1': source}, version='v2') == []


def test_generic_units_are_lexical_not_inferred_singular_equivalents():
    q = quantity_catalog(snippets('2 policies.', '4 policies.', '4 policy.'), version='v2')
    assert [(x['value'], x['unit']) for x in q] == [('2', 'policies'), ('4', 'policies')]
    assert calculate('difference', ['N1', 'N2'], q, version='v2')['result'] == '2'


@pytest.mark.parametrize('source', ['₹ 10 widgets', 'USD 10 widgets', '10 widgets / second',
    '1, 234 widgets', '100-200 widgets', '2024-10-20 widgets', 'abc-10 widgets',
    'one hundred and five widgets', 'one to two widgets', '10 GiB', '12 days', 'ten dollars',
    '10 seconds ₹', '10 widgets ₩', 'twenty widgets / hour', '1.2.3 tickets'])
def test_v2_unsupported_not_misparsed(source):
    assert quantity_catalog(snippets(source), version='v2') == []


def test_notices_are_bounded_local_and_do_not_claim_absence():
    source = 'One failure. 10 GiB and 3/4 widgets.'
    s = snippets(source)
    q = quantity_catalog(s, source_context={'C1': source}, version='v2')
    notices = extraction_notices(s, q, source_context={'C1': source})
    assert notices and len(notices) <= 50
    for notice in notices:
        assert source[notice['start']:notice['end']] == notice['source_text']
        assert notice['citation_id'] == 'C1'
        assert 'not extracted' in notice['reason']
    with pytest.raises(ValueError):
        extraction_notices(s, copy.deepcopy(q) + copy.deepcopy(q), source_context={'C1': source})


def test_v2_rejects_tampering_and_bad_version():
    q = quantity_catalog(snippets('two tickets', 'four tickets'), version='v2')
    q[0]['value'] = '10'
    with pytest.raises(ValueError):
        calculate('difference', ['N1', 'N2'], q, version='v2')
    with pytest.raises(ValueError):
        quantity_catalog([], version='v5')


def test_unsupported_word_number_has_source_local_notice():
    source = 'Observed one hundred and five widgets.'
    s = snippets(source)
    q = quantity_catalog(s, source_context={'C1': source}, version='v2')
    notices = extraction_notices(s, q, source_context={'C1': source})
    assert notices and notices[0]['source_text'].startswith('one hundred')
    assert all(n['source_text'] in source for n in notices)


@pytest.mark.parametrize('source', [
    'fourteen point five seconds', '5 ± 2 users', 'five +/- two users',
    'between five and ten widgets', 'between 5 and 10 widgets',
    'twenty to thirty bolts', '20 to 30 bolts', '5 to 10ms', 'between 5 and 10ms', 'one hundred and twenty bolts',
    'one hundred twenty bolts', 'three quarters of 20 users',
])
def test_ambiguous_numbers_do_not_become_partial_quantities(source):
    q = quantity_catalog(snippets(source), version='v2')
    if source == 'three quarters of 20 users':
        assert [(item['value'], item['unit']) for item in q] == [('20', 'user')]
    else:
        assert q == []


def test_explicit_shared_range_still_supported_and_singular_session_alias():
    s = snippets('from five to ten widgets', '1 concurrent session', '2 concurrent sessions')
    q = quantity_catalog(s, version='v2')
    assert [(item['value'], item['unit']) for item in q] == [
        ('5', 'widgets'), ('10', 'widgets'), ('1', 'session'), ('2', 'session')]
    assert calculate('difference', ['N3', 'N4'], q, version='v2')['result'] == '1'


@pytest.mark.parametrize('source,cut', [('fourteen point five seconds', 15),
                                        ('between 5 and 10ms', 12),
                                        ('5 ± 2 users', 4)])
def test_ambiguous_expression_does_not_leak_through_snippet_split(source, cut):
    s = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': source[:cut]},
         {'snippet_id': 'S2', 'citation_id': 'C1', 'quote': source[cut:]}]
    assert quantity_catalog(s, source_context={'C1': source}, version='v2') == []


def test_compound_units_keep_exact_identity_and_evidence():
    source = 'Before zero invoice errors. After 12 invoice errors.'
    q = quantity_catalog(snippets(source), source_context={'C1': source}, version='v3')
    assert [(x['value'], x['unit'], x['source_text']) for x in q] == [
        ('0', 'invoice errors', 'zero invoice errors'), ('12', 'invoice errors', '12 invoice errors')]
    assert all(source[x['start']:x['end']] == x['source_text'] for x in q)
    assert calculate('difference', ['N1', 'N2'], q, version='v3')['result'] == '12'
    assert quantity_catalog(snippets(source)) == []
    assert quantity_catalog(snippets(source), version='v2') == []


def test_compound_shared_unit_and_truncated_source():
    source = 'from five to ten customer renewals'
    q = quantity_catalog(snippets(source), source_context={'C1': source}, version='v3')
    assert [(x['value'], x['unit']) for x in q] == [('5', 'customer renewals'), ('10', 'customer renewals')]
    assert all(x['unit_source_text'] == 'customer renewals' for x in q)
    assert quantity_catalog(snippets(source[:-3]), source_context={'C1': source}, version='v3') == []


@pytest.mark.parametrize('source', ['1.5 invoice errors', '5 ± 2 invoice errors',
    'between 5 and 10 service contracts', '$10 invoice errors', '10 invoice errors / hour'])
def test_unsafe_compound_quantities_are_not_extracted(source):
    assert quantity_catalog(snippets(source), version='v3') == []


def test_compound_extraction_does_not_merge_across_common_clause_words():
    q = quantity_catalog(snippets('10 tickets increased. 12 tickets and 3 service contracts.'), version='v3')
    assert [(x['value'], x['unit']) for x in q] == [
        ('10', 'tickets'), ('12', 'tickets'), ('3', 'service contracts')]


@pytest.mark.parametrize('source', ['one hundred tickets', 'twenty million widgets',
    '3 square meters', 'from one to two hundred tickets', '12 customer renewal tickets',
    '12 customer renewals tickets', 'from five to ten customer renewals tickets',
    'one trillion and five support tickets', 'one quadrillion and five support tickets',
    'one dozen and five support tickets', '4 billing accounts\nreceivable'])
def test_v3_does_not_extract_partial_scales_or_long_units(source):
    assert quantity_catalog(snippets(source), version='v3') == []


def test_v4_compound_days_support_both_requested_operations():
    source = '20 calendar days. 15 calendar days.'
    s = snippets(source)
    q = quantity_catalog(s, source_context={'C1': source}, version='v4')
    assert [(v['source_text'], v['unit']) for v in q] == [
        ('20 calendar days', 'calendar day'), ('15 calendar days', 'calendar day')]
    difference = calculate('difference', ['N1', 'N2'], q, version='v4')
    change = calculate('percent_change', ['N1', 'N2'], q, version='v4')
    assert (difference['result'], difference['unit']) == ('-5', 'calendar day')
    assert (change['result'], change['unit']) == ('-25', '%')
    assert extraction_notices(s, q, source_context={'C1': source}, version='v4') == []
    assert quantity_catalog(s, source_context={'C1': source}, version='v3') == []


@pytest.mark.parametrize('unit', ['business days', 'working days'])
def test_v4_compound_days_do_not_mix(unit):
    q = quantity_catalog(snippets(f'20 calendar days. 15 {unit}.'), version='v4')
    assert calculate('difference', ['N1', 'N2'], q, version='v4')['status'] == 'unknown'


@pytest.mark.parametrize('source', [
    'between 10 and 20 calendar days', '10-20 calendar days',
    'not 20 calendar days', 'at least 20 calendar days',
    '20 calendar days per month', '20 calendar days remaining',
])
def test_v4_compound_days_reject_ambiguous_forms(source):
    assert quantity_catalog(snippets(source), version='v4') == []


def test_v4_shared_compound_and_source_prefix_boundaries():
    source = 'from 20 to 15 calendar days.'
    q = quantity_catalog(snippets(source), source_context={'C1': source}, version='v4')
    assert [item['value'] for item in q] == ['20', '15']
    assert quantity_catalog(snippets(source[:-5]), source_context={'C1': source}, version='v4') == []
    with pytest.raises(ValueError, match='source context'):
        quantity_catalog(snippets('20 calendar days'), source_context={'C1': '15 calendar days'}, version='v4')


@pytest.mark.parametrize('source', [
    'The duration was not exactly 5 calendar days.',
    'The duration did not change from 20 to 15 calendar days.',
    'The duration was approximately equal to 5 calendar days.',
])
def test_v4_compound_days_reject_qualified_clause_and_report_notice(source):
    s = snippets(source)
    q = quantity_catalog(s, source_context={'C1': source}, version='v4')
    assert q == []
    notices = extraction_notices(s, q, source_context={'C1': source}, version='v4')
    assert notices and all(n['citation_id'] == 'C1' for n in notices)


def test_v4_qualified_clause_guard_does_not_cross_sentence():
    source = 'This was not measured. The duration was 5 calendar days.'
    q = quantity_catalog(snippets(source), version='v4')
    assert [v['source_text'] for v in q] == ['5 calendar days']

from potluck_local.services.investigation_context import snippets_from_evidence
from potluck_local.services.investigation_calculations import quantity_catalog
import pytest


def record(text, cid='source:a:b'):
    return {'kind': 'source_chunk', 'citation_id': cid, 'presented_content': text}


def test_paragraphs_keep_hard_wraps_lists_decimals_and_code():
    content = 'First 1.2 value\ncontinues here.\n\n1. First item\n2. Second item\n\n```\na = 2.5\nb = 3\n```\n'
    snippets, trace = snippets_from_evidence([record(content)])
    quotes = [s['quote'] for s in snippets]
    assert 'First 1.2 value\ncontinues here.\n' in quotes
    assert any('1. First item' in q for q in quotes)
    assert any('2. Second item' in q for q in quotes)
    assert any('a = 2.5\nb = 3' in q for q in quotes)
    assert all(q in content for q in quotes)
    assert not trace['truncated']
    assert snippets_from_evidence([record(content)])[0] == snippets


def test_bounds_and_source_binding():
    content = ('x' * 501 + '\n\n') * 110
    snippets, trace = snippets_from_evidence([record(content)])
    assert len(snippets) == 100
    assert all(len(s['quote']) <= 500 and s['quote'] in content and s['citation_id'] == 'source:a:b' for s in snippets)
    assert trace['truncated'] and trace['omitted_snippets'] and trace['split_blocks']


def test_v1_replay_preserves_historical_blank_line_loss():
    content = 'A customer.\n\n- One organization\n'
    expected = [{'snippet_id': 'S1', 'citation_id': 'source:a:b', 'quote': 'A customer.\n'},
                {'snippet_id': 'S2', 'citation_id': 'source:a:b', 'quote': '- One organization\n'}]
    snippets, trace = snippets_from_evidence([record(content)], version='paragraph-v1')
    assert snippets == expected
    assert snippets_from_evidence([record(content)]) == (snippets, trace)
    assert trace == {'version': 'paragraph-v1', 'max_snippets': 100, 'max_quote_length': 500,
                     'omitted_snippets': 0, 'split_blocks': 0, 'truncated': False}


def test_v2_blank_line_list_quantity_catalog_source_offsets():
    content = 'Enterprise is a valid starting customer.\n\n- One organization has 42 seats.\n'
    cid = 'source:a:b'
    source_context = {cid: content}
    old, _ = snippets_from_evidence([record(content)])
    with pytest.raises(ValueError, match='snippets do not match source context'):
        quantity_catalog(old, source_context=source_context, version='v3')

    snippets, _ = snippets_from_evidence([record(content)], version='paragraph-v2')
    catalog = quantity_catalog(snippets, source_context=source_context, version='v3')
    assert len(catalog) == 1
    quantity = catalog[0]
    quote = next(s['quote'] for s in snippets if s['snippet_id'] == quantity['snippet_id'])
    assert quantity['citation_id'] == cid
    assert quantity['snippet_text'] == quote
    assert quote[quantity['start']:quantity['end']] == quantity['source_text'] == '42 seats'
    source_offset = sum(len(s['quote']) for s in snippets[:next(
        i for i, s in enumerate(snippets) if s['snippet_id'] == quantity['snippet_id'])])
    assert content[source_offset + quantity['start']:source_offset + quantity['end']] == '42 seats'


@pytest.mark.parametrize('content', [
    'Enterprise is a valid starting customer.\n\n- One organization, one agreed workflow\n',
    ' \n\n- first\n\n2. second\n \t\n',
    'a' * 1200 + '\n\n' + ' ' * 1100 + 'last',
    '```\n- code\n\n' + 'x' * 900 + '\n```\n\n- list\n',
    ('paragraph\n\n- list\n' * 150),
])
def test_v2_quotes_form_exact_source_prefix(content):
    snippets, trace = snippets_from_evidence([record(content)], version='paragraph-v2')
    quotes = [s['quote'] for s in snippets]
    joined = ''.join(quotes)
    assert content.startswith(joined)
    assert all(0 < len(q) <= 500 and q in content for q in quotes)
    assert len(snippets) <= 100
    assert trace['omitted_characters'] == len(content) - len(joined)
    assert trace['truncated'] == (len(joined) < len(content))
    assert bool(trace['omitted_snippets']) == trace['truncated']


def test_v2_multiple_sources_and_whitespace_only():
    contents = {'a': '\n  first\n\n- item\n', 'b': 'b' * 520, 'c': ' \n\t'}
    snippets, trace = snippets_from_evidence(
        [record(text, cid) for cid, text in contents.items()], version='paragraph-v2')
    for cid in ('a', 'b'):
        assert ''.join(s['quote'] for s in snippets if s['citation_id'] == cid) == contents[cid]
    assert not any(s['citation_id'] == 'c' for s in snippets)
    assert trace['whitespace_only_characters'] == len(contents['c'])
    assert trace['omitted_characters'] == 0 and not trace['truncated']


def test_v2_cap_reports_omissions_across_sources():
    contents = {'a': 'a' * 51000, 'b': '\n\n- subsequent source'}
    snippets, trace = snippets_from_evidence(
        [record(text, cid) for cid, text in contents.items()], version='paragraph-v2')
    assert len(snippets) == 100
    for cid, content in contents.items():
        assert content.startswith(''.join(s['quote'] for s in snippets if s['citation_id'] == cid))
    assert trace['omitted_characters'] == sum(map(len, contents.values())) - sum(len(s['quote']) for s in snippets)
    assert trace['truncated'] and trace['omitted_snippets'] > 0


def test_unknown_version_rejected():
    with pytest.raises(ValueError):
        snippets_from_evidence([], version='paragraph-v3')

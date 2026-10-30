from potluck_local.services.investigation_context import snippets_from_evidence


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

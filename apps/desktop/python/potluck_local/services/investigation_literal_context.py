"""Merge only standalone question connective fragments, without changing text."""
import re

# These fragments are syntax only; neither the following proposition nor the
# branch condition is classified, answered or rewritten.
VERSION = 'literal-context-coalescence-v1'
_PREFIX = re.compile(
    r'\A\s*[,;:]?\s*(?:if\s+(?:not|so)\b\s*[,;:]?\s*)?'
    r'(?:(?:and|or|but)\b\s*)?(?:what|which|who|where|when|how|why)?\s*[,;:]?\s*\Z',
    re.I,
)



def coalesce_context_prefixes(clauses):
    """Return exact merged literals and per-output original indices/codepoint spans."""
    if not isinstance(clauses, list) or not clauses or any(not isinstance(c, str) or not c.strip() for c in clauses):
        raise ValueError('Invalid literal clauses')
    output, mapping, pending = [], [], []
    offset = 0
    for index, text in enumerate(clauses):
        start, end = offset, offset + len(text)
        offset = end
        pending.append({'index': index, 'start': start, 'end': end})
        if re.search(r'[A-Za-z]', text) and _PREFIX.fullmatch(text):
            continue
        first = pending[0]['index']
        merged = ''.join(clauses[first:index + 1])
        output.append(merged)
        mapping.append({'output_index': len(output) - 1, 'start': pending[0]['start'],
                        'end': end, 'originals': pending})
        pending = []
    if pending:
        raise ValueError('Question mapping needs review')
    if ''.join(output) != ''.join(clauses):
        raise ValueError('Literal context reconstruction failed')
    return {'clauses': output, 'mapping': mapping, 'offset_unit': 'unicode_codepoints'}

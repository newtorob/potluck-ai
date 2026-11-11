"""Conservative exact extraction of explicitly supported per-second rates."""
import re
from typing import Any

from .investigation_quantities_v4 import (
    _negated_or_bounded, _safe, _value, extraction_notices_v4, source_matches_v4,
)

# Aliases identify the same numerator and denominator; no rate conversion is done.
_RATE = re.compile(
    r'(?i)(?<![\w.,/+−–—])(?P<number>[+-]?[0-9]+(?:\.[0-9]+)?)'
    r'[ \t]+(?P<unit>(?:tokens?|tok|requests?|req)[ \t]*(?:/[ \t]*|[ \t]+per[ \t]+)'
    r'(?:s|sec|secs|second|seconds))(?![\w/])'
)
# Block older extraction from picking up pieces of unsupported rate expressions.
_RATE_LIKE = re.compile(
    r'(?i)(?<!\w)[+-]?[\d.,]+(?:[eE][+-]?\d+)?\s*'
    r'(?:tokens?|toks?|requests?|reqs?)\s*(?:/|per\b)[^\s,;!?)]*'
)
_TRAILING_OPERATOR = re.compile(r'\s*(?:[/^:*×]|per\b|[-−–—+]\s*\d)', re.I)


def source_matches_v5(source: str, prefix_length: int) -> list[dict[str, Any]]:
    blocked = [m.span() for m in _RATE_LIKE.finditer(source) if m.start() < prefix_length]
    additions = []
    for match in _RATE.finditer(source):
        if match.start() >= prefix_length:
            break
        start, end = match.span()
        value = _value(match.group('number'))
        # Markdown emphasis is presentation, not part of the quantity span.
        emphasized = source[max(0, start - 2):start] == '**' and source[end:end + 2] == '**'
        if source[max(0, start - 2):start] == '**' and not emphasized:
            continue
        tail = source[end + 2:] if emphasized else source[end:]
        safe_source = source
        if emphasized:
            safe_source = source[:start - 2] + '  ' + source[start:end] + '  ' + source[end + 2:]
        previous = safe_source[:start].rstrip()
        clause = re.split(r'[;!?\n]', previous)[-1]
        if re.search(r'(?i)\b(?:between|from)\b.*\b(?:and|to)\s*$', clause):
            continue
        # Emphasis must not hide a partial number, identifier, or operator.
        if emphasized and start > 2 and (source[start - 3].isalnum() or source[start - 3] in '._,/%+-−–—'):
            continue
        if (value is None or _negated_or_bounded(safe_source, start)
                or re.search(r'(?i)\b(?:between|from)\s*$', previous)
                or re.search(r'(?i)\b(?:between|from)\s+\d+(?:\.\d+)?\s+(?:and|to)\s*$', previous)
                or not _safe(safe_source, start, end)
                or _TRAILING_OPERATOR.match(tail)
                or re.match(r'\s*(?:to|through|and)\s+[+-]?\d', tail, re.I)):
            continue
        raw = match.group('unit').lower()
        numerator = 'token' if re.match(r'tok', raw) else 'request'
        additions.append({'start': start, 'end': end, 'value': value,
                          'unit': numerator + '/s', 'dimension': 'rate'})
    base = [entry for entry in source_matches_v4(source, prefix_length)
            if not any(entry['start'] < end and start < entry['end'] for start, end in blocked)
            and not any(entry['start'] < item['end'] and item['start'] < entry['end'] for item in additions)]
    return sorted(base + additions, key=lambda item: item['start'])


def extraction_notices_v5(snippets: list[dict[str, Any]], quantities: list[dict[str, Any]], *,
                          source_context: dict[str, str] | None = None) -> list[dict[str, Any]]:
    notices = extraction_notices_v4(snippets, quantities, source_context=source_context, _version='v5')
    if len(notices) >= 50:
        return notices
    for snippet in snippets:
        quote = snippet['quote']
        for match in re.finditer(r'(?i)(?<![\w.])\d+(?:\.\d+)?e[+-]?\d+', quote):
            if any(n['snippet_id'] == snippet['snippet_id'] and n['start'] <= match.start() < n['end']
                   for n in notices):
                continue
            end = min(len(quote), match.end() + 40)
            notices.append({'snippet_id': snippet['snippet_id'], 'citation_id': snippet['citation_id'],
                            'source_text': quote[match.start():end], 'start': match.start(), 'end': end,
                            'reason': 'Number-like source text was not extracted; its format or unit may be unsupported or incomplete.'})
            if len(notices) >= 50:
                return notices
    return notices

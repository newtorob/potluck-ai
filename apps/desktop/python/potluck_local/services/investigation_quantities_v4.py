"""Bounded v4 extraction with explicit compound day units.

At most two generic words are retained; lexical form does not establish
nounhood, equivalence, or source comparability.
"""
from decimal import Decimal
import re
from typing import Any
import unicodedata

from . import investigation_calculations as v1

_SMALL = dict(zip('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split(), range(20)))
_TENS = dict(zip('twenty thirty forty fifty sixty seventy eighty ninety'.split(), range(20, 100, 10)))
_SMALL_PATTERN = '|'.join(sorted(_SMALL, key=len, reverse=True))
_TENS_PATTERN = '|'.join(_TENS)
_NUMBER_PATTERN = (rf'(?:[+-]?[0-9]+(?:\.[0-9]+)?|'
                   rf'(?:{_TENS_PATTERN})(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?'
                   rf'|(?:{_SMALL_PATTERN}))')
_UNIT_PATTERN = r'(?:concurrent\s+sessions?|[A-Za-z]{2,32}[ \t]+[A-Za-z]{3,32}|[A-Za-z]{2,32}|%)'
_SINGLE = re.compile(rf'(?i)(?<![\w.,/+−–—])(?P<number>{_NUMBER_PATTERN})\s+(?P<unit>{_UNIT_PATTERN})(?=$|[\s.,;:!?)\]}}])')
_SHARED = re.compile(rf'(?i)\bfrom\s+(?P<first>{_NUMBER_PATTERN})\s+to\s+'
                     rf'(?P<second>{_NUMBER_PATTERN})\s*(?P<unit>{_UNIT_PATTERN})(?=$|[\s.,;:!?)\]}}])')
_SIMPLE_UNIT = r'(?:concurrent\s+sessions?|[A-Za-z]{2,32}|%)'
_SINGLE_SIMPLE = re.compile(rf'(?i)(?<![\w.,/+−–—])(?P<number>{_NUMBER_PATTERN})\s+(?P<unit>{_SIMPLE_UNIT})(?=$|[\s.,;:!?)\]}}])')
_SHARED_SIMPLE = re.compile(rf'(?i)\bfrom\s+(?P<first>{_NUMBER_PATTERN})\s+to\s+'
                            rf'(?P<second>{_NUMBER_PATTERN})\s*(?P<unit>{_SIMPLE_UNIT})(?=$|[\s.,;:!?)\]}}])')
_EXCLUDED = set('hundred thousand million billion trillion quadrillion quintillion dozen score square cubic '
                'days weeks months years decades centuries dates times dollars cents euros pounds yen '
                'rupees bytes kilobytes megabytes gigabytes terabytes bits hertz watts volts amps '
                'meters metres kilometers miles feet inches grams kilograms liters litres degrees '
                'percent percentages points basis bps is was has does goes says means looks seems '
                'always sometimes perhaps versus across towards this these those hers his its '
                'less more plus minus tens hundreds thousands millions billions'.split())
_CLAUSE_WORDS = set('and or but nor while when where which that who whose because if then '
                    'were are have had did do can could will would should may might must '
                    'in on at by for from to of with without after before during as '
                    'increased decreased rose fell dropped grew remained became reported '
                    'shows showed indicates indicated caused reached exceeded matched'.split())
# These patterns identify ambiguous constructions, not new arithmetic operations.
# A number inside one of them is not independently selected as a count.
_AMBIGUOUS = re.compile(
    rf'(?i)(?<![\w])(?:'
    rf'{_NUMBER_PATTERN}\s+point\s+{_NUMBER_PATTERN}'
    rf'|{_NUMBER_PATTERN}\s*(?:±|\+\s*/\s*-)\s*{_NUMBER_PATTERN}'
    rf'|{_NUMBER_PATTERN}\s+(?:halves|half|quarters?|thirds?|fourths?|fifths?|sixths?|sevenths?|eighths?|ninths?|tenths?)\b'
    rf'|between\s+{_NUMBER_PATTERN}\s+and\s+{_NUMBER_PATTERN}'
    rf'|{_NUMBER_PATTERN}\s+to\s+{_NUMBER_PATTERN}'
    rf')(?![0-9])'
)
_SUFFIX_OPERATOR = re.compile(r'\s*(?:[/:^*×]|per\b|[-−–—+]\s*\d)', re.IGNORECASE)


def _value(text: str) -> str | None:
    text = text.lower()
    if text[0].isdigit() or text[0] in '+-':
        if len(text.lstrip('+-').replace('.', '')) > 18:
            return None
        number = Decimal(text)
        return str(number) if abs(number) <= Decimal('1e18') else None
    parts = re.split(r'[- ]', text)
    if len(parts) == 1:
        return str(_SMALL.get(parts[0], _TENS.get(parts[0])))
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _SMALL and 1 <= _SMALL[parts[1]] <= 9:
        return str(_TENS[parts[0]] + _SMALL[parts[1]])
    return None


def _unit(raw: str) -> tuple[str, str] | None:
    word = raw.lower()
    duration = re.fullmatch(r'(calendar|business|working)\s+days?', word)
    if duration:
        return duration.group(1) + ' day', 'compound_day'
    if re.fullmatch(r'concurrent\s+sessions?', word):
        return 'session', 'count'
    if word in v1._UNITS:
        return v1._UNITS[word]
    parts = word.split()
    if len(parts) == 2:
        first, last = parts
        if (first in _EXCLUDED or first in _CLAUSE_WORDS or first in v1._CONNECTORS
                or first in v1._UNITS or first.endswith('s') or last in _CLAUSE_WORDS
                or last in _EXCLUDED or last in v1._CONNECTORS
                or (last in v1._UNITS and v1._UNITS[last][1] != 'count')
                or not last.endswith('s') or last.endswith(('ss', 'us', 'is'))):
            return None
        return word, 'count'
    if (word in _EXCLUDED or word in v1._CONNECTORS or len(word) < 3 or not word.endswith('s')
            or word.endswith(('ss', 'us', 'is'))):
        return None
    return word, 'count'


def _safe(source: str, start: int, end: int) -> bool:
    previous = source[:start].rstrip()
    if v1._CURRENCY_PREFIX.search(previous) or (previous and (
            unicodedata.category(previous[-1]) == 'Sc' or previous[-1] in '/^'
            or previous[-1].isdigit())):
        return False
    if previous and previous[-1] in '-−–—':
        return False
    if previous and previous[-1] in ',:' and previous[:-1].rstrip()[-1:].isdigit():
        return False
    if re.search(r'(?i)\b(?:hundred|thousand|million|billion|trillion|quadrillion|quintillion|dozen|score)(?:\s+and)?$|\b(?:one|two|three|four|five|six|seven|eight|nine|[0-9]+)\s+to$', previous):
        return False
    if _SUFFIX_OPERATOR.match(source, end):
        return False
    cursor = end
    while cursor < len(source) and source[cursor].isspace():
        cursor += 1
    return cursor == len(source) or unicodedata.category(source[cursor]) != 'Sc'


def _compound_boundary(source: str, end: int) -> bool:
    # A third word may be part of the unit; never silently truncate it.
    following = re.match(r'\s+([A-Za-z]+)\b', source[end:])
    return not following or following.group(1).lower() in _CLAUSE_WORDS


def _negated_or_bounded(source: str, start: int) -> bool:
    # Stay within the current clause and permit a short intervening phrase,
    # such as "not exactly" or "did not change from". This is a conservative
    # lexical guard, not an inference about whether the source is true.
    clause = re.split(r'[.!?;\n\r]', source[:start])[-1][-160:]
    return bool(re.search(
        r'(?i)\b(?:not|no|never|under|over|less than|more than|at least|at most|'
        r'up to|approximately|about|around|roughly)\b'
        r'(?:[ \t]+[A-Za-z]+){0,4}[ \t]*$', clause))


def source_matches_v4(source: str, prefix_length: int) -> list[dict[str, Any]]:
    ambiguous = []
    for match in _AMBIGUOUS.finditer(source):
        if match.start() >= prefix_length:
            break
        # An explicit complete supported shared-unit expression is handled below.
        ambiguous.append(match.span())
    base = [entry for entry in v1._source_matches(source, prefix_length)
            if _safe(source, entry.get('expression_start', entry['start']),
                     entry.get('expression_end', entry['end']))]
    additions: list[dict[str, Any]] = []
    shared_ranges: list[tuple[int, int]] = []
    for match in sorted([*_SHARED.finditer(source), *_SHARED_SIMPLE.finditer(source)],
                        key=lambda m: (m.start(), -m.end())):
        if match.start() >= prefix_length:
            break
        if any(start <= match.start() < end for start, end in shared_ranges):
            continue
        unit = _unit(match.group('unit'))
        first, second = _value(match.group('first')), _value(match.group('second'))
        if (unit and unit[1] == 'compound_day' and _negated_or_bounded(source, match.start())):
            continue
        if (not unit or first is None or second is None
                or (' ' in unit[0] and unit[1] != 'compound_day' and ('.' in match.group('first') or '.' in match.group('second')))
                or (' ' in unit[0] and not _compound_boundary(source, match.end()))
                or not _safe(source, match.start(), match.end())):
            continue
        shared_ranges.append(match.span())
        common = {'unit': unit[0], 'dimension': unit[1], 'unit_source_text': match.group('unit'),
                  'unit_source_start': match.start('unit'), 'unit_source_end': match.end('unit'),
                  'expression_start': match.start(), 'expression_end': match.end()}
        additions.extend([{**common, 'start': match.start('first'), 'end': match.end('first'), 'value': first},
                          {**common, 'start': match.start('second'), 'end': match.end('unit'), 'value': second}])
    for match in sorted([*_SINGLE.finditer(source), *_SINGLE_SIMPLE.finditer(source)],
                        key=lambda m: (m.start(), -m.end())):
        if match.start() >= prefix_length:
            break
        if any(start <= match.start() < end for start, end in shared_ranges):
            continue
        if any(item['start'] <= match.start() < item['end'] for item in additions):
            continue
        unit, value = _unit(match.group('unit')), _value(match.group('number'))
        if unit and unit[1] == 'compound_day' and _negated_or_bounded(source, match.start()):
            continue
        if (unit and value is not None and not (' ' in unit[0] and unit[1] != 'compound_day' and '.' in match.group('number'))
                and not (' ' in unit[0] and not _compound_boundary(source, match.end()))
                and _safe(source, match.start(), match.end())):
            additions.append({'start': match.start(), 'end': match.end(), 'value': value,
                              'unit': unit[0], 'dimension': unit[1]})
    # A complete supported shared-unit expression overrides the range block.
    ambiguous = [(left, right) for left, right in ambiguous
                 if not any(start <= left and right <= end for start, end in shared_ranges)]
    base = [entry for entry in base if not any(left <= entry['start'] < right for left, right in ambiguous)]
    additions = [entry for entry in additions if not any(left <= entry['start'] < right for left, right in ambiguous)]
    results = [entry for entry in base if not any(
        (entry['start'] < item.get('expression_end', item['end'])
         and item.get('expression_start', item['start']) < entry['end']) for item in additions)]
    results.extend(additions)
    return sorted(results, key=lambda item: item['start'])


def extraction_notices_v4(snippets: list[dict[str, Any]], quantities: list[dict[str, Any]], *,
                          source_context: dict[str, str] | None = None) -> list[dict[str, Any]]:
    expected = v1.quantity_catalog(snippets, source_context=source_context, version='v4')
    if quantities != expected:
        raise ValueError('quantity catalog does not match snippets')
    notices: list[dict[str, Any]] = []
    number_like = re.compile(rf'(?i)(?<![\w])(?:[+-]?[0-9]+(?:[.,/][0-9]+)*|'
                             rf'(?:{_TENS_PATTERN})(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|'
                             rf'(?:{_SMALL_PATTERN}))(?![A-Za-z])')
    for snippet in snippets:
        quote, sid = snippet['quote'], snippet['snippet_id']
        spans = [(q['start'], q['end']) for q in quantities if q['snippet_id'] == sid]
        for match in number_like.finditer(quote):
            if any(start <= match.start() < end for start, end in spans):
                continue
            end = min(len(quote), match.end() + 40)
            notices.append({'snippet_id': sid, 'citation_id': snippet['citation_id'],
                            'source_text': quote[match.start():end], 'start': match.start(), 'end': end,
                            'reason': 'Number-like source text was not extracted; its format or unit may be unsupported or incomplete.'})
            if len(notices) >= 50:
                return notices
    return notices

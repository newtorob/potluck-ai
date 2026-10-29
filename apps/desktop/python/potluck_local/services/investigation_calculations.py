"""Conservative source-bound arithmetic for diagnostic investigation briefs.

This validates arithmetic, not the meaning, authority or comparability of sources.
A difference between percent inputs is measured in percentage points; relative
percent change is a separate operation.
"""
from collections.abc import Iterator
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re
from typing import Any
import unicodedata

_ALGORITHM_VERSION = 'cited-calculations-v1'
_CAVEAT = 'Arithmetic only; source selection, authority, completeness and comparability are not verified.'
_UNITS = {
    'ms': ('ms', 'time'), 'millisecond': ('ms', 'time'), 'milliseconds': ('ms', 'time'),
    's': ('s', 'time'), 'sec': ('s', 'time'), 'secs': ('s', 'time'),
    'second': ('s', 'time'), 'seconds': ('s', 'time'),
    'min': ('min', 'time'), 'mins': ('min', 'time'),
    'minute': ('min', 'time'), 'minutes': ('min', 'time'),
    'h': ('h', 'time'), 'hr': ('h', 'time'), 'hrs': ('h', 'time'),
    'hour': ('h', 'time'), 'hours': ('h', 'time'),
    '%': ('%', 'percent'),
}
# Count units have explicitly equivalent singular and plural forms only.
for _word in ('request', 'error', 'failure', 'event', 'item', 'user', 'job', 'task',
              'incident', 'attempt', 'response', 'machine', 'server', 'node', 'call', 'session'):
    _UNITS[_word] = (_word, 'count')
    _UNITS[_word + 's'] = (_word, 'count')
_FACTORS = {'ms': Decimal('0.001'), 's': Decimal(1), 'min': Decimal(60), 'h': Decimal(3600)}
_OPERATIONS = {'difference', 'percentage_point_difference', 'percent_change', 'percentage'}
# Words that cannot themselves plausibly be a unit. Other words make a bare
# number ambiguous and are intentionally excluded.
_CONNECTORS = {'to', 'and', 'or', 'from', 'before', 'after', 'was', 'were', 'is', 'are',
               'in', 'on', 'at', 'of', 'for', 'with', 'but', 'than', 'as'}
_NUMBER = re.compile(r'(?<![\w.,/+−–—$€£¥%])[-+]?[0-9]+(?:\.[0-9]+)?')
_WORD = re.compile(r'[A-Za-z]+')
_CURRENCY_PREFIX = re.compile(r'(?i)(?<![A-Za-z])(?:USD|EUR|GBP|JPY|CAD|AUD|CHF|CNY|INR|NZD|HKD|SGD)\s*$')
_ID = re.compile(r'N[1-9][0-9]*\Z')


def _extract(quote: str, *, max_start: int | None = None) -> Iterator[tuple[int, int, str, str, str]]:
    """Return accepted exact spans; reject partial matches in unsupported forms."""
    for match in _NUMBER.finditer(quote):
        start, end = match.span()
        if max_start is not None and start >= max_start:
            break
        number = match.group()
        previous = quote[:start].rstrip()
        if _CURRENCY_PREFIX.search(previous):
            continue
        if previous and (unicodedata.category(previous[-1]) == 'Sc' or previous[-1] in '/^'):
            continue
        if previous.endswith(':') and previous[:-1].rstrip()[-1:].isdigit():
            continue
        if previous[-1:].isdigit() or (previous.endswith(',') and previous[:-1].rstrip()[-1:].isdigit()):
            continue
        if previous and previous[-1] in '-−–—' and previous[:-1].rstrip()[-1:].isdigit():
            continue
        # Prevent partial matches within dates, fractions, ranges, scientific
        # notation, comma-grouped numbers, versions, currencies or identifiers.
        if end < len(quote) and (quote[end].isdigit() or quote[end] in '_,/:^$€£¥−–—'):
            continue
        if end < len(quote) and quote[end] in '.-' and end + 1 < len(quote) and quote[end + 1].isdigit():
            continue
        if start and (quote[start - 1] in '-−–—' or quote[start - 1].isdigit()):
            continue
        if len(number.lstrip('+-').replace('.', '')) > 18:
            continue
        try:
            value = Decimal(number)
        except InvalidOperation:
            continue
        if not value.is_finite() or abs(value) > Decimal('1e18'):
            continue
        cursor = end
        while cursor < len(quote) and quote[cursor].isspace():
            cursor += 1
        if cursor < len(quote) and unicodedata.category(quote[cursor]) == 'Sc':
            continue
        unit, dimension, span_end = '', 'dimensionless', end
        if cursor < len(quote) and quote[cursor] == '%':
            unit, dimension, span_end = '%', 'percent', cursor + 1
            if span_end < len(quote) and (quote[span_end].isalnum() or quote[span_end] in '%/'):
                continue
        else:
            word = _WORD.match(quote, cursor)
            if word:
                raw = word.group().lower()
                if raw in _UNITS:
                    unit, dimension = _UNITS[raw]
                    span_end = word.end()
                    # Composite units such as requests/s are unsupported.
                    if span_end < len(quote) and not (quote[span_end].isspace() or quote[span_end] in '.,;:!?)]}'):
                        continue
                elif raw not in _CONNECTORS:
                    continue
            elif cursor < len(quote) and not (quote[cursor] in '.,;:!?)]}\n\r' or quote[cursor].isspace()):
                continue
        tail = quote[span_end:].lstrip()
        if tail.startswith(('/', ':', '^')) or re.match(r'^(?:per\b|[-−–—+]\s*\d)', tail, re.IGNORECASE):
            continue
        yield start, span_end, str(value), unit, dimension


# This is the only supported shared-unit construction. The source span of the
# trailing unit and the entire construction are retained with both quantities.
_SHARED_START = re.compile(r'(?i)\bfrom\s+')
_SHARED = re.compile(
    r'(?i)from\s+(?P<first>[+-]?[0-9]+(?:\.[0-9]+)?)\s+to\s+'
    r'(?P<second>[+-]?[0-9]+(?:\.[0-9]+)?)\s*'
    r'(?P<unit>concurrent\s+sessions|'
    + '|'.join(re.escape(unit) for unit in sorted(_UNITS, key=len, reverse=True))
    + r')(?=$|[\s.,;:!?)\]}])'
)
_SHARED_CANDIDATE = re.compile(
    r'(?i)from\s+[+-]?[0-9]+(?:\.[0-9]+)?\s+to\s+'
    r'[+-]?[0-9]+(?:\.[0-9]+)?\s*(?P<suffix>[A-Za-z%°µ]+)'
)
_SHARED_COMPOSITE = re.compile(r'\s*(?:[/:^]|per\b|[-−–—+]\s*\d)', re.IGNORECASE)
_SHARED_FIELDS = ('unit_source_text', 'unit_source_start', 'unit_source_end',
                  'expression_start', 'expression_end')


def _source_matches(source: str, prefix_length: int) -> list[dict[str, Any]]:
    shared: list[tuple[re.Match[str], str, str, str, str]] = []
    blocked: list[tuple[int, int]] = []
    for marker in _SHARED_START.finditer(source):
        if marker.start() >= prefix_length:
            break
        match = _SHARED.match(source, marker.start())
        if not match:
            candidate = _SHARED_CANDIDATE.match(source, marker.start())
            if candidate and candidate.group('suffix').lower() not in _CONNECTORS:
                blocked.append(candidate.span())
            continue
        raw_unit = match.group('unit').lower()
        unit, dimension = ('session', 'count') if re.fullmatch(r'concurrent\s+sessions', raw_unit) else _UNITS[raw_unit]
        after_unit = match.end()
        while after_unit < len(source) and source[after_unit].isspace():
            after_unit += 1
        if (_SHARED_COMPOSITE.match(source, match.end())
                or (after_unit < len(source) and unicodedata.category(source[after_unit]) == 'Sc')):
            blocked.append(match.span())
            continue
        values = []
        for name in ('first', 'second'):
            raw = match.group(name)
            if len(raw.lstrip('+-').replace('.', '')) > 18:
                break
            value = Decimal(raw)
            if abs(value) > Decimal('1e18'):
                break
            values.append(str(value))
        if len(values) == 2:
            shared.append((match, values[0], values[1], unit, dimension))
        else:
            blocked.append(match.span())

    matches: list[dict[str, Any]] = []
    for start, end, value, unit, dimension in _extract(source, max_start=prefix_length):
        if (any(m.start() <= start < m.end() for m, *_ in shared)
                or any(left <= start < right for left, right in blocked)):
            continue
        matches.append({'start': start, 'end': end, 'value': value, 'unit': unit, 'dimension': dimension})
    for match, first, second, unit, dimension in shared:
        common = {'unit': unit, 'dimension': dimension,
                  'unit_source_text': match.group('unit'), 'unit_source_start': match.start('unit'),
                  'unit_source_end': match.end('unit'), 'expression_start': match.start(),
                  'expression_end': match.end()}
        matches.append({**common, 'start': match.start('first'), 'end': match.end('first'), 'value': first})
        matches.append({**common, 'start': match.start('second'), 'end': match.end('unit'), 'value': second})
    return sorted(matches, key=lambda item: item['start'])


def quantity_catalog(
    snippets: list[dict[str, Any]], *, source_context: dict[str, str] | None = None,
    version: str = 'v1'
) -> list[dict[str, Any]]:
    """Extract quantities; context prevents truncated presentation from changing tokens.

    If provided, each citation's ordered snippets must concatenate to a prefix
    of its full source. Only spans wholly contained in one snippet are emitted.
    Full context is used solely for validation and is never included in output.
    """
    if version not in ('v1', 'v2'):
        raise ValueError('invalid calculation version')
    if version == 'v2':
        from .investigation_quantities import source_matches_v2
        source_matches = source_matches_v2
    else:
        source_matches = _source_matches
    if not isinstance(snippets, list) or len(snippets) > 1000:
        raise ValueError('invalid snippets')
    if source_context is not None and (not isinstance(source_context, dict) or len(source_context) > 1000):
        raise ValueError('invalid source context')
    seen: set[str] = set()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for snippet in snippets:
        if not isinstance(snippet, dict):
            raise ValueError('invalid snippet')
        sid, cid, quote = snippet.get('snippet_id'), snippet.get('citation_id'), snippet.get('quote')
        if (not isinstance(sid, str) or not sid or sid in seen or not isinstance(cid, str)
                or not cid or not isinstance(quote, str) or len(quote) > 10000):
            raise ValueError('invalid snippet identity or text')
        seen.add(sid)
        grouped.setdefault(cid, []).append(snippet)

    spans: dict[str, list[dict[str, Any]]] = {}
    if source_context is not None:
        total_source_length = 0
        for cid, group in grouped.items():
            source = source_context.get(cid)
            if not isinstance(source, str) or len(source) > 2_097_152:
                raise ValueError('missing or oversized source context')
            total_source_length += len(source)
            if total_source_length > 8_388_608:
                raise ValueError('source context exceeds limit')
            prefix = ''.join(s['quote'] for s in group)
            if not source.startswith(prefix):
                raise ValueError('snippets do not match source context')
            offsets = []
            position = 0
            for snippet in group:
                next_position = position + len(snippet['quote'])
                offsets.append((position, next_position, snippet['snippet_id']))
                spans[snippet['snippet_id']] = []
                position = next_position
            span_index = 0
            for entry in source_matches(source, len(prefix)):
                start, end = entry['start'], entry['end']
                while span_index < len(offsets) and start >= offsets[span_index][1]:
                    span_index += 1
                if span_index == len(offsets):
                    break
                snippet_start, snippet_end, sid = offsets[span_index]
                # A shared unit is valid only when its complete expression was presented.
                bound_start = entry.get('expression_start', start)
                bound_end = entry.get('expression_end', end)
                if bound_start >= snippet_start and bound_end <= snippet_end:
                    local = dict(entry)
                    for key in ('start', 'end', 'unit_source_start', 'unit_source_end',
                                'expression_start', 'expression_end'):
                        if key in local:
                            local[key] -= snippet_start
                    spans[sid].append(local)

    catalog: list[dict[str, Any]] = []
    for snippet in snippets:
        sid, cid, quote = snippet['snippet_id'], snippet['citation_id'], snippet['quote']
        matches = spans[sid] if source_context is not None else source_matches(quote, len(quote))
        for entry in matches:
            if len(catalog) >= 1000:
                raise ValueError('too many quantities')
            start, end = entry['start'], entry['end']
            catalog.append({'id': f'N{len(catalog) + 1}', 'snippet_id': sid, 'citation_id': cid,
                            'source_text': quote[start:end], 'snippet_text': quote, **entry})
    return catalog


def _validate_catalog(quantities: list[dict[str, Any]], version: str = 'v1') -> dict[str, dict[str, Any]]:
    if not isinstance(quantities, list) or len(quantities) > 1000:
        raise ValueError('invalid quantity catalog')
    if version == 'v2':
        from .investigation_quantities import source_matches_v2
        source_matches = source_matches_v2
    else:
        source_matches = _source_matches
    by_id = {}
    seen_spans = set()
    snippets = {}
    for index, entry in enumerate(quantities, 1):
        if not isinstance(entry, dict):
            raise ValueError('invalid quantity')
        qid = entry.get('id')
        if not isinstance(qid, str) or not _ID.fullmatch(qid) or qid != f'N{index}' or qid in by_id:
            raise ValueError('invalid or duplicate quantity ID')
        sid, cid, quote = entry.get('snippet_id'), entry.get('citation_id'), entry.get('snippet_text')
        start, end = entry.get('start'), entry.get('end')
        if (not isinstance(sid, str) or not sid or not isinstance(cid, str) or not cid
                or not isinstance(quote, str) or len(quote) > 10000
                or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(quote)
                or entry.get('source_text') != quote[start:end]):
            raise ValueError('quantity does not match source')
        expected = {key: entry.get(key) for key in ('start', 'end', 'value', 'unit', 'dimension')}
        expected.update({key: entry[key] for key in _SHARED_FIELDS if key in entry})
        if expected not in source_matches(quote, len(quote)):
            raise ValueError('quantity does not match source')
        if (sid, start, end) in seen_spans or (sid in snippets and snippets[sid] != (cid, quote)):
            raise ValueError('duplicate or inconsistent source span')
        seen_spans.add((sid, start, end))
        snippets[sid] = (cid, quote)
        by_id[qid] = deepcopy(entry)
    return by_id


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, 'f')
    return rendered.rstrip('0').rstrip('.') if '.' in rendered else rendered


def calculate(operation: str, operand_ids: list[str], quantities: list[dict[str, Any]], *,
              version: str = 'v1') -> dict[str, Any]:
    """Compute a fixed operation, or return a reason it cannot be computed."""
    if version not in ('v1', 'v2'):
        raise ValueError('invalid calculation version')
    if operation not in _OPERATIONS or not isinstance(operand_ids, list) or len(operand_ids) > 2:
        raise ValueError('invalid operation or operand count')
    by_id = _validate_catalog(quantities, version)
    if any(not isinstance(qid, str) or qid not in by_id for qid in operand_ids):
        raise ValueError('invalid operand ID')
    if len(set(operand_ids)) != len(operand_ids):
        raise ValueError('duplicate operand ID')
    inputs = [deepcopy(by_id[qid]) for qid in operand_ids]
    formula = {'difference': 'after - before', 'percentage_point_difference': 'after - before',
               'percent_change': '(after - before) / before × 100',
               'percentage': 'part / total × 100'}[operation]
    result = {'status': 'unknown', 'text': '', 'formula': formula, 'result': None,
              'unit': None, 'reason': None, 'inputs': inputs,
              'algorithm_version': _ALGORITHM_VERSION if version == 'v1' else 'cited-calculations-v2', 'normalized_inputs': [], 'evaluated_expression': None,
              'rounding': {'mode': 'ROUND_HALF_UP', 'max_fractional_digits': 6}, 'caveat': _CAVEAT}

    def unknown(reason: str) -> dict[str, Any]:
        result['reason'] = reason
        result['text'] = 'Unknown: ' + reason
        return result

    if len(inputs) != 2:
        return unknown('Two distinct cited inputs are required.')
    first, second = inputs
    if first['dimension'] != second['dimension'] or (first['dimension'] == 'count' and first['unit'] != second['unit']):
        return unknown('Inputs have incompatible units.')
    if operation == 'percentage_point_difference' and first['dimension'] != 'percent':
        return unknown('Percentage-point difference requires percent inputs.')
    with localcontext() as ctx:
        ctx.prec = 60
        a, b = Decimal(first['value']), Decimal(second['value'])
        factor_a = _FACTORS[first['unit']] if first['dimension'] == 'time' else Decimal(1)
        factor_b = _FACTORS[second['unit']] if second['dimension'] == 'time' else Decimal(1)
        a *= factor_a
        b *= factor_b
        normalized_unit = 's' if first['dimension'] == 'time' else first['unit']
        roles = ('part', 'total') if operation == 'percentage' else ('before', 'after')
        result['normalized_inputs'] = [
            {'id': item['id'], 'role': role, 'value': _decimal_text(value), 'unit': normalized_unit,
             'original_value': item['value'], 'original_unit': item['unit'],
             'conversion_factor': _decimal_text(factor)}
            for item, role, value, factor in zip(inputs, roles, (a, b), (factor_a, factor_b))
        ]
        if operation in ('percent_change', 'percentage') and a == 0 and operation == 'percent_change':
            return unknown('The baseline is zero.')
        if operation == 'percentage' and b == 0:
            return unknown('The total is zero.')
        if operation in ('difference', 'percentage_point_difference'):
            value = b - a
        elif operation == 'percent_change':
            value = (b - a) / a * 100
        else:
            value = a / b * 100
        value = value.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)
        if value == 0:
            value = abs(value)
        rendered = _decimal_text(value)
        unit = ('percentage points' if operation in ('difference', 'percentage_point_difference') and first['dimension'] == 'percent' else
                '%' if operation in ('percent_change', 'percentage') else
                's' if first['dimension'] == 'time' else first['unit'])
        def operand_text(value: Decimal) -> str:
            return _decimal_text(value) + (f' {normalized_unit}' if normalized_unit else '')

        a_text, b_text = operand_text(a), operand_text(b)
        if operation in ('difference', 'percentage_point_difference'):
            expression = f'{b_text} - {a_text}'
        elif operation == 'percent_change':
            expression = f'({b_text} - {a_text}) / {a_text} × 100'
        else:
            expression = f'{a_text} / {b_text} × 100'
        output_text = rendered + (f' {unit}' if unit else '')
        result.update(status='computed', result=rendered, unit=unit, reason=None,
                      evaluated_expression=f'{expression} = {output_text} (rounded)')
        label = {'difference': 'Signed difference', 'percentage_point_difference': 'Signed percentage-point difference',
                 'percent_change': 'Signed percent change', 'percentage': 'Percentage'}[operation]
        if operation == 'difference' and first['dimension'] == 'percent':
            label = 'Signed percentage-point difference'
        result['text'] = f'{label}: {rendered}' + (f' {unit}' if unit else '') + '.'
        return result


def extraction_notices(snippets: list[dict[str, Any]], quantities: list[dict[str, Any]], *,
                       source_context: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Report bounded source-local number-like spans not extracted by v2."""
    from .investigation_quantities import extraction_notices_v2
    return extraction_notices_v2(snippets, quantities, source_context=source_context)

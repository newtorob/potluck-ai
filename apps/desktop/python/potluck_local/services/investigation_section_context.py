"""Retain source-bound Markdown section headings for selected evidence."""
from copy import deepcopy
import re


def _headings(text):
    rows = []
    offset = 0
    fence = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line.rstrip('\r\n'))
        if marker:
            run, tail = marker.groups()
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not tail.strip():
                fence = None
        elif fence is None:
            heading = re.match(r'^ {0,3}(#{1,6})(?:[ \t]+[^\r\n]*|[ \t]*)$', line.rstrip('\r\n'))
            if heading:
                rows.append({'start': offset, 'end': offset + len(line.rstrip('\r\n')),
                             'level': len(heading[1])})
        offset += len(line)
    for i, row in enumerate(rows):
        row['scope_end'] = next((later['start'] for later in rows[i+1:]
                                 if later['level'] <= row['level']), len(text))
    return rows


def retain(prepared, result):
    """Add only exact governing heading units, without asserting their meaning."""
    result = deepcopy(result)
    chosen = {u['id'] for u in result['selected']}
    added = set()
    for context in prepared['contexts']:
        units = [u for u in prepared['units'] if u['source_ids'] == context['source_ids']]
        selected = [u for u in units if u['id'] in chosen]
        for heading in _headings(context['text']):
            if not any(u['start'] < heading['scope_end'] and heading['start'] < u['end'] for u in selected):
                continue
            if any(u['start'] <= heading['start'] and heading['end'] <= u['end'] for u in selected):
                continue
            unit = next((u for u in units if u['start'] <= heading['start'] and heading['end'] <= u['end']), None)
            exact = context['text'][heading['start']:heading['end']]
            if unit is None or unit['quote'].strip() != exact.strip():
                # Promoting a larger paragraph could also promote unrelated claims.
                raise ValueError('Governing heading is not an isolated source unit')
            added.add(unit['id'])
    for uid in added:
        result['classifications'][uid] = 'dependency'
    selected_ids = chosen | added
    result['selected'] = [dict(deepcopy(u), classification=result['classifications'][u['id']])
                          for u in prepared['units'] if u['id'] in selected_ids]
    for bucket in ('background', 'uncertain'):
        result[bucket] = [u for u in result[bucket] if u['id'] not in added]
    result['structural_context_units'] = [u['id'] for u in prepared['units'] if u['id'] in added]
    return result

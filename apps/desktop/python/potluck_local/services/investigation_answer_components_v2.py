"""Versioned component composition with a larger supported-answer budget."""
from copy import deepcopy
import json

from . import investigation_answer_components as v1
from .investigation_planned_answers import _unique_object

VERSION = 'cited-components-v2'
MAX_COMPOSED_TEXT = 900
MAX_UNKNOWN_TEXT = v1.MAX_COMPOSED_TEXT
INSTRUCTION = v1.INSTRUCTION.replace(
    'The PRIMARY PLUS ALL COMPONENTS AND LABELS must fit 300 characters.',
    'For supported answers the PRIMARY PLUS ALL COMPONENTS AND LABELS must fit 900 characters; '
    'for unknown answers the total must fit 300 characters.')
response_schema = v1.response_schema
KINDS = v1.KINDS


def extract(raw, plan, snippets):
    if not isinstance(raw, str) or len(raw) > 60000:
        raise ValueError('Invalid component response')
    data = json.loads(raw, object_pairs_hook=_unique_object)
    masked = deepcopy(data)
    # Delegate the original field, citation, status and array validation to v1;
    # only its combined-text budget differs for supported answers.
    for row in plan:
        if row['operation'] != 'answer':
            continue
        item = masked.get(row['id']) if isinstance(masked, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get('groups'), list):
            raise ValueError('Invalid component groups')
        for group in item['groups']:
            if not isinstance(group, dict):
                raise ValueError('Invalid component group')
            for kind in KINDS:
                values = group.get(kind)
                if not isinstance(values, list):
                    raise ValueError('Invalid answer component array')
                original_values = []
                for i, entry in enumerate(values):
                    if not isinstance(entry, dict) or set(entry) != {'text', 'snippet_ids'}:
                        raise ValueError('Invalid answer component')
                    validated = {'snippet_ids': entry['snippet_ids'], 'text': v1._text(entry['text'], required=True)}
                    if validated in original_values:
                        raise ValueError('Duplicate answer component')
                    original_values.append(validated)
                    entry['text'] = f'Component {i}.'
    _, _ = v1.extract(json.dumps(masked), plan, snippets)
    # Extract each group independently with short placeholders, then restore
    # exact validated component text before composing the final answer.
    originals = {}
    for row in plan:
        if row['operation'] != 'answer':
            continue
        originals[row['id']] = []
        for group in data[row['id']]['groups']:
            captured = {kind: [{'snippet_ids': list(c['snippet_ids']), 'text': v1._text(c['text'], required=True)}
                               for c in group.pop(kind)] for kind in KINDS}
            conclusion = v1._text(group.get('conclusion'), required=group['status'] == 'supported')
            reason = v1._text(group.get('unknown_reason'), required=group['status'] == 'unknown')
            originals[row['id']].append({'components': captured, 'primary_conclusion': conclusion,
                                        'primary_unknown_reason': reason})
            def compose(primary, kinds, limit):
                pieces = [primary] + [kind.capitalize() + ': ' + ' '.join(c['text'] for c in captured[kind])
                                      for kind in kinds if captured[kind]]
                result = ' '.join(pieces)
                if len(result) > limit:
                    raise ValueError(f'Composed answer exceeds {limit}-character budget; no partial answer saved')
                return result
            group['conclusion'] = compose(conclusion, ('conditions', 'exceptions'), MAX_COMPOSED_TEXT) if conclusion else ''
            group['unknown_reason'] = compose(reason, ('distinctions',), MAX_UNKNOWN_TEXT) if reason else ''
    return json.dumps(data), originals

"""Opt-in source-bound components allowing exact plain-text clauses."""
from copy import deepcopy
import json
import re

from . import investigation_answer_components_v4 as v4
from .investigation_planned_answers import _unique_object
from .investigation_unknown_explanations import _reason

VERSION = 'cited-components-v5'
KINDS = v4.KINDS
INSTRUCTION = v4.INSTRUCTION.replace(
    'Each text is a complete plain sentence without inline source IDs, at most 160 characters.',
    'Each primary is a complete plain sentence. All text fields omit inline source IDs and are at most 160 characters.'
) + (
    ' Conditions, exceptions and distinctions may be concise plain-text clauses; '
    'a component need not end with punctuation. Primary statements remain complete sentences.'
)
response_schema = v4.response_schema
restore_buckets = v4.restore_buckets


def extract(raw, plan, snippets):
    if not isinstance(raw, str) or len(raw) > 60000:
        raise ValueError('Invalid component response')
    data = json.loads(raw, object_pairs_hook=_unique_object)
    masked = deepcopy(data)
    originals = {}
    for row in plan:
        if row['operation'] != 'answer':
            continue
        rid = row['id']; originals[rid] = []
        item = data.get(rid) if isinstance(data, dict) else None
        if not isinstance(item, dict):
            raise ValueError('Invalid status buckets')
        for status in ('supported', 'unknown', 'conflicting'):
            bucket = item.get(status)
            if not isinstance(bucket, list):
                raise ValueError('Invalid status bucket')
            for i, group in enumerate(bucket):
                if not isinstance(group, dict):
                    raise ValueError('Invalid status group')
                captured = {}
                for kind in KINDS:
                    entries = group.get(kind, [])
                    if not isinstance(entries, list):
                        raise ValueError('Invalid component array')
                    captured[kind] = []
                    for j, entry in enumerate(entries):
                        if not isinstance(entry, dict) or set(entry) != {'text', 'snippet_ids'}:
                            raise ValueError('Invalid component')
                        text = entry['text']
                        if not isinstance(text, str) or len(text) > 160:
                            raise ValueError('Invalid component text')
                        normalized = _reason(text, 'unknown', require_complete=False)
                        if normalized != text or re.search(r'\[\s*[SNFU][0-9]+(?:\s*[,;]\s*[SNFU][0-9]+)*\s*\]', text, re.I):
                            raise ValueError('Component must be exact safe text')
                        captured[kind].append(deepcopy(entry))
                        masked[rid][status][i][kind][j]['text'] = f'Component {j}.'
                    if len({json.dumps(x, sort_keys=True) for x in captured[kind]}) != len(captured[kind]):
                        raise ValueError('Duplicate answer component')
                originals[rid].append(captured)
    transformed, details = v4.extract(json.dumps(masked), plan, snippets)
    transformed = json.loads(transformed)
    for row in plan:
        if row['operation'] != 'answer':
            continue
        rid = row['id']
        for group, detail, captured in zip(transformed[rid]['groups'], details[rid], originals[rid]):
            detail['components'] = captured
            def compose(primary, kinds, limit):
                parts = [primary] + [kind.capitalize() + ': ' + '; '.join(c['text'] for c in captured[kind])
                                     for kind in kinds if captured[kind]]
                result = ' '.join(parts)
                if len(result) > limit:
                    raise ValueError('Composed answer exceeds component budget; no partial answer saved')
                return result
            if group['status'] == 'supported':
                group['conclusion'] = compose(detail['primary_conclusion'], ('conditions', 'exceptions'), 900)
            elif group['status'] == 'unknown':
                reason = detail['primary_unknown_reason']
                if captured['distinctions']:
                    reason = 'Distinctions: ' + '; '.join(c['text'] for c in captured['distinctions']) + ' — ' + reason
                if len(reason) > 300:
                    raise ValueError('Composed answer exceeds component budget; no partial answer saved')
                group['unknown_reason'] = reason
    return json.dumps(transformed), details

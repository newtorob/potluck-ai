"""Opt-in larger supported primary with exact source-bound component replay."""
from copy import deepcopy
import json
import re

from . import investigation_answer_components_v5 as v5
from .investigation_planned_answers import _unique_object
from .investigation_unknown_explanations import _reason

VERSION = 'cited-components-v6'
_OLD_LIMIT = 'Each primary is a complete plain sentence. All text fields omit inline source IDs and are at most 160 characters.'
_NEW_LIMIT = ('First fully restate relevant reviewed-claim facts and material qualifiers before adding supporting-source background. '
              'Each primary is a complete plain sentence. All text fields omit inline source IDs. '
              'Supported primary is at most 300 characters; unknown and conflicting primaries and each component are at most 160 characters.')
if _OLD_LIMIT not in v5.INSTRUCTION:
    raise ValueError('Unexpected previous component instruction')
INSTRUCTION = v5.INSTRUCTION.replace(_OLD_LIMIT, _NEW_LIMIT)
restore_buckets = v5.restore_buckets


def response_schema(schema, plan):
    result = v5.response_schema(schema, plan)
    for row in plan:
        if row['operation'] == 'answer':
            result['properties'][row['id']]['properties']['supported']['items']['properties']['primary']['properties']['text']['maxLength'] = 300
    return result


def extract(raw, plan, snippets):
    if not isinstance(raw, str) or len(raw) > 60000:
        raise ValueError('Invalid component response')
    data = json.loads(raw, object_pairs_hook=_unique_object)
    masked = deepcopy(data); originals = {}
    for row in plan:
        if row['operation'] != 'answer':
            continue
        rid = row['id']; item = data.get(rid) if isinstance(data, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get('supported'), list):
            raise ValueError('Invalid status buckets')
        originals[rid] = []
        for i, group in enumerate(item['supported']):
            if not isinstance(group, dict) or not isinstance(group.get('primary'), dict):
                raise ValueError('Invalid primary statement')
            text = group['primary'].get('text')
            if not isinstance(text, str) or len(text) > 300:
                raise ValueError('Invalid primary statement')
            normalized = _reason(text, 'unknown', require_complete=True)
            if normalized != text or re.search(r'\[\s*[SNFU][0-9]+(?:\s*[,;]\s*[SNFU][0-9]+)*\s*\]', text, re.I):
                raise ValueError('Invalid primary statement')
            originals[rid].append(text)
            masked[rid]['supported'][i]['primary']['text'] = 'Supported answer.'
    transformed, details = v5.extract(json.dumps(masked), plan, snippets)
    transformed = json.loads(transformed)
    for row in plan:
        if row['operation'] != 'answer':
            continue
        rid = row['id']
        for i, text in enumerate(originals[rid]):
            entry = details[rid][i]; entry['primary_conclusion'] = text
            captured = entry['components']
            parts = [text] + [kind.capitalize() + ': ' + '; '.join(c['text'] for c in captured[kind])
                              for kind in ('conditions', 'exceptions') if captured[kind]]
            composed = ' '.join(parts)
            if len(composed) > 900:
                raise ValueError('Composed answer exceeds component budget; no partial answer saved')
            transformed[rid]['groups'][i]['conclusion'] = composed
    return json.dumps(transformed), details

"""Group validated exact-source parts without dropping leads or provenance.

The caller validates source bindings first. Grouping does not establish semantic
support or complete question coverage.
"""
from copy import deepcopy
import json


def render(parts, *, max_visible=8000):
    original_parts = deepcopy(parts)
    groups = []
    seen = set()
    for part in deepcopy(parts):
        if part['id'] in seen:
            raise ValueError('Duplicate part')
        seen.add(part['id'])
        verdict = part['verdict']
        if verdict not in ('supported', 'unknown', 'conflicting'):
            raise ValueError('Invalid verdict')
        lead = (f"{part['id']}: {part['request']}" if verdict == 'supported' else
                f"The selected reviewed statements do not establish {part['id']}: {part['request']}"
                if verdict == 'unknown' else
                'The selected reviewed statements have unresolved incompatible alternatives for '
                f"{part['id']}: {part['request']} No authoritative winner is established.")
        # Consecutive identical evidence may share one block; keep part order,
        # every question/status lead and every provenance/role distinction.
        identity = json.dumps({k: v for k, v in part.items() if k not in ('id', 'request')},
                              sort_keys=True, ensure_ascii=False)
        if groups and groups[-1]['identity'] == identity:
            groups[-1]['part_ids'].append(part['id'])
            groups[-1]['leads'].append(lead)
        else:
            groups.append({'identity': identity, 'part_ids': [part['id']],
                           'leads': [lead], 'source_text': part['source_text']})
    blocks = ['\n\n'.join(g['leads']) + ('\n\n' + g['source_text'] if g['source_text'] else '')
              for g in groups]
    text = '\n\n'.join(blocks)
    if len(text) > max_visible:
        raise ValueError('Visible budget exceeded')
    return {'text': text, 'parts': original_parts,
            'groups': [{k: v for k, v in g.items() if k != 'identity'} for g in groups]}

"""Versioned whole-passage input selection using the serving engine's tokenizer."""
from copy import deepcopy
import hashlib
import json
import re

VERSION = 'bounded-evidence-v1'
OUTPUT_TOKENS = 2000
NOTICE = 'Input capacity omitted some retrieved passages. The answer may be incomplete; inspect the full evidence.'
INSTRUCTION = (' Unknown means the requested fact was not established by the presented evidence. '
               'Select directly relevant passages even when they only establish a limitation or a related distinction. '
               'Do not substitute a related but different concept for the requested fact.')


def subset(data, ids):
    result = deepcopy(data)
    for field, key in [('snippets', 'snippet_id'), ('quantities', 'snippet_id'), ('extraction_notices', 'snippet_id')]:
        if field in result:
            result[field] = [row for row in result[field] if row[key] in ids]
    budget = result.get('passage_text_budget', {})
    if 'units' in budget:
        budget['units'] = [row for row in budget['units'] if row['snippet_id'] in ids]
    if 'formula_ratio' in result:
        result['formula_ratio']['formulas'] = [f for f in result['formula_ratio']['formulas'] if f['snippet_id'] in ids]
    omitted = [row['snippet_id'] for row in data['snippets'] if row['snippet_id'] not in ids]
    if omitted:
        result.setdefault('evidence_notices', []).append(NOTICE)
        result['input_omissions'] = {'snippet_ids': omitted, 'selection': 'retrieval order; whole passages'}
    return result


async def fit(data, build, measure):
    """At most eight tokenization probes, no generation retries or text clipping.

    Retrieval already ranks source chunks. Retain its prefix and leave all saved
    evidence intact. A measured oversized mandatory envelope fails explicitly.
    """
    ids = [row['snippet_id'] for row in data['snippets']]
    retained = list(ids)
    # Bound schema operands after source selection, not the saved full catalog.
    while retained and sum(q['snippet_id'] in retained for q in data.get('quantities', [])) > 100:
        retained.pop()
    probes = []
    for _ in range(8):
        selected = subset(data, set(retained))
        messages, schema, encoding = build(selected)
        measured = await measure(messages, schema)
        count, capacity = measured['input_tokens'], measured['context_tokens']
        if type(count) is not int or count < 1 or type(capacity) is not int or capacity <= OUTPUT_TOKENS:
            raise ValueError('Invalid measured investigation capacity')
        probes.append({'input_tokens': count, 'context_tokens': capacity, 'snippet_count': len(retained)})
        if count + OUTPUT_TOKENS <= capacity:
            return messages, schema, encoding, {
                'version': VERSION, 'input_tokens': count, 'output_tokens': OUTPUT_TOKENS,
                'context_tokens': capacity, 'probes': probes, 'selected_snippet_ids': retained,
                'omitted_snippet_ids': [sid for sid in ids if sid not in retained],
                'selected_quantity_ids': [q['id'] for q in selected.get('quantities', [])],
                'selected_formula_ids': [f['id'] for f in selected.get('formula_ratio', {}).get('formulas', [])],
                'messages_sha256': hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                'selection': 'retrieval order; whole passages',
            }
        if not retained:
            break
        # Leave headroom for omission notices and fixed template/schema overhead.
        keep = min(len(retained) - 1, int(len(retained) * (capacity - OUTPUT_TOKENS) / count * .85))
        retained = retained[:max(0, keep)]
    raise ValueError('Investigation request cannot fit the loaded context')


def validate_selection(raw, trace):
    """Schema constraints are not a trust boundary; reject unseen model operands."""
    available = set(trace['selected_snippet_ids']) | set(trace['selected_quantity_ids']) | set(trace.get('selected_formula_ids', []))
    def walk(value):
        if isinstance(value, dict):
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
        elif isinstance(value, str) and re.fullmatch(r'[SNF][1-9][0-9]*', value) and value not in available:
            raise ValueError('Model selected evidence omitted from its input')
    walk(json.loads(raw))

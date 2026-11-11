"""Bounded, explicitly unverified explanations for source-bounded unknowns."""
from copy import deepcopy
import json
import re
import unicodedata

from .investigation_planned_answers import _unique_object
from .memory_safety import redact_credentials

LEGACY_VERSION = 'cited-unknown-v1'
RELATED_VERSION = 'cited-unknown-v2'
DISTINCTION_VERSION = 'cited-unknown-v3'
VERSION = 'cited-unknown-v4'
SUPPORTED_VERSIONS = (LEGACY_VERSION, RELATED_VERSION, DISTINCTION_VERSION, VERSION)
MAX_REASON = 300
LABEL = 'Why this remains unanswered (model assessment, unverified)'
SCOPE = 'This assessment is limited to the presented evidence; it does not prove the answer is unavailable elsewhere.'
LEGACY_INSTRUCTION = (
    ' Each factual focus group also requires unknown_reason. Use an empty string for supported or conflicting groups. '
    'For unknown groups, briefly explain the specific requested information that the presented evidence does not establish. '
    'When passages describe a related but different fact, select those passage IDs and explain the distinction. '
    'Cite only relevant passages through snippet_ids, not inside unknown_reason. Do not turn a related identifier, '
    'qualification, policy or plan into proof of the requested fact. If no relevant passage exists, use no IDs and '
    'describe only the missing information. Do not invent evidence, future outcomes, reasons for absence or universal '
    'claims of nonexistence. Reasons are single-line plain text, at most 300 characters, and remain unverified model assessments.'
)


RELATED_INSTRUCTION = (
    ' Each factual focus group requires unknown_reason. This field is the only free-text explanation exception '
    'to the extractive-passage rule above. Use an empty string for supported or conflicting groups. '
    'For unknown groups, inspect the closest related passages before writing the reason. If evidence supplies '
    'a nearby fact that could be mistaken for the requested answer, explicitly name what that fact establishes '
    'and what it does not establish, and select its snippet_ids even though the requested answer is unknown. '
    'An unknown status does not mean empty citations. Do not discard relevant context simply because it lacks '
    'the exact requested detail. Do not infer a requested fact from a different identifier, qualification, '
    'policy or plan. Use empty snippet_ids only when no presented passage is meaningfully related; then '
    'describe only the missing requested information. Do not invent evidence, future outcomes, reasons for '
    'absence or universal nonexistence. Reasons are single-line plain text, at most 300 characters, without '
    'inline citation markers, and remain unverified model assessments.'
)


DISTINCTION_INSTRUCTION = RELATED_INSTRUCTION + (
    ' For an unknown request for a value, date, version or identifier, explicitly check for similar-looking '
    'values in the related evidence. If present, the reason must name the nearby value, what it actually '
    'identifies or measures, and why it is not the requested fact. Cite that passage. Merely saying that '
    'the requested fact is absent while quoting a nearby value leaves the distinction implicit and is '
    'insufficient. Do not manufacture a comparison when there is no similar-looking value in the evidence. '
    'A requested fact being absent is not an output-capacity failure: keep more false when all requested '
    'focuses are represented, including unknown focuses. Set more true only if the group, passage or text '
    'limits actually prevented representing an additional requested focus.'
)


INSTRUCTION = DISTINCTION_INSTRUCTION + (
    ' Keep unknown_reason to one or two complete sentences, aiming for at most 200 characters so it fits '
    'comfortably below the 300-character hard limit. Prefer concise concept names over passage-ID prose. '
    'Include the missing fact and the essential distinction; omit secondary detail. End with sentence '
    'punctuation, never an unfinished word or clause. Keep exact related passages in snippet_ids.'
)


def instruction_for(version):
    if version not in SUPPORTED_VERSIONS:
        raise ValueError('Invalid unknown explanation mode')
    return {LEGACY_VERSION: LEGACY_INSTRUCTION, RELATED_VERSION: RELATED_INSTRUCTION, DISTINCTION_VERSION: DISTINCTION_INSTRUCTION, VERSION: INSTRUCTION}[version]


def response_schema(schema, plan, *, version=VERSION):
    schema = deepcopy(schema)
    for row in plan:
        if row['operation'] != 'answer':
            continue
        group = schema['properties'][row['id']]['properties']['groups']['items']
        group['properties']['unknown_reason'] = {'type': 'string', 'maxLength': MAX_REASON}
        group['required'].append('unknown_reason')
        if version != LEGACY_VERSION:
            # Assess the evidence before committing to the related passage selection.
            order = ('focus', 'status', 'unknown_reason', 'snippet_ids')
            group['properties'] = {key: group['properties'][key] for key in order}
            group['required'] = list(order)
    return schema


def _reason(text, status, *, require_complete=False):
    if (not isinstance(text, str) or len(text) > MAX_REASON
            or any((unicodedata.category(c).startswith('C') or c in '\u2028\u2029') for c in text)
            or re.search(r'\[\s*[SNFU][0-9]+(?:\s*[,;]\s*[SNFU][0-9]+)*\s*\]', text)):
        raise ValueError('Invalid unknown explanation')
    if status != 'unknown':
        if text != '':
            raise ValueError('Explanation requires an unknown focus')
        return ''
    text = redact_credentials(text.strip())
    if not text or len(text) > MAX_REASON:
        raise ValueError('Unknown focus requires a bounded explanation')
    if require_complete and text[-1] not in '.!?。！？':
        raise ValueError('Unknown explanation must end with sentence punctuation')
    return text


def extract(raw, plan, *, version=VERSION):
    """Strip the opt-in extension before invoking the unchanged base decoder."""
    if not isinstance(raw, str) or len(raw) > 60000:
        raise ValueError('Invalid model format')
    data = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(data, dict):
        raise ValueError('Invalid model format')
    reasons = {}
    for row in plan:
        if row['operation'] != 'answer':
            continue
        item = data.get(row['id'])
        if not isinstance(item, dict) or not isinstance(item.get('groups'), list):
            raise ValueError('Invalid explanation groups')
        reasons[row['id']] = []
        for group in item['groups']:
            if not isinstance(group, dict) or 'unknown_reason' not in group:
                raise ValueError('Missing unknown explanation field')
            reasons[row['id']].append(_reason(group.pop('unknown_reason'), group.get('status'), require_complete=version == VERSION))
    return json.dumps(data), reasons


def records(reasons, coverage, snippets, *, version=VERSION):
    available = {s['snippet_id']: s for s in snippets}
    operations = {}
    for rid, values in reasons.items():
        groups = coverage['focus_groups'][rid]['groups']
        if len(groups) != len(values):
            raise ValueError('Invalid explanation coverage')
        operations[rid] = [
            {'group_index': i, 'focus': group['focus'], 'text': values[i],
             'snippet_ids': list(group['snippet_ids']),
             'source_excerpts': [deepcopy(available[sid]) for sid in group['snippet_ids']]}
            for i, group in enumerate(groups) if group['status'] == 'unknown'
        ]
    return {'version': version, 'operations': operations}


def render(entries):
    if not entries:
        return ''
    lines = [LABEL + ':']
    for entry in entries:
        ids = ', '.join('[' + sid + ']' for sid in entry['snippet_ids']) or 'none selected'
        lines += [entry['focus'] + ': ' + entry['text'], 'Related source passages: ' + ids + '.']
    return '\n'.join(lines + [SCOPE, ''])


def restore(payload, stored, *, version=VERSION):
    """Rebuild original model fields from the immutable saved assessment trace."""
    if not isinstance(stored, dict) or stored.get('version') != version or not isinstance(stored.get('operations'), dict):
        raise ValueError('Invalid original unknown explanations')
    if set(stored['operations']) != set(payload):
        raise ValueError('Invalid original explanation requests')
    for rid, item in payload.items():
        for group in item['groups']:
            group['unknown_reason'] = ''
        entries = stored['operations'][rid]
        if not isinstance(entries, list):
            raise ValueError('Invalid original explanation entries')
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError('Invalid original explanation entry')
            i = entry.get('group_index')
            if type(i) is not int or not 0 <= i < len(item['groups']) or i in seen:
                raise ValueError('Invalid original explanation group')
            seen.add(i)
            group = item['groups'][i]
            if group['status'] != 'unknown':
                raise ValueError('Original explanation requires an unknown focus')
            group['unknown_reason'] = entry.get('text')
    return payload

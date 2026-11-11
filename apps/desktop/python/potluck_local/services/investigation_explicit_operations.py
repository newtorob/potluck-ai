"""Internal explicit-role contract for cited calculations."""
from __future__ import annotations

import json
from typing import Any

from potluck_local.services import investigation_reviewed_operations as v9

ROLES = {
    'difference': ('minuend_id', 'subtrahend_id'),
    'percentage_point_difference': ('minuend_id', 'subtrahend_id'),
    'percent_change': ('baseline_id', 'current_id'),
    'percentage': ('part_id', 'total_id'),
}
GROUPS = ('known_input_snippet_ids', 'competing_snippet_ids', 'limitation_snippet_ids')
ANSWER_INSTRUCTION = (
    v9.ANSWER_INSTRUCTION.replace('select operands in before/after or part/total order',
                                  'select operands by their named roles')
    + ' For each calculation use named operand roles: minuend minus '
    'subtrahend, current relative to baseline, or part divided by total. In the JSON use the string unavailable for an unknown role and none when no '
    'limitation basis applies. Separate known input, competing evidence and limitation snippet IDs. Set '
    'limitation_basis to documented only when limitation excerpts document it; use '
    'not_established when the presented evidence does not establish an input, without claiming '
    'the whole document lacks it. Assess source authority '
    'and competing evidence yourself; cite the relevant excerpts and explain your assessment.'
)


def response_schema(plan: list[dict[str, str]], snippets: list[dict[str, str]],
                    quantities: list[dict[str, Any]]) -> dict[str, Any]:
    schema = v9.response_schema(plan, snippets, quantities)
    nids = [n['id'] for n in quantities]
    sids = [s['snippet_id'] for s in snippets if s['quote'].strip()]
    for request in plan:
        operation = request['operation']
        if operation == 'answer':
            continue
        props = schema['properties'][request['id']]['properties']
        props.pop('operand_ids')
        props.pop('snippet_ids')
        for role in ROLES[operation]:
            props[role] = {'type': 'string', 'enum': nids + ['unavailable']}
        for group in GROUPS:
            props[group] = {'type': 'array', 'items': ({'type': 'string', 'enum': sids} if sids else {'type': 'string'}), 'maxItems': min(8, len(sids))}
        props['limitation_basis'] = {'type': 'string', 'enum': ['documented', 'not_established', 'none']}
        schema['properties'][request['id']]['required'] = list(props)
    return schema


def decode_results(raw: Any, plan: list[dict[str, str]], snippets: list[dict[str, str]],
                   quantities: list[dict[str, Any]], notices: list[dict[str, Any]], *,
                   version: str = 'v3') -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        if not isinstance(raw, str) or len(raw) > 60000:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {r['id'] for r in plan}:
            raise ValueError
        by_quantity = {q['id']: q for q in quantities}
        valid_snippets = {s['snippet_id'] for s in snippets if s['quote'].strip()}
        translated, annotations = {}, {}
        for request in plan:
            rid, operation = request['id'], request['operation']
            row = data[rid]
            if operation == 'answer':
                translated[rid] = row
                continue
            roles = ROLES[operation]
            if not isinstance(row, dict) or set(row) != set(roles) | set(GROUPS) | {'status', 'reason', 'limitation_basis'}:
                raise ValueError
            status = row['status']
            if not isinstance(status, str) or status not in ('ready', 'missing', 'unsupported', 'conflicting'):
                raise ValueError
            role_values = [None if row[k] == 'unavailable' else row[k] for k in roles]
            ids = [x for x in role_values if x is not None]
            if (any(not isinstance(x, str) or x not in by_quantity for x in ids)
                    or len(set(ids)) != len(ids)
                    or (row['status'] == 'ready' and len(ids) != 2)):
                raise ValueError
            groups = {}
            for group in GROUPS:
                entries = row[group]
                if (not isinstance(entries, list) or len(entries) > 8
                        or any(not isinstance(x, str) or x not in valid_snippets for x in entries)
                        or len(set(entries)) != len(entries)):
                    raise ValueError
                groups[group] = list(entries)
            for qid in ids:
                sid = by_quantity[qid]['snippet_id']
                if sid not in groups['known_input_snippet_ids']:
                    raise ValueError
            basis = None if row['limitation_basis'] == 'none' else row['limitation_basis']
            if row['limitation_basis'] is None or any(row[k] is None for k in roles):
                raise ValueError
            if basis not in ('documented', 'not_established', None):
                raise ValueError
            if basis == 'documented' and not groups['limitation_snippet_ids']:
                raise ValueError
            if status == 'ready' and (basis is not None or groups['competing_snippet_ids']
                                      or groups['limitation_snippet_ids']):
                raise ValueError
            if status != 'ready' and basis is None:
                raise ValueError
            if status == 'conflicting' and not groups['competing_snippet_ids']:
                raise ValueError
            sids = list(dict.fromkeys(sid for group in GROUPS for sid in groups[group]))
            if len(sids) > 8:
                raise ValueError
            # Existing calculator expects subtrahend before minuend.
            ordered = ([role_values[1], role_values[0]] if operation in
                       ('difference', 'percentage_point_difference') else role_values)
            translated[rid] = {'status': row['status'], 'operand_ids': [x for x in ordered if x is not None],
                               'snippet_ids': sids, 'reason': row['reason']}
            annotations[rid] = {'operand_roles': dict(zip(roles, role_values)), **groups,
                                'limitation_basis': basis,
                                'evidence_assessment': 'Model assessment; source authority and completeness are not verified.'}
        brief, traces = v9.decode_results(json.dumps(translated), plan, snippets, quantities, notices, version=version)
        for trace in traces:
            trace.update(annotations[trace['request_id']])
        return brief, traces
    except (ValueError, TypeError, KeyError):
        raise ValueError('Invalid model format') from None

"""Offline source-bound unit plans; caller-declared edges are not approval."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAX_CLAIMS = 16
MAX_CLAIM_CHARS = 16000
MAX_CATALOG = 72
MAX_CLOSED = 16
MAX_VISIBLE = 8000
VERSION = 'explicit-source-plan-v1'
_IDENTIFIER = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}')


def digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode()).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _valid_claim(claim: Any, seen: set[str]) -> bool:
    return (
        isinstance(claim, dict)
        and set(claim) == {'id', 'text', 'citations'}
        and isinstance(claim['id'], str)
        and bool(_IDENTIFIER.fullmatch(claim['id']))
        and claim['id'] not in seen
        and isinstance(claim['text'], str)
        and bool(claim['text'].strip())
        and len(claim['text']) <= MAX_CLAIM_CHARS
        and isinstance(claim['citations'], list)
        and bool(claim['citations'])
    )


def _valid_unit(unit: Any, seen: set[str]) -> bool:
    return (
        isinstance(unit, dict)
        and set(unit) == {'id', 'ranges', 'required_unit_ids'}
        and isinstance(unit['id'], str)
        and bool(_IDENTIFIER.fullmatch(unit['id']))
        and unit['id'] not in seen
        and isinstance(unit['ranges'], list)
        and bool(unit['ranges'])
        and isinstance(unit['required_unit_ids'], list)
        and all(isinstance(item, str) for item in unit['required_unit_ids'])
        and len(unit['required_unit_ids']) == len(set(unit['required_unit_ids']))
    )


def prepare(claims: Any, plans: Any) -> dict[str, Any]:
    if (
        not isinstance(claims, list)
        or not 1 <= len(claims) <= MAX_CLAIMS
        or not isinstance(plans, dict)
        or any(not isinstance(key, str) for key in plans)
    ):
        raise ValueError('invalid inputs')
    claim_ids: set[str] = set()
    output: list[dict[str, Any]] = []
    for claim in claims:
        if not _valid_claim(claim, claim_ids):
            raise ValueError('invalid claim')
        claim_id = claim['id']
        claim_ids.add(claim_id)
        text = claim['text']
        plan = plans.get(claim_id)
        if plan is None:
            if claim_id in plans:
                raise ValueError('invalid plan')
            output.append({
                'id': f'{claim_id}:whole', 'claim_id': claim_id,
                'local_id': 'whole', 'ranges': [[0, len(text)]], 'text': text,
                'required_unit_ids': [], 'fallback': 'missing plan',
            })
            continue
        if (
            not isinstance(plan, dict)
            or set(plan) != {'source_sha256', 'units'}
            or plan['source_sha256'] != text_hash(text)
            or not isinstance(plan['units'], list)
            or not plan['units']
        ):
            raise ValueError('invalid or stale plan')
        local_ids: set[str] = set()
        entries: list[dict[str, Any]] = []
        covered = [False] * len(text)
        for unit in plan['units']:
            if not _valid_unit(unit, local_ids):
                raise ValueError('invalid unit')
            local_ids.add(unit['id'])
            last = 0
            parts = []
            for source_range in unit['ranges']:
                if (
                    not isinstance(source_range, list)
                    or len(source_range) != 2
                    or any(type(offset) is not int for offset in source_range)
                    or not 0 <= source_range[0] < source_range[1] <= len(text)
                    or source_range[0] < last
                ):
                    raise ValueError('invalid ranges')
                last = source_range[1]
                parts.append(text[source_range[0]:source_range[1]])
                for offset in range(*source_range):
                    covered[offset] = True
            unit_text = ''.join(parts)
            if not unit_text.strip():
                raise ValueError('empty source unit')
            entries.append({
                'id': f"{claim_id}:{unit['id']}", 'claim_id': claim_id,
                'local_id': unit['id'], 'ranges': unit['ranges'], 'text': unit_text,
                'required_unit_ids': [f'{claim_id}:{dep}' for dep in unit['required_unit_ids']],
                'fallback': None,
            })
        if any(not covered[index] and not ch.isspace() for index, ch in enumerate(text)):
            raise ValueError('uncovered source')
        if any(dep not in local_ids for unit in plan['units'] for dep in unit['required_unit_ids']):
            raise ValueError('unknown dependency')
        output.extend(entries)
    if set(plans) - claim_ids:
        raise ValueError('unknown claim plan')
    if len(output) > MAX_CATALOG:
        raise ValueError('catalog limit')
    return {
        'version': VERSION, 'claims': claims, 'plans': plans, 'units': output,
        'input_sha256': digest({'claims': claims, 'plans': plans}),
    }


def render(catalog: Any, selected: Any) -> dict[str, Any]:
    if not isinstance(catalog, dict) or catalog != prepare(catalog.get('claims'), catalog.get('plans')):
        raise ValueError('catalog tampered')
    if (
        not isinstance(selected, list)
        or any(not isinstance(item, str) for item in selected)
        or len(set(selected)) != len(selected)
    ):
        raise ValueError('invalid selection')
    index = {unit['id']: unit for unit in catalog['units']}
    if len(index) != len(catalog['units']) or any(item not in index for item in selected):
        raise ValueError('unknown unit')
    closed = set(selected)
    pending = list(selected)
    while pending:
        for dependency in index[pending.pop()]['required_unit_ids']:
            if dependency not in closed:
                closed.add(dependency)
                pending.append(dependency)
    if len(closed) > MAX_CLOSED:
        raise ValueError('closed unit limit')
    visible = [unit for unit in catalog['units'] if unit['id'] in closed]
    text = '\n'.join(unit['text'] for unit in visible)
    if len(text) > MAX_VISIBLE:
        raise ValueError('visible text limit')
    return {
        'text': text, 'closed_unit_ids': [unit['id'] for unit in visible],
        'catalog_sha256': digest(catalog), 'full_claims': catalog['claims'],
    }

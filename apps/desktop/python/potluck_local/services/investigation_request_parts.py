"""Explicit question parts with application-owned IDs, not verified coverage."""
from __future__ import annotations

from typing import Any

from .reviewed_source_unit_plan import digest

VERSION = 'explicit-question-parts-v1'
MAX_PARTS = 6
MAX_PART_CHARS = 500
MAX_QUESTION_CHARS = 4000
ORIGINS = ('fixture_supplied', 'reviewer_supplied', 'model_proposed')


def prepare(question: str, parts: list[str], *, origin: str,
            request: str | None = None) -> dict[str, Any]:
    if request is None:
        request = question
    if (any(not isinstance(value, str) or not value.strip()
            or len(value) > MAX_QUESTION_CHARS for value in (question, request))
            or not isinstance(origin, str) or origin not in ORIGINS
            or not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS):
        raise ValueError('Invalid question part plan')
    seen = set()
    rows = []
    for index, part in enumerate(parts, 1):
        if not isinstance(part, str) or not part.strip() or len(part) > MAX_PART_CHARS:
            raise ValueError('Invalid question part request')
        normalized = ' '.join(part.split()).casefold()
        if normalized in seen:
            raise ValueError('Duplicate question part request')
        seen.add(normalized)
        rows.append({'id': f'P{index}', 'request': part})
    body = {'version': VERSION, 'question': question, 'request': request,
            'origin': origin, 'parts': rows, 'coverage': 'not_verified'}
    return {**body, 'plan_sha256': digest(body)}


def validate(plan: Any, *, question: str | None = None,
             request: str | None = None) -> dict[str, Any]:
    if (not isinstance(plan, dict)
            or set(plan) != {'version', 'question', 'request', 'origin', 'parts',
                             'coverage', 'plan_sha256'}
            or not isinstance(plan['parts'], list)
            or any(not isinstance(part, dict) or set(part) != {'id', 'request'}
                   for part in plan['parts'])):
        raise ValueError('Invalid question part envelope')
    expected = prepare(plan['question'], [part['request'] for part in plan['parts']],
                       origin=plan['origin'], request=plan['request'])
    if (plan != expected or question is not None and question != plan['question']
            or request is not None and request != plan['request']):
        raise ValueError('Question part plan binding mismatch')
    return expected

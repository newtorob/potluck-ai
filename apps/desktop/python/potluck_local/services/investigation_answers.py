"""Bounded question and source-snippet contract for opt-in investigations."""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from potluck_local.services.memory_safety import redact_credentials


class Answer(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    status: str
    text: str = Field(min_length=1, max_length=1000)
    snippet_ids: list[str] = Field(max_length=8)


def questions_from_texts(texts: Any) -> list[dict[str, str]]:
    if not isinstance(texts, list) or not 1 <= len(texts) <= 8:
        raise ValueError('Invalid questions')
    clean = []
    for text in texts:
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ValueError('Invalid questions')
        clean.append(redact_credentials(text.strip()))
    if len(set(clean)) != len(clean):
        raise ValueError('Duplicate questions')
    return [{'id': f'Q{i}', 'text': text} for i, text in enumerate(clean, 1)]


def planner_schema() -> dict[str, Any]:
    return {'type': 'object', 'properties': {'questions': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 500}, 'minItems': 1, 'maxItems': 8}},
            'required': ['questions'], 'additionalProperties': False}


def decode_plan(raw: Any) -> list[dict[str, str]]:
    try:
        if not isinstance(raw, str) or len(raw) > 10000:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {'questions'}:
            raise ValueError
        return questions_from_texts(data['questions'])
    except (ValueError, TypeError):
        raise ValueError('Invalid model format') from None


def decode_span_plan(raw: Any, issue: str) -> list[dict[str, str]]:
    """Validate ordered issue substrings; this does not establish complete coverage."""
    try:
        if not isinstance(raw, str) or len(raw) > 10000:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {'questions'}:
            raise ValueError
        spans = data['questions']
        if not isinstance(spans, list) or not 1 <= len(spans) <= 8:
            raise ValueError
        seen: set[str] = set()
        cursor = 0
        for span in spans:
            if (not isinstance(span, str) or not span.strip() or len(span) > 500
                    or span in seen):
                raise ValueError
            start = issue.find(span, cursor)
            if start < 0:
                raise ValueError
            seen.add(span)
            cursor = start + len(span)
        return [{'id': f'Q{i}', 'text': span} for i, span in enumerate(spans, 1)]
    except (ValueError, TypeError):
        raise ValueError('Invalid model format') from None


def snippets_from_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, str]]:
    snippets: list[dict[str, str]] = []
    sources: dict[str, str] = {}
    for record in evidence:
        children = record['sources'] if record['kind'] == 'reviewed_claim' else [record]
        for child in children:
            content = child.get('presented_content', '')
            cid = child['citation_id']
            if len(content) > len(sources.get(cid, '')):
                sources[cid] = content
    for cid, content in sources.items():
        offset = 0
        while offset < len(content):
            end = min(offset + 500, len(content))
            window = content[offset:end]
            boundary = re.search(r'(?:\n|[.!?](?=\s|$))[ \t\r\n]*', window)
            if boundary:
                end = offset + boundary.end()
            elif end < len(content):
                spaces = list(re.finditer(r'\s+', window))
                if spaces:
                    end = offset + spaces[-1].end()
            quote = content[offset:end]
            snippets.append({'snippet_id': f'S{len(snippets) + 1}',
                             'citation_id': cid, 'quote': quote})
            offset = end
    return snippets


def answer_schema(questions: list[dict[str, str]], snippets: list[dict[str, str]]) -> dict[str, Any]:
    ids = [s['snippet_id'] for s in snippets if s['quote'].strip()]
    snippet_item = {'type': 'string', 'enum': ids} if ids else {'type': 'string'}
    answer = {'type': 'object', 'properties': {
        'snippet_ids': {'type': 'array', 'items': snippet_item, 'maxItems': min(8, len(ids))},
        'text': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
        'status': {'type': 'string', 'enum': ['supported', 'unknown', 'conflicting']}},
        'required': ['snippet_ids', 'text', 'status'], 'additionalProperties': False}
    ids = [q['id'] for q in questions]
    return {'type': 'object', 'properties': {qid: answer for qid in ids},
            'required': ids, 'additionalProperties': False}


def build_brief(raw: Any, questions: list[dict[str, str]],
                snippets: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {q['id'] for q in questions}:
        raise ValueError('Invalid model format')
    available = {snippet['snippet_id']: snippet for snippet in snippets}
    answers = []
    for question in questions:
        answer = Answer.model_validate(raw[question['id']])
        if not answer.text.strip() or answer.status not in ('supported', 'unknown', 'conflicting'):
            raise ValueError('Invalid model format')
        if (len(set(answer.snippet_ids)) != len(answer.snippet_ids)
                or any(sid not in available or not available[sid]['quote'].strip() for sid in answer.snippet_ids)):
            raise ValueError('Invalid model citation')
        if (answer.status in ('supported', 'conflicting') and not answer.snippet_ids):
            raise ValueError('Invalid model citation')
        answers.append({'question_id': question['id'], 'status': answer.status,
                        'text': redact_credentials(answer.text.strip()),
                        'snippet_ids': answer.snippet_ids,
                        'source_excerpts': [available[sid].copy() for sid in answer.snippet_ids]})
    return {'questions': questions, 'answers': answers, 'facts': [], 'prior_fixes': [],
            'hypotheses': [], 'recommended_checks': [], 'model_uncertainties': []}


def decode_answers(raw: Any, questions: list[dict[str, str]],
                   snippets: list[dict[str, str]]) -> dict[str, Any]:
    try:
        if not isinstance(raw, str) or len(raw) > 40000:
            raise ValueError
        return build_brief(json.loads(raw), questions, snippets)
    except (ValueError, TypeError):
        raise ValueError('Invalid model format') from None


def corrected_brief(raw: Any, original: dict[str, Any],
                    snippets: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != set(original) or raw['questions'] != original['questions']:
        raise ValueError('Invalid questions')
    if any(raw[k] != [] for k in ('facts', 'prior_fixes', 'hypotheses',
                                    'recommended_checks', 'model_uncertainties')):
        raise ValueError('Invalid sections')
    answers = raw['answers']
    if not isinstance(answers, list) or len(answers) != len(original['questions']):
        raise ValueError('Invalid answers')
    mapped = {}
    for answer in answers:
        if (not isinstance(answer, dict) or set(answer) not in (
                {'question_id', 'status', 'text', 'snippet_ids'},
                {'question_id', 'status', 'text', 'snippet_ids', 'source_excerpts'})):
            raise ValueError('Invalid answer')
        qid = answer['question_id']
        if not isinstance(qid, str) or qid in mapped:
            raise ValueError('Invalid answer')
        mapped[qid] = {k: answer[k] for k in ('status', 'text', 'snippet_ids')}
    result = build_brief(mapped, original['questions'], snippets)
    actual = {a['question_id']: a for a in answers}
    if any('source_excerpts' in actual[a['question_id']] and
           actual[a['question_id']]['source_excerpts'] != a['source_excerpts'] for a in result['answers']):
        raise ValueError('Invalid source excerpts')
    return result

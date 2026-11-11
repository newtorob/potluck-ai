"""Lossless display projection of validated fixed-part answers, never authority."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import hashlib
import re
from typing import Any

VERSION = 'readable-source-groups-v1'


def cells(line: str) -> list[str] | None:
    """Keep escaped and inline-code pipes inside their original cell."""
    value = line.strip()
    if not value.startswith('|') or not value.endswith('|'):
        return None
    output, current, ticks = [], '', ''
    i = 1
    while i < len(value) - 1:
        char = value[i]
        if char == '\\' and i + 1 < len(value) - 1:
            current += value[i:i + 2]
            i += 2
            continue
        if char == '`':
            end = i + 1
            while end < len(value) - 1 and value[end] == '`':
                end += 1
            marker = value[i:end]
            ticks = marker if not ticks else '' if ticks == marker else ticks
            current += marker
            i = end
            continue
        if char == '|' and not ticks:
            output.append(current.strip())
            current = ''
        else:
            current += char
        i += 1
    output.append(current.strip())
    return output if len(output) > 1 and not ticks else None


def _headers(row: str, sources: list[dict[str, Any]]) -> list[str] | None:
    matches = set()
    for source in sources:
        header = None
        lines = source.get('content', '').splitlines()
        for index, line in enumerate(lines):
            parsed = cells(line)
            if parsed and all(re.fullmatch(r':?-{3,}:?', cell) for cell in parsed):
                previous = cells(lines[index - 1]) if index else None
                header = previous if previous and len(previous) == len(parsed) else None
                continue
            if not parsed:
                header = None
                continue
            if line.strip() == row.strip() and header and len(header) == len(parsed):
                matches.add(tuple(header))
    if len(matches) == 1:
        header = list(matches.pop())
        if all(header) and len(set(header)) == len(header):
            return header
    return None


def _records(text: str, sources: list[dict[str, Any]], escape: Callable[[str], str]) -> str:
    output = []
    fence = None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            output.append(escape(line))
            continue
        row = cells(line) if fence is None else None
        # A complete local table already has its own structural context.
        adjacent_table = any(cells(lines[i]) for i in (index - 1, index + 1) if 0 <= i < len(lines))
        headers = _headers(line, sources) if row and not adjacent_table else None
        if headers:
            output.append('')
            for label, value in zip(headers, row):
                safe_label = re.sub(r'([\\`*_{}\[\]()<>])', r'\\\1', label)
                output.append(f'- **{safe_label}:** {escape(value) if value else "(Empty cell)"}')
            output.append('')
        else:
            output.append(escape(line))
    return '\n'.join(output)


def _prefix(texts: list[str]) -> str:
    chunks = [text.split('\n\n') for text in texts]
    common = []
    for row in zip(*chunks):
        if len(set(row)) != 1 or any(re.match(r'^\s*(`{3,}|~{3,}|\|)', line) for line in row[0].splitlines()):
            break
        common.append(row[0])
    if len(common) < 2:
        return ''
    prefix = '\n\n'.join(common) + '\n\n'
    return prefix if all(text.startswith(prefix) and text[len(prefix):].strip() for text in texts) else ''


def project(answer: dict[str, Any], evidence: list[dict[str, Any]], *,
            escape: Callable[[str], str] = lambda value: value) -> dict[str, Any] | None:
    presentation = answer.get('passage_presentation') or {}
    if presentation.get('version') != 'fixed-part-selection-v32':
        return None
    text = answer['text']
    passages = presentation.get('passages', [])
    markers = list(re.finditer(r'(?m)^\[(\d+)\]\n', text))
    if not markers or len(markers) != len(passages):
        return None
    claims = {item.get('citation_id'): item for item in evidence if item.get('kind') == 'reviewed_claim'}
    groups = []
    used = set()
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        body = text[marker.end():end].rstrip('\n')
        matches = [p for p in passages if p.get('text', '').rstrip('\n') == body]
        if len(matches) != 1 or matches[0]['unit_id'] in used:
            return None
        passage = matches[0]
        claim = claims.get(passage['citation_id'])
        if not claim or not claim.get('sources'):
            return None
        used.add(passage['unit_id'])
        groups.append({'id': marker.group(1), 'text': body, 'passage': passage, 'claim': claim})
    revisions = [{(s['collection_id'], s['document_id'], s['revision_id']) for s in g['claim']['sources']}
                 for g in groups]
    shared = (_prefix([g['text'] for g in groups]) if len(groups) > 1
              and all(revision == revisions[0] for revision in revisions) else '')
    lines = [escape(text[:markers[0].start()].rstrip())]
    if shared:
        lines += ['### Shared source context',
                  'Applies to evidence ' + ', '.join('[' + g['id'] + ']' for g in groups) + '.',
                  escape(shared.rstrip())]
    mappings = []
    for group in groups:
        suffix = group['text'][len(shared):]
        assert shared + suffix == group['text']
        lines += ['### Evidence ' + group['id'], _records(suffix, group['claim']['sources'], escape)]
        mappings.append({'evidence_id': group['id'], 'unit_id': group['passage']['unit_id'],
                         'citation_id': group['passage']['citation_id'],
                         'shared_prefix_chars': len(shared),
                         'original_sha256': hashlib.sha256(group['text'].encode()).hexdigest()})
    markdown = '\n\n'.join(lines)
    return {'version': VERSION, 'original_text': text, 'original_status': answer['status'],
            'original_snippet_ids': deepcopy(answer['snippet_ids']), 'markdown': markdown,
            'shared_context': bool(shared), 'source_groups': mappings}


def decorate(item: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for answer in (item.get('brief') or {}).get('answers', []):
        value = project(answer, item.get('evidence', []))
        if value:
            result[answer['question_id']] = value
    return {**item, 'readable_answers': result} if result else item


def export(item: dict[str, Any], escape: Callable[[str], str]) -> str | None:
    answers = (item.get('brief') or {}).get('answers', [])
    if not answers or (item.get('review') or {}).get('decision') == 'correct':
        return None
    preserve_lines = lambda value: '\n'.join(escape(line) for line in value.splitlines())
    projections = [project(answer, item.get('evidence', []), escape=preserve_lines) for answer in answers]
    if any(value is None for value in projections):
        return None
    lines = ['# Investigation', '', escape(item['issue']), '',
             'Readable view of the original answer. Source selections and original output are retained.', '',
             'Investigation: ' + escape(item.get('id', '')), 'Model: ' + escape(item.get('model_slug', '')),
             'Model SHA256: ' + escape(item.get('model_sha', '')), 'Created: ' + escape(item.get('created_at', '')), '']
    for answer, value in zip(answers, projections):
        lines += ['## Answer · ' + escape(answer['status']), value['markdown'], '', '### Citations']
        for group in value['source_groups']:
            lines.append('- Evidence ' + group['evidence_id'] + ': ' + escape(group['citation_id']))
        lines += ['', '<details><summary>Original answer and exact excerpts</summary>', '']
        raw = answer['text'] + '\n\n' + '\n\n'.join(
            s['snippet_id'] + ' · ' + s['citation_id'] + '\n' + s['quote'] for s in answer['source_excerpts'])
        fence = '`' * max(3, 1 + max((len(m.group()) for m in re.finditer(r'`+', raw)), default=0))
        lines += [fence + 'text', raw, fence, '', '</details>', '']
    review = item.get('review')
    if review:
        lines += ['## Review', escape(review['decision']), 'Reviewer: ' + escape(review['reviewer']),
                  escape(review['reason']), '']
    lines += ['## Actual outcome', escape(str(item.get('outcome') or 'No outcome recorded.')), '']
    return '\n\n'.join(lines)

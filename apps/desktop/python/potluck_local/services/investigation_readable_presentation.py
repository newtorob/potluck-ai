"""Lossless display projection of validated fixed-part answers, never authority."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import hashlib
import re
from typing import Any

VERSION = 'readable-source-groups-v1'
TASK_VERSION = 'readable-task-statements-v2'
TASK_SCOPED_VERSION = 'readable-task-statements-v3'


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


def _task_intro(answer: dict[str, Any], groups: list[dict[str, Any]], prefix: str,
                question: str) -> tuple[str, str] | None:
    """Compact only a fully reconstructed single-part lead; keep scoped decisions."""
    presentation = answer['passage_presentation']
    parts = presentation.get('parts', [])
    if len(parts) != 1 or not isinstance(parts[0], dict):
        return None
    part = parts[0]
    pid, status = part.get('id'), answer['status']
    if (not isinstance(pid, str) or not pid or part.get('request') != question
            or status not in ('supported', 'unknown') or part.get('verdict') != status):
        return None
    markers = ', '.join('[' + group['id'] + ']' for group in groups)
    lead = pid + ': ' + question
    lead += (' — Not established by the selected reviewed statements.\nScope basis: '
             if status == 'unknown' else '\nEvidence: ') + markers
    blocks = []
    acknowledgements = presentation.get('acknowledgements', [])
    unresolved = presentation.get('unresolved_requests', [])
    if not isinstance(acknowledgements, list) or not isinstance(unresolved, list):
        return None
    if acknowledgements:
        numbers = {g['passage']['unit_id']: g['id'] for g in groups}
        lines = ['Direct answers (selected reviewed statements; unverified):']
        for row in acknowledgements:
            if (not isinstance(row, dict) or row.get('part_id') != pid
                    or row.get('answer') not in ('Yes', 'No')
                    or not isinstance(row.get('clause'), str) or not row['clause'].strip()
                    or row['clause'] not in question
                    or not isinstance(row.get('source_unit_ids'), list)
                    or not row['source_unit_ids']
                    or any(not isinstance(uid, str) or uid not in numbers
                           for uid in row['source_unit_ids'])):
                return None
            refs = list(dict.fromkeys(numbers[uid] for uid in row['source_unit_ids']))
            lines.append('- ' + row['answer'] + ': ' + row['clause'] + ' '
                         + ' '.join('[' + ref + ']' for ref in refs))
            if presentation.get('version') in ('fixed-part-selection-v45', 'fixed-part-selection-v46', 'fixed-part-selection-v47') and 'scope_unit_ids' in row:
                scope_ids = row['scope_unit_ids']
                if (not isinstance(scope_ids, list) or not scope_ids
                        or any(not isinstance(uid, str) or uid not in numbers for uid in scope_ids)
                        or len(set(scope_ids)) != len(scope_ids)
                        or set(scope_ids) != set(numbers)):
                    return None
                scope_refs = list(dict.fromkeys(numbers[uid] for uid in scope_ids))
                lines.append('  Selected-source scope for this answer (input, not verified interpretation): '
                             + ' '.join('[' + ref + ']' for ref in scope_refs))
        blocks.append('\n'.join(lines))
    if unresolved:
        lines = ['Not established by the selected reviewed statements (model assessment; review required):']
        for row in unresolved:
            if not isinstance(row, dict):
                return None
            start, end = row.get('start'), row.get('end')
            if (row.get('part_id') != pid or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(question)
                    or row.get('text') != question[start:end]):
                return None
            lines.append('- ' + pid + ': ' + row['text'])
        blocks.append('\n'.join(lines))
    if status == 'unknown' and not unresolved:
        return None
    if prefix != '\n\n'.join([*blocks, lead]):
        return None
    if not blocks:
        return TASK_VERSION, ''
    # A missing detail remains scoped to the exact request fragment and its
    # selected evidence, rather than hiding the task's known instructions.
    if status == 'unknown':
        blocks.append('Scope basis: ' + markers)
    return TASK_SCOPED_VERSION, '\n\n'.join(blocks)


def _task_layout(answer: dict[str, Any], groups: list[dict[str, Any]], prefix: str,
                 question: str | None, escape: Callable[[str], str]) -> dict[str, Any] | None:
    """Preserve body order; relocate only proven administrative task metadata."""
    from .investigation_readable_task_statement import project_task_statement

    presentation = answer['passage_presentation']
    if presentation.get('version') not in ('fixed-part-selection-v43', 'fixed-part-selection-v44', 'fixed-part-selection-v45', 'fixed-part-selection-v46', 'fixed-part-selection-v47') or not question:
        return None
    intro = _task_intro(answer, groups, prefix, question)
    if intro is None:
        return None
    version, scoped = intro
    prepared = []
    for group in groups:
        if (not group['passage']['unit_id'].endswith(':whole')
                or group['text'] != group['claim'].get('content')
                or group['claim'].get('status') != 'approved'):
            return None
        value = project_task_statement(group['text'], group['claim']['sources'])
        if value is None:
            return None
        prepared.append((group, value))
    lines, metadata_lines, mappings = ([escape(scoped)] if scoped else []), [], []
    for group, value in prepared:
        lines += ['### Evidence ' + group['id'], escape(value['source_header'].rstrip()),
                  'Source task metadata (as recorded): `' + value['status_line'] + '`',
                  escape(value['heading'])]
        # Earlier prerequisites and later exceptions remain in their original
        # source body order. No action extraction, summary or semantic rewrite.
        lines += [_records(chunk['text'], group['claim']['sources'], escape)
                  for chunk in value['body_chunks']]
        # The strict helper excludes fence characters from metadata values.
        # Keep the complete administrative block visible and verbatim.
        metadata_lines += ['#### Evidence ' + group['id'] + ' source metadata (verbatim)',
                           '```yaml\n' + value['metadata']['text'] + '\n```']
        mappings.append({'evidence_id': group['id'], 'unit_id': group['passage']['unit_id'],
                         'citation_id': group['passage']['citation_id'], 'shared_prefix_chars': 0,
                         'original_sha256': value['original_sha256'],
                         'task_statement_projection': {
                             'version': value['version'], 'offset_unit': value['offset_unit'],
                             'segments': [{key: row[key] for key in ('kind', 'start', 'end', 'citation_id')
                                           if key in row} for row in value['segments']]}})
    return {'version': version, 'original_text': answer['text'],
            'original_status': answer['status'],
            'original_snippet_ids': deepcopy(answer['snippet_ids']),
            'markdown': '\n\n'.join(lines + metadata_lines),
            'shared_context': False, 'source_groups': mappings}


def project(answer: dict[str, Any], evidence: list[dict[str, Any]], *,
            escape: Callable[[str], str] = lambda value: value,
            question: str | None = None, allow_task_layout: bool = True) -> dict[str, Any] | None:
    presentation = answer.get('passage_presentation') or {}
    if presentation.get('version') not in ('fixed-part-selection-v32', 'fixed-part-selection-v33', 'fixed-part-selection-v34', 'fixed-part-selection-v35', 'fixed-part-selection-v36', 'fixed-part-selection-v37', 'fixed-part-selection-v38', 'fixed-part-selection-v39', 'fixed-part-selection-v40', 'fixed-part-selection-v41', 'fixed-part-selection-v42', 'fixed-part-selection-v43', 'fixed-part-selection-v44', 'fixed-part-selection-v45', 'fixed-part-selection-v46', 'fixed-part-selection-v47'):
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
    if allow_task_layout:
        task = _task_layout(answer, groups, text[:markers[0].start()].rstrip(), question, escape)
        if task is not None:
            return task
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
    from . import investigation_answer_outlines as outlines
    result = {}
    questions = {row['id']: row['text'] for row in (item.get('brief') or {}).get('questions', [])}
    for answer in (item.get('brief') or {}).get('answers', []):
        value = project(answer, item.get('evidence', []), question=questions.get(answer['question_id']),
                        allow_task_layout=(item.get('review') or {}).get('decision') != 'correct')
        if value:
            result[answer['question_id']] = value
    organized = outlines.project_item(item)
    return {**item, **({'readable_answers': result} if result else {}),
            **({'answer_outlines': organized} if organized else {})}


def export(item: dict[str, Any], escape: Callable[[str], str]) -> str | None:
    from . import investigation_answer_outlines as outlines
    answers = (item.get('brief') or {}).get('answers', [])
    if not answers or (item.get('review') or {}).get('decision') == 'correct':
        return None
    preserve_lines = lambda value: '\n'.join(escape(line) for line in value.splitlines())
    organized = outlines.project_item(item)
    projections = []
    questions = {row['id']: row['text'] for row in (item.get('brief') or {}).get('questions', [])}
    for answer in answers:
        value = project(answer, item.get('evidence', []), escape=preserve_lines,
                        question=questions.get(answer['question_id']))
        if value is None and answer['question_id'] in organized:
            citations = list(dict.fromkeys(row['citation_id'] for row in answer['source_excerpts']))
            value = {'markdown': '', 'source_groups': [{'evidence_id': str(i), 'citation_id': citation}
                     for i, citation in enumerate(citations, 1)]}
        projections.append(value)
    if any(value is None for value in projections):
        return None
    lines = ['# Investigation', '', escape(item['issue']), '',
             'Readable view of the original answer. Source selections and original output are retained.', '',
             'Investigation: ' + escape(item.get('id', '')), 'Model: ' + escape(item.get('model_slug', '')),
             'Model SHA256: ' + escape(item.get('model_sha', '')), 'Created: ' + escape(item.get('created_at', '')), '']
    coverage = (item.get('provenance') or {}).get('document_coverage')
    if coverage:
        lines += ['## Selected document input',
                  str(coverage['supplied_documents']) + ' of ' + str(coverage['selected_documents']) + ' selected indexed documents supplied in full from reviewed statements.',
                  'Folder exclusions apply. Input coverage does not verify model interpretation or company-wide absence.']
        lines += ['- ' + escape(doc['relative_path']) + ': ' + escape(doc['claim_citation_id']) for doc in coverage['documents']]
    for answer, value in zip(answers, projections):
        outline = organized.get(answer['question_id'])
        if outline:
            by_id = {row['snippet_id']: row for row in answer['source_excerpts']}
            parts = []
            for section in outline['sections']:
                parts.append('### ' + escape(section['title']))
                for block in section['blocks']:
                    rows = [by_id[sid] for sid in block['snippet_ids']]
                    parts.append(_records('\n\n'.join(row['quote'].strip() for row in rows),
                                          [s for e in item['evidence'] for s in e.get('sources', [])], preserve_lines))
                    parts.append('Citations: ' + ', '.join(escape(c) for c in dict.fromkeys(row['citation_id'] for row in rows)))
            value = {**value, 'markdown': '\n\n'.join(parts)}
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

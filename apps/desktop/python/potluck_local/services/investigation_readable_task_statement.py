"""Conservative, reversible projection of one complete task statement."""
import hashlib
import json
import re
from datetime import date


_FIELDS = {'id', 'title', 'type', 'status', 'priority', 'area', 'owner',
           'source', 'verified', 'created', 'updated', 'tags'}
_STATUSES = {'open', 'todo', 'in progress', 'blocked', 'done', 'closed'}
_ID = re.compile(r'[A-Z][A-Z0-9]*-[0-9]+\Z')
_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.,/@+-]*\Z")


def _metadata(text):
    lines = text.splitlines(keepends=True)
    if len(lines) < 3 or lines[0] != '---\n' or lines[-1].rstrip('\n') != '---':
        return None
    fields = {}
    status_line = None
    for line in lines[1:-1]:
        match = re.fullmatch(r'([a-z]+): ([^\n\r]+)\n', line)
        if not match or match[1] not in _FIELDS or match[1] in fields:
            return None
        key, raw = match.groups()
        if raw.startswith('"'):
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                return None
            if not isinstance(value, str):
                return None
        else:
            value = raw
        # The exact title stays visible as the proven source heading. Other
        # metadata cannot carry a condition that would move behind the body.
        if not isinstance(value, str) or (key != 'title' and any(
                x in value.lower().split() for x in ('if', 'unless', 'before', 'after', 'until', 'when', 'required', 'except'))):
            return None
        if key == 'tags':
            if value not in ('[]', '[task]'):
                return None
        elif key in ('created', 'updated'):
            try:
                if date.fromisoformat(value).isoformat() != value:
                    return None
            except ValueError:
                return None
        elif key == 'verified':
            if value not in ('true', 'false'):
                return None
        elif key == 'priority':
            if not re.fullmatch(r'P[0-5]', value):
                return None
        elif key in ('owner', 'area', 'source'):
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@+-]{0,63}', value):
                return None
        elif not _VALUE.fullmatch(value):
            return None
        fields[key] = value
        if key == 'status':
            status_line = line.rstrip('\n')
    if not {'id', 'title', 'type', 'status'} <= fields.keys():
        return None
    if not _ID.fullmatch(fields['id']) or fields['type'] != 'task' or fields['status'] not in _STATUSES:
        return None
    return fields, status_line


def project_task_statement(text: str, sources: list[dict]) -> dict | None:
    if not isinstance(text, str) or not isinstance(sources, list) or not sources:
        return None
    header = re.match(r'\ADocument: ([^\n]+) / ([^\n]+)\n\n', text)
    if not header:
        return None
    identities, chunk_ids, citations, rows = set(), set(), set(), []
    for source in sources:
        if not isinstance(source, dict):
            return None
        identity = tuple(source.get(k) for k in ('collection_id', 'document_id', 'revision_id', 'relative_path'))
        citation, chunk = source.get('citation_id'), source.get('chunk_id')
        content, heading = source.get('content'), source.get('heading')
        if (source.get('kind') != 'source_chunk' or any(not isinstance(x, str) or not x for x in identity) or identity[3] != header[2]
                or not isinstance(citation, str) or not citation or not isinstance(chunk, str) or not chunk
                or citation in citations or chunk in chunk_ids or not isinstance(content, str) or not content
                or not isinstance(heading, str) or text.count(content) != 1):
            return None
        identities.add(identity)
        citations.add(citation)
        chunk_ids.add(chunk)
        rows.append((text.index(content), source))
    if len(identities) != 1:
        return None
    rows.sort(key=lambda row: row[0])
    ordered = [row[1] for row in rows]
    first = ordered[0]
    if first['heading'] or first.get('anchor') != 'intro':
        return None
    parsed = _metadata(first['content'])
    if parsed is None:
        return None
    fields, status_line = parsed
    heading = fields['id'] + ' ' + fields['title']
    if any(s['heading'] != heading for s in ordered[1:]):
        return None
    pieces = [(s['heading'] + '\n\n' if s['heading'] else '') + s['content'] for s in ordered]
    if header.group() + '\n\n'.join(pieces) != text:
        return None
    # An exact construction plus one occurrence per content excludes overlap.
    cursor = len(header.group())
    segments = [{'kind': 'source_header', 'start': 0, 'end': cursor, 'text': text[:cursor]}]
    body = []
    for index, source in enumerate(ordered):
        if index:
            segments.append({'kind': 'separator', 'start': cursor, 'end': cursor + 2, 'text': '\n\n'})
            cursor += 2
        if source['heading']:
            injected = source['heading'] + '\n\n'
            segments.append({'kind': 'injected_heading', 'start': cursor, 'end': cursor + len(injected), 'text': injected, 'citation_id': source['citation_id']})
            cursor += len(injected)
        content = source['content']
        segments.append({'kind': 'metadata' if index == 0 else 'body', 'start': cursor, 'end': cursor + len(content), 'text': content, 'citation_id': source['citation_id']})
        cursor += len(content)
        if index:
            body.append({'text': content, 'citation_id': source['citation_id']})
    if cursor != len(text) or not body:
        return None
    return {'version': 'task-body-order-preserved-v1', 'offset_unit': 'unicode_codepoints', 'original_sha256': hashlib.sha256(text.encode()).hexdigest(), 'source_header': header.group(),
            'heading': heading, 'status_line': status_line, 'body_chunks': body,
            'metadata': {'text': first['content'], 'citation_id': first['citation_id']}, 'segments': segments}

"""Bounded, source-bound paragraph excerpts for opt-in investigations."""
from __future__ import annotations

import re
from typing import Any

MAX_SNIPPETS = 100
MAX_QUOTE_LENGTH = 500


def snippets_from_evidence(evidence: list[dict[str, Any]], *, version: str = 'paragraph-v1') -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Return contiguous substrings and explicit truncation information.

    A single newline inside prose stays in its paragraph. Blank lines and list
    markers mark boundaries; long blocks split at whitespace when possible.
    """
    if version == 'paragraph-v3':
        return _snippets_v3(evidence)
    if version == 'paragraph-v2':
        return _snippets_v2(evidence)
    if version != 'paragraph-v1':
        raise ValueError(f'Unknown paragraph version: {version}')
    sources: dict[str, str] = {}
    for record in evidence:
        children = record['sources'] if record['kind'] == 'reviewed_claim' else [record]
        for child in children:
            cid = child['citation_id']
            content = child.get('presented_content', '')
            if len(content) > len(sources.get(cid, '')):
                sources[cid] = content
    snippets: list[dict[str, str]] = []
    omitted = 0
    split_blocks = 0
    for cid, content in sources.items():
        # Boundaries are positions in the original string, never reconstructed text.
        boundaries = [0]
        in_fence = False
        for match in re.finditer(r'(?m)^.*(?:\n|$)', content):
            line = match.group()
            if not line:
                continue
            stripped = line.lstrip()
            if stripped.startswith(('```', '~~~')):
                in_fence = not in_fence
            elif not in_fence and match.start() > boundaries[-1]:
                if not line.strip() or re.match(r'\s*(?:[-*+]\s+|\d+[.)]\s+)', line):
                    boundaries.append(match.start())
        boundaries.append(len(content))
        for start, stop in zip(boundaries, boundaries[1:]):
            while start < stop:
                end = min(start + MAX_QUOTE_LENGTH, stop)
                if end < stop:
                    split_blocks += 1
                    window = content[start:end]
                    spaces = list(re.finditer(r'\s+', window))
                    if spaces and spaces[-1].end() > 0:
                        end = start + spaces[-1].end()
                quote = content[start:end]
                if quote.strip():
                    if len(snippets) < MAX_SNIPPETS:
                        snippets.append({'snippet_id': f'S{len(snippets) + 1}',
                                         'citation_id': cid, 'quote': quote})
                    else:
                        omitted += 1
                start = end
    return snippets, {'version': 'paragraph-v1', 'max_snippets': MAX_SNIPPETS,
                      'max_quote_length': MAX_QUOTE_LENGTH,
                      'omitted_snippets': omitted, 'split_blocks': split_blocks,
                      'truncated': omitted > 0}


def _snippets_v2(evidence: list[dict[str, Any]]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    sources: dict[str, str] = {}
    for record in evidence:
        children = record['sources'] if record['kind'] == 'reviewed_claim' else [record]
        for child in children:
            cid = child['citation_id']
            content = child.get('presented_content', '')
            if len(content) > len(sources.get(cid, '')):
                sources[cid] = content

    snippets: list[dict[str, str]] = []
    omitted = omitted_characters = split_blocks = whitespace_only_characters = 0
    for cid, content in sources.items():
        if not content.strip():
            whitespace_only_characters += len(content)
            continue
        boundaries = [0]
        in_fence = False
        for match in re.finditer(r'(?m)^.*(?:\n|$)', content):
            line = match.group()
            if not line:
                continue
            if line.lstrip().startswith(('```', '~~~')):
                in_fence = not in_fence
            elif not in_fence and match.start() > boundaries[-1]:
                if not line.strip() or re.match(r'\s*(?:[-*+]\s+|\d+[.)]\s+)', line):
                    boundaries.append(match.start())
        boundaries.append(len(content))
        for start, stop in zip(boundaries, boundaries[1:]):
            while start < stop:
                end = min(start + MAX_QUOTE_LENGTH, stop)
                if end < stop:
                    split_blocks += 1
                    spaces = list(re.finditer(r'\s+', content[start:end]))
                    if spaces:
                        end = start + spaces[-1].end()
                if len(snippets) < MAX_SNIPPETS:
                    snippets.append({'snippet_id': f'S{len(snippets) + 1}',
                                     'citation_id': cid, 'quote': content[start:end]})
                else:
                    omitted += 1
                    omitted_characters += end - start
                start = end
    return snippets, {'version': 'paragraph-v2', 'max_snippets': MAX_SNIPPETS,
                      'max_quote_length': MAX_QUOTE_LENGTH, 'omitted_snippets': omitted,
                      'omitted_characters': omitted_characters,
                      'whitespace_only_characters': whitespace_only_characters,
                      'split_blocks': split_blocks, 'truncated': omitted > 0}


def _snippets_v3(evidence: list[dict[str, Any]]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Partition presented source prefixes without reconstructing or dropping text."""
    sources: dict[str, str] = {}
    for record in evidence:
        children = record['sources'] if record['kind'] == 'reviewed_claim' else [record]
        for child in children:
            cid = child['citation_id']
            content = child.get('presented_content', '')
            if len(content) > len(sources.get(cid, '')):
                sources[cid] = content

    snippets: list[dict[str, str]] = []
    omitted = omitted_characters = split_blocks = 0
    sentence_splits = whitespace_splits = hard_splits = 0
    limit = 1000
    for cid, content in sources.items():
        # Source offsets define blocks. Code fences delimit a block so sentence
        # heuristics cannot change where prose and code are partitioned.
        blocks: list[tuple[int, int, bool]] = []
        start = 0
        fence = False
        opening = ''
        for match in re.finditer(r'(?m)^.*(?:\n|$)', content):
            line = match.group()
            if not line:
                continue
            marker = re.match(r'(`{3,}|~{3,})', line.lstrip())
            if marker and not fence:
                if match.start() > start:
                    blocks.append((start, match.start(), False))
                start = match.start()
                fence = True
                opening = marker.group()
            elif (marker and fence and marker.group()[0] == opening[0]
                  and len(marker.group()) >= len(opening)
                  and not line.lstrip()[marker.end():].strip()):
                blocks.append((start, match.end(), True))
                start = match.end()
                fence = False
            elif not fence and match.start() > start and (not line.strip() or re.match(r'\s*(?:[-*+]\s+|\d+[.)]\s+)', line)):
                blocks.append((start, match.start(), False))
                start = match.start()
        if start < len(content):
            blocks.append((start, len(content), fence))
        for start, stop, code in blocks:
            while start < stop:
                end = min(start + limit, stop)
                if end < stop:
                    split_blocks += 1
                    window = content[start:end]
                    sentences = [] if code else list(re.finditer(r'[.!?。！？]["\'”’\)\]]*\s+', window))
                    if sentences and sentences[-1].end() >= limit // 2:
                        end = start + sentences[-1].end()
                        sentence_splits += 1
                    else:
                        spaces = list(re.finditer(r'\s+', window))
                        if spaces:
                            end = start + spaces[-1].end()
                            whitespace_splits += 1
                        else:
                            hard_splits += 1
                if len(snippets) < MAX_SNIPPETS:
                    snippets.append({'snippet_id': f'S{len(snippets) + 1}',
                                     'citation_id': cid, 'quote': content[start:end]})
                else:
                    omitted += 1
                    omitted_characters += end - start
                start = end
    return snippets, {'version': 'paragraph-v3', 'max_snippets': MAX_SNIPPETS,
                      'max_quote_length': limit, 'omitted_snippets': omitted,
                      'omitted_characters': omitted_characters, 'split_blocks': split_blocks,
                      'sentence_splits': sentence_splits, 'whitespace_splits': whitespace_splits,
                      'hard_splits': hard_splits, 'truncated': omitted > 0}

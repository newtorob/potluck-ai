"""Bounded, source-bound paragraph excerpts for opt-in investigations."""
from __future__ import annotations

import re
from typing import Any

MAX_SNIPPETS = 100
MAX_QUOTE_LENGTH = 500


def snippets_from_evidence(evidence: list[dict[str, Any]]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Return contiguous substrings and explicit truncation information.

    A single newline inside prose stays in its paragraph. Blank lines and list
    markers mark boundaries; long blocks split at whitespace when possible.
    """
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

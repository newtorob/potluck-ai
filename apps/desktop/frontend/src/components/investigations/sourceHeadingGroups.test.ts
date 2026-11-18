import { describe, expect, it } from 'vitest';
import { sourceHeadingGroups } from './sourceHeadingGroups';

function fixture() {
  const source = [
    {
      heading: 'Requirements',
      content: 'Ship only after both reviewers approve.\nKeep the rollback ready.',
    },
    {
      heading: 'Exception until 17:00 🚦',
      content: 'Do not skip the second review unless the incident lead approves.',
    },
  ];
  const content =
    'Document: Policy\n\n' + source.map((s) => `${s.heading}\n\n${s.content}`).join('\n\n');
  const chunks = source.map((s, i) => ({
    ...s,
    kind: 'source_chunk',
    freshness: 'recent',
    document_id: 'doc1',
    revision_id: 'rev1',
    relative_path: 'policy.md',
    chunk_id: `chunk${i}`,
    citation_id: `source:chunk${i}:rev1`,
  }));
  const evidence = [
    {
      kind: 'reviewed_claim',
      status: 'approved',
      citation_id: 'claim:one',
      content,
      sources: chunks,
    },
  ];
  const passages = source.map((s) => ({ quote: s.content, citation_id: 'claim:one' }));
  return { source, chunks, content, evidence, passages };
}

describe('sourceHeadingGroups', () => {
  it('retains exact conditional Unicode headings, paragraph order and citations without mutating input', () => {
    const f = fixture();
    const before = JSON.stringify(f);
    expect(sourceHeadingGroups(f.passages, f.evidence)).toEqual(
      f.source.map((s) => ({ heading: s.heading, text: s.content, citation: 'claim:one' })),
    );
    expect(JSON.stringify(f)).toBe(before);
  });
  it('keeps adjacent excerpts from the same section together', () => {
    const f = fixture();
    const rows = [
      ...f.source[0]!.content.split('\n').map((quote) => ({ quote, citation_id: 'claim:one' })),
      f.passages[1]!,
    ];
    expect(sourceHeadingGroups(rows, f.evidence)?.map((g) => g.text)).toEqual([
      f.source[0]!.content.replace('\n', '\n\n'),
      f.source[1]!.content,
    ]);
  });
  it('rejects duplicate quote occurrences even when their text matches', () => {
    const f = fixture();
    f.evidence[0]!.content += '\n\n' + f.passages[0]!.quote;
    expect(sourceHeadingGroups(f.passages, f.evidence)).toBeNull();
  });
  it('rejects ambiguous source chunks and duplicate headings', () => {
    const f = fixture();
    f.chunks.push({ ...f.chunks[0]!, chunk_id: 'other', citation_id: 'source:other:rev1' });
    expect(sourceHeadingGroups(f.passages, f.evidence)).toBeNull();
    const g = fixture();
    g.chunks[1]!.heading = g.chunks[0]!.heading;
    expect(sourceHeadingGroups(g.passages, g.evidence)).toBeNull();
  });
  it('refuses an excerpt crossing a section boundary', () => {
    const f = fixture();
    const quote =
      f.source[0]!.content + '\n\n' + f.source[1]!.heading + '\n\n' + f.source[1]!.content;
    expect(
      sourceHeadingGroups([{ quote, citation_id: 'claim:one' }, f.passages[1]!], f.evidence),
    ).toBeNull();
  });
  it.each([
    'heading',
    'document_id',
    'revision_id',
    'relative_path',
    'chunk_id',
    'content',
  ] as const)('refuses absent or empty %s metadata', (key) => {
    const f = fixture();
    f.chunks[0]![key] = '';
    expect(sourceHeadingGroups(f.passages, f.evidence)).toBeNull();
  });
  it('refuses a mismatched source citation, mixed revisions or mixed documents', () => {
    for (const key of ['citation_id', 'revision_id', 'document_id'] as const) {
      const f = fixture();
      f.chunks[1]![key] = 'other';
      expect(sourceHeadingGroups(f.passages, f.evidence)).toBeNull();
    }
  });
  it('does not use a heading that is not adjacent to its exact chunk in the approved statement', () => {
    const f = fixture();
    f.evidence[0]!.content =
      f.content.replace('Requirements\n\n', 'Misleading\n\n') + '\n\nRequirements';
    expect(sourceHeadingGroups(f.passages, f.evidence)).toBeNull();
  });
  it('refuses reordering, repeats and overlap', () => {
    const f = fixture();
    for (const rows of [
      [...f.passages].reverse(),
      [...f.passages, f.passages[0]!],
      [
        f.passages[0]!,
        { quote: 'Keep the rollback ready.', citation_id: 'claim:one' },
        f.passages[1]!,
      ],
    ])
      expect(sourceHeadingGroups(rows, f.evidence)).toBeNull();
  });
  it('refuses mixed claim citations and duplicate claim identities', () => {
    const f = fixture();
    expect(
      sourceHeadingGroups(
        [f.passages[0]!, { ...f.passages[1]!, citation_id: 'claim:two' }],
        f.evidence,
      ),
    ).toBeNull();
    expect(sourceHeadingGroups(f.passages, [...f.evidence, ...f.evidence])).toBeNull();
  });
  it('leaves one section unchanged and handles malformed metadata safely', () => {
    const f = fixture();
    expect(
      sourceHeadingGroups(
        f.source[0]!.content.split('\n').map((quote) => ({ quote, citation_id: 'claim:one' })),
        f.evidence,
      ),
    ).toBeNull();
    expect(sourceHeadingGroups(f.passages, [{ ...f.evidence[0], sources: [null] }])).toBeNull();
    expect(sourceHeadingGroups([], f.evidence)).toBeNull();
  });
  it.each(['proposed', 'superseded', 'deleted', undefined])(
    'keeps %s claim status in the existing view',
    (status) => {
      const f = fixture();
      expect(sourceHeadingGroups(f.passages, [{ ...f.evidence[0], status }])).toBeNull();
    },
  );
  it.each(['stale', 'unknown', undefined])(
    'keeps mixed %s source freshness in the existing view',
    (freshness) => {
      const f = fixture();
      expect(
        sourceHeadingGroups(f.passages, [
          { ...f.evidence[0], sources: [f.chunks[0], { ...f.chunks[1], freshness }] },
        ]),
      ).toBeNull();
    },
  );
});

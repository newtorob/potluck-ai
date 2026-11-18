type Passage = { quote: string; citation_id: string };
export type SourceHeadingGroup = { heading: string; text: string; citation: string };

function uniquePosition(text: string, part: string) {
  const start = text.indexOf(part);
  return start >= 0 && start === text.lastIndexOf(part) ? start : -1;
}

/** Headings describe supplied source sections, never inferred answer categories. */
export function sourceHeadingGroups(
  passages: Passage[],
  evidence: Array<Record<string, unknown>>,
): SourceHeadingGroup[] | null {
  try {
    if (passages.length < 2) return null;
    const citation = passages[0]!.citation_id;
    if (passages.some((row) => row.citation_id !== citation)) return null;
    const claims = evidence.filter(
      (row) => row.kind === 'reviewed_claim' && row.citation_id === citation,
    );
    if (claims.length !== 1) return null;
    const claim = claims[0]!;
    if (
      claim.status !== 'approved' ||
      typeof claim.content !== 'string' ||
      !Array.isArray(claim.sources)
    )
      return null;
    const chunks = claim.sources as Array<Record<string, unknown>>;
    if (!chunks.length) return null;
    const identities = new Set<string>();
    const chunkIds = new Set<string>();
    for (const chunk of chunks) {
      const identity = [chunk.document_id, chunk.revision_id, chunk.relative_path];
      if (
        chunk.kind !== 'source_chunk' ||
        chunk.freshness !== 'recent' ||
        identity.some((value) => typeof value !== 'string' || !value.trim()) ||
        typeof chunk.chunk_id !== 'string' ||
        !chunk.chunk_id ||
        chunkIds.has(chunk.chunk_id) ||
        chunk.citation_id !== `source:${chunk.chunk_id}:${String(chunk.revision_id)}` ||
        typeof chunk.content !== 'string' ||
        !chunk.content.trim() ||
        typeof chunk.heading !== 'string'
      )
        return null;
      identities.add(JSON.stringify(identity));
      chunkIds.add(chunk.chunk_id);
    }
    if (identities.size !== 1) return null;
    const groups: SourceHeadingGroup[] = [];
    let previousEnd = -1;
    for (const row of passages) {
      const quote = row.quote.trim();
      if (!quote) return null;
      const position = uniquePosition(claim.content, quote);
      if (position < previousEnd || position < 0) return null;
      const matching = chunks.filter((chunk) => (chunk.content as string).trim().includes(quote));
      if (matching.length !== 1) return null;
      const chunk = matching[0]!;
      const heading = (chunk.heading as string).trim();
      const content = (chunk.content as string).trim();
      const chunkPosition = uniquePosition(claim.content, content);
      const prefix = `\n\n${heading}\n\n`;
      if (
        !heading ||
        /[\r\n]/.test(heading) ||
        chunks.filter((item) => (item.heading as string).trim() === heading).length !== 1 ||
        chunkPosition < prefix.length ||
        claim.content.slice(chunkPosition - prefix.length, chunkPosition) !== prefix ||
        uniquePosition(claim.content, prefix + content) !== chunkPosition - prefix.length ||
        position < chunkPosition ||
        position + quote.length > chunkPosition + content.length
      )
        return null;
      const previous = groups.at(-1);
      if (previous?.heading === heading) previous.text += `\n\n${quote}`;
      else groups.push({ heading, text: quote, citation });
      previousEnd = position + quote.length;
    }
    if (
      groups.length < 2 ||
      groups.map((group) => group.text).join('\n\n') !==
        passages.map((row) => row.quote.trim()).join('\n\n')
    )
      return null;
    return groups;
  } catch {
    return null;
  }
}

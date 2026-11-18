import type { Brief } from '@/types/investigations';

type Answer = NonNullable<Brief['answers']>[number];
type RecordValue = Record<string, unknown>;
const record = (value: unknown): value is RecordValue =>
  !!value && typeof value === 'object' && !Array.isArray(value);
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const only = (value: RecordValue, keys: string[]) =>
  Object.keys(value).every((key) => keys.includes(key));
const empty = (value: unknown) => Array.isArray(value) && value.length === 0;
const one = (value: unknown, expected: unknown) =>
  Array.isArray(value) && value.length === 1 && value[0] === expected;

/** Only compress an exactly reconstructible, single-part unknown; all uncertainty falls back. */
export function resolveConciseUnknown(
  question: string,
  answer: Answer,
  evidence: Array<Record<string, unknown>>,
): { request: string; citation: string } | null {
  try {
    const p: unknown = answer.passage_presentation;
    if (
      !question ||
      answer.status !== 'unknown' ||
      !record(p) ||
      !['fixed-part-selection-v32', 'fixed-part-selection-v33'].includes(String(p.version)) ||
      p.answer !== answer.text ||
      !Array.isArray(p.parts) ||
      p.parts.length !== 1 ||
      !Array.isArray(p.display_parts) ||
      p.display_parts.length !== 1 ||
      !record(p.parts[0]) ||
      !record(p.display_parts[0])
    )
      return null;
    const part = p.parts[0];
    const display = p.display_parts[0];
    const common = [
      'id',
      'request',
      'evidence_unit_ids',
      'context_unit_ids',
      'background_unit_ids',
      'verdict',
      'requested_unit_ids',
      'unit_ids',
      'dependency_only_unit_ids',
      'source_text',
    ];
    if (
      !only(display, [
        ...common,
        'passages',
        'visible_unit_ids',
        'visible_dependency_unit_ids',
        'part_background_only_unit_ids',
      ]) ||
      common.some((key) => !same(part[key], display[key])) ||
      (display.visible_unit_ids !== undefined &&
        !same(display.visible_unit_ids, part.visible_unit_ids)) ||
      (display.visible_dependency_unit_ids !== undefined &&
        !same(display.visible_dependency_unit_ids, part.visible_dependency_unit_ids)) ||
      (display.part_background_only_unit_ids !== undefined &&
        !same(display.part_background_only_unit_ids, part.part_background_only_unit_ids))
    )
      return null;
    if (
      !only(p, [
        'version',
        'parts',
        'answer',
        'scope',
        'coverage',
        'needs_review',
        'passages',
        'request_context',
        'display_parts',
        'presentation_kind',
        'background_role',
      ]) ||
      !only(part, [
        'id',
        'request',
        'evidence_unit_ids',
        'context_unit_ids',
        'background_unit_ids',
        'verdict',
        'requested_unit_ids',
        'unit_ids',
        'dependency_only_unit_ids',
        'source_text',
        'passages',
        'visible_unit_ids',
        'visible_dependency_unit_ids',
        'part_background_only_unit_ids',
      ]) ||
      part.id !== 'P1' ||
      part.request !== question ||
      part.verdict !== 'unknown' ||
      !empty(part.context_unit_ids) ||
      !empty(part.background_unit_ids) ||
      !empty(part.dependency_only_unit_ids) ||
      !empty(part.visible_dependency_unit_ids) ||
      !empty(part.part_background_only_unit_ids) ||
      !record(p.request_context) ||
      !empty(p.request_context.requested_unit_ids) ||
      !empty(p.request_context.unit_ids) ||
      !empty(p.request_context.visible_unit_ids) ||
      !empty(p.request_context.passages) ||
      (p.safeguards !== undefined && !empty(p.safeguards)) ||
      !Array.isArray(p.passages) ||
      p.passages.length !== 1 ||
      !record(p.passages[0]) ||
      !Array.isArray(part.passages) ||
      part.passages.length !== 1 ||
      !record(part.passages[0]) ||
      !Array.isArray(display.passages) ||
      display.passages.length !== 1 ||
      !record(display.passages[0])
    )
      return null;
    const passage = p.passages[0];
    const passageCore = ['unit_id', 'citation_id', 'ranges', 'text'];
    for (const candidate of [part.passages[0], display.passages[0]]) {
      if (
        !record(candidate) ||
        !only(candidate, [...passageCore, 'snippet_ids']) ||
        passageCore.some((key) => !same(candidate[key], passage[key])) ||
        (candidate.snippet_ids !== undefined && !same(candidate.snippet_ids, passage.snippet_ids))
      )
        return null;
    }
    if (
      !only(passage, ['unit_id', 'citation_id', 'ranges', 'text', 'snippet_ids']) ||
      !only(p.request_context, ['requested_unit_ids', 'unit_ids', 'visible_unit_ids', 'passages'])
    )
      return null;
    const unit = passage.unit_id;
    const citation = passage.citation_id;
    if (
      typeof unit !== 'string' ||
      !unit ||
      typeof citation !== 'string' ||
      !citation ||
      !one(part.evidence_unit_ids, unit) ||
      !one(part.requested_unit_ids, unit) ||
      !one(part.unit_ids, unit) ||
      !one(part.visible_unit_ids, unit) ||
      typeof passage.text !== 'string' ||
      !passage.text ||
      part.source_text !== passage.text ||
      !Array.isArray(passage.snippet_ids) ||
      !passage.snippet_ids.length ||
      !same(answer.snippet_ids, passage.snippet_ids) ||
      new Set(answer.snippet_ids).size !== answer.snippet_ids.length ||
      !Array.isArray(answer.source_excerpts) ||
      answer.source_excerpts.length !== answer.snippet_ids.length
    )
      return null;
    const matching = evidence.filter(
      (item) => item.kind === 'reviewed_claim' && item.citation_id === citation,
    );
    if (matching.length !== 1 || typeof matching[0]!.content !== 'string') return null;
    const chars = Array.from(matching[0]!.content);
    if (!Array.isArray(passage.ranges) || !passage.ranges.length) return null;
    let previous = 0;
    const ranges: Array<[number, number]> = [];
    for (const range of passage.ranges) {
      if (
        !Array.isArray(range) ||
        range.length !== 2 ||
        !Number.isInteger(range[0]) ||
        !Number.isInteger(range[1]) ||
        range[0] < previous ||
        range[0] >= range[1] ||
        range[1] > chars.length
      )
        return null;
      const [start, end] = range as [number, number];
      ranges.push([start, end]);
      previous = end;
    }
    if (ranges.map(([start, end]) => chars.slice(start, end).join('')).join('') !== passage.text)
      return null;
    const spans: Array<[number, number]> = [];
    let cursorUtf16 = 0;
    for (let i = 0; i < answer.snippet_ids.length; i++) {
      const excerpt = answer.source_excerpts[i];
      if (
        !excerpt ||
        excerpt.snippet_id !== answer.snippet_ids[i] ||
        excerpt.citation_id !== citation ||
        !excerpt.quote
      )
        return null;
      const source = chars.join('');
      const startUtf16 = source.indexOf(excerpt.quote, cursorUtf16);
      if (startUtf16 < 0 || source.slice(cursorUtf16, startUtf16).trim()) return null;
      cursorUtf16 = startUtf16 + excerpt.quote.length;
      const start = Array.from(source.slice(0, startUtf16)).length;
      spans.push([start, start + Array.from(excerpt.quote).length]);
    }
    if (
      spans.some(([start, end]) => !ranges.some(([a, b]) => start < b && end > a)) ||
      ranges.some(([start, end]) => {
        for (let i = start; i < end; i++)
          if (!spans.some(([a, b]) => a <= i && i < b) && !/\s/u.test(chars[i]!)) return true;
        return false;
      })
    )
      return null;
    const expected = `P1: ${question} — Not established by the selected reviewed statements.\nScope basis: [1]\n\n[1]\n${passage.text}`;
    if (answer.text !== expected) return null;
    return { request: question, citation };
  } catch {
    return null;
  }
}

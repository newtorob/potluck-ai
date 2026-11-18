import { describe, expect, it } from 'vitest';
import type { Brief } from '@/types/investigations';
import { resolveConciseUnknown } from './resolveConciseUnknown';

type Answer = NonNullable<Brief['answers']>[number];
const question = 'How long did café 🚀 take?';
const text = 'Duration for café 🚀 is unrecorded.';
const citation = 'claim:one';
const passage = {
  unit_id: 'C1:whole',
  citation_id: citation,
  ranges: [[0, Array.from(text).length]],
  text,
  snippet_ids: ['S1'],
};
const part = {
  id: 'P1',
  request: question,
  verdict: 'unknown',
  evidence_unit_ids: ['C1:whole'],
  context_unit_ids: [],
  background_unit_ids: [],
  requested_unit_ids: ['C1:whole'],
  unit_ids: ['C1:whole'],
  dependency_only_unit_ids: [],
  visible_unit_ids: ['C1:whole'],
  visible_dependency_unit_ids: [],
  part_background_only_unit_ids: [],
  source_text: text,
  passages: [passage],
};
const full = `P1: ${question} — Not established by the selected reviewed statements.\nScope basis: [1]\n\n[1]\n${text}`;
const item = {
  question_id: 'R1',
  status: 'unknown',
  text: full,
  snippet_ids: ['S1'],
  source_excerpts: [{ snippet_id: 'S1', citation_id: citation, quote: text }],
  passage_presentation: {
    version: 'fixed-part-selection-v33',
    answer: full,
    parts: [part],
    display_parts: [part],
    passages: [passage],
    request_context: { requested_unit_ids: [], unit_ids: [], visible_unit_ids: [], passages: [] },
  },
} as unknown as Answer;
const evidence = [{ kind: 'reviewed_claim', citation_id: citation, content: text, sources: [] }];

describe('resolveConciseUnknown', () => {
  it('accepts only the exactly bound Unicode codepoint record', () => {
    expect(resolveConciseUnknown(question, item, evidence)).toEqual({
      request: question,
      citation,
    });
  });
  it('accepts the server display and passage shapes with repeated ordered snippets', () => {
    const candidate = structuredClone(item) as any;
    const p = candidate.passage_presentation;
    p.parts = structuredClone(p.parts);
    p.display_parts = structuredClone(p.display_parts);
    delete p.display_parts[0].visible_unit_ids;
    delete p.display_parts[0].visible_dependency_unit_ids;
    delete p.display_parts[0].part_background_only_unit_ids;
    delete p.parts[0].passages[0].snippet_ids;
    delete p.display_parts[0].passages[0].snippet_ids;
    expect(resolveConciseUnknown(question, candidate, evidence)).not.toBeNull();
  });
  it('rejects altered verdicts, scope, source, excerpt and range bindings', () => {
    for (const mutate of [
      (a: any) => {
        a.status = 'supported';
      },
      (a: any) => {
        a.passage_presentation.display_parts[0].request = 'Other?';
      },
      (a: any) => {
        a.passage_presentation.parts[0].background_unit_ids = ['C1:whole'];
      },
      (a: any) => {
        a.passage_presentation.parts[0].dependency_only_unit_ids = ['C1:whole'];
      },
      (a: any) => {
        a.passage_presentation.request_context.unit_ids = ['C1:whole'];
      },
      (a: any) => {
        a.passage_presentation.passages[0].ranges[0][1]++;
      },
      (a: any) => {
        a.source_excerpts[0].quote = 'different';
      },
      (a: any) => {
        a.text += ' extra';
      },
      (a: any) => {
        a.passage_presentation.version = 'fixed-part-selection-v31';
      },
    ]) {
      const changed = structuredClone(item);
      mutate(changed);
      expect(resolveConciseUnknown(question, changed, evidence)).toBeNull();
    }
    expect(resolveConciseUnknown('Other question?', item, evidence)).toBeNull();
    expect(resolveConciseUnknown(question, item, [...evidence, ...evidence])).toBeNull();
    expect(
      resolveConciseUnknown(question, item, [{ ...evidence[0]!, content: 'Changed' }]),
    ).toBeNull();
  });
});

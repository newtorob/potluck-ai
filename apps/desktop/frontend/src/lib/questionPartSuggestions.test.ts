import { describe, expect, it } from 'vitest';
import { suggestQuestionParts } from './questionPartSuggestions';
import { questionPartsError } from '@/components/investigations/QuestionPartsEditor';

describe('literal question-part suggestions', () => {
  it.each([
    'What broke, and what recovery was implemented?',
    'Which checks failed? What remains open?',
    'What changed and why did the test miss it?',
  ])('preserves every character and order in %s', (question) => {
    const { parts } = suggestQuestionParts(question);
    expect(parts).toHaveLength(2);
    expect(parts.join('')).toBe(question);
    expect(questionPartsError(parts)).toBe('');
  });
  it.each([
    'What changed if staging fails, and what should production do?',
    'What is allowed unless the owner objects, and what is excluded?',
    'What do "ready?" and "live?" mean?',
    'What are the sender, embed and consent checks?',
    'What happens before sending, and what result is recorded?',
    '1. What changed?\n2. What remains?',
    'Why is it not ready, and what blocks approval?',
    'What? When? Where? Which? Who? Why? How?',
  ])('keeps uncertain or shared wording together in %s', (question) => {
    expect(suggestQuestionParts(question).parts).toEqual([question]);
  });
  it('does not clip over-limit input or turn an empty question into a usable part', () => {
    expect(suggestQuestionParts('  ').parts).toEqual([]);
    const long = 'x'.repeat(501);
    expect(suggestQuestionParts(long).parts).toEqual([long]);
    expect(questionPartsError(suggestQuestionParts(long).parts)).toContain('500');
  });
  it('retains boundary whitespace as well as conditions rather than normalizing a proposal', () => {
    const question = '  What changed, and what remains?  ';
    expect(suggestQuestionParts(question).parts.join('')).toBe(question);
  });
});

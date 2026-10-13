import { describe, expect, it } from 'vitest';
import { getAllPosts, getPost, parsePost, formatDate } from './blog';

const source = (extra: Record<string, unknown> = {}) => `---\n${JSON.stringify({ title: 'A guide', description: 'A useful guide', author: 'Rob', date: '2026-10-06', category: 'Guide', image: '/blog/a-guide/opengraph-image', ...extra })}\n---\nRead this guide.`;

describe('blog publishing', () => {
  it('parses metadata and body independently, including Windows line endings', () => {
    const post = parsePost(source().replace(/\n/g, '\r\n'), 'a-guide');
    expect(post.content).toBe('Read this guide.');
    expect(post.readingMinutes).toBe(1);
    expect(post.date).toBe('2026-10-06');
  });
  it('rejects invalid publishing metadata rather than emitting broken pages', () => {
    expect(() => parsePost(source({ title: '' }), 'a-guide')).toThrow('Missing title');
    expect(() => parsePost(source({ date: '2026-02-30' }), 'a-guide')).toThrow('Invalid date');
    expect(() => parsePost(source({ draft: 'false' }), 'a-guide')).toThrow('Invalid draft');
    expect(() => parsePost(source({ image: '//external.test/x' }), 'a-guide')).toThrow('local share image');
  });
  it('does not resolve unpublished or arbitrary paths', () => {
    expect(getPost('../../package')).toBeUndefined();
    expect(getPost('missing-article')).toBeUndefined();
    expect(getAllPosts().every((post) => !post.draft)).toBe(true);
    expect(parsePost(source({ draft: true }), 'a-guide').draft).toBe(true);
  });
  it('provides the first guide and stable calendar dates', () => {
    expect(getPost('run-a-local-llm-on-another-computer')?.title).toBe('How to run a local LLM on another computer');
    expect(formatDate('2026-10-06')).toBe('October 6, 2026');
  });
});

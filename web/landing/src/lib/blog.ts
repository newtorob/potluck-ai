import fs from 'node:fs';
import path from 'node:path';

const CONTENT_DIR = path.join(process.cwd(), 'src/content/blog');
export const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL?.trim().replace(/\/$/, '') || 'https://trypotluck.ai';

export interface BlogPost {
  slug: string;
  title: string;
  description: string;
  author: string;
  date: string;
  updated?: string;
  category: string;
  image: string;
  diagram: 'remote-inference' | 'split-inference' | 'open-source';
  draft: boolean;
  content: string;
  readingMinutes: number;
}

/** JSON front matter keeps article metadata typed without adding a YAML dependency. */
export function parsePost(source: string, slug: string): BlogPost {
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug)) throw new Error(`Invalid article slug: ${slug}`);
  const match = source.replace(/\r\n/g, '\n').match(/^---\n([\s\S]*?)\n---\n([\s\S]+)$/);
  if (!match) throw new Error(`Missing article front matter: ${slug}`);
  const metadata = JSON.parse(match[1]!) as Record<string, unknown>;
  for (const key of ['title', 'description', 'author', 'date', 'category', 'image']) {
    if (typeof metadata[key] !== 'string' || !metadata[key].trim()) throw new Error(`Missing ${key}: ${slug}`);
  }
  for (const key of ['date', 'updated']) {
    const value = metadata[key];
    if (value !== undefined && (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(Date.parse(value)) || new Date(value).toISOString().slice(0, 10) !== value)) {
      throw new Error(`Invalid ${key}: ${slug}`);
    }
  }
  if (metadata.draft !== undefined && typeof metadata.draft !== 'boolean') throw new Error(`Invalid draft flag: ${slug}`);
  if (metadata.diagram !== undefined && metadata.diagram !== 'remote-inference' && metadata.diagram !== 'split-inference' && metadata.diagram !== 'open-source') throw new Error(`Invalid article diagram: ${slug}`);
  if (!(metadata.image as string).startsWith('/') || (metadata.image as string).startsWith('//')) throw new Error(`Use a local share image: ${slug}`);
  const content = match[2]!.trim();
  return {
    slug, title: metadata.title as string, description: metadata.description as string,
    author: metadata.author as string, date: metadata.date as string,
    ...(metadata.updated ? { updated: metadata.updated as string } : {}),
    category: metadata.category as string, image: metadata.image as string,
    diagram: metadata.diagram === 'open-source' ? 'open-source' : metadata.diagram === 'split-inference' ? 'split-inference' : 'remote-inference',
    draft: metadata.draft === true, content,
    readingMinutes: Math.max(1, Math.ceil(content.split(/\s+/).length / 220)),
  };
}

export function getAllPosts(): BlogPost[] {
  return fs.readdirSync(CONTENT_DIR)
    .filter((file) => file.endsWith('.md'))
    .map((file) => parsePost(fs.readFileSync(path.join(CONTENT_DIR, file), 'utf8'), file.slice(0, -3)))
    .filter((post) => !post.draft)
    .sort((a, b) => b.date.localeCompare(a.date) || a.slug.localeCompare(b.slug));
}

export function getPost(slug: string): BlogPost | undefined {
  // Look up published slugs; never interpolate a URL parameter into a file path.
  return getAllPosts().find((post) => post.slug === slug);
}

export function formatDate(date: string): string {
  return new Intl.DateTimeFormat('en-US', { month: 'long', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(new Date(`${date}T00:00:00Z`));
}

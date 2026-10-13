import type { MetadataRoute } from 'next';
import { getAllPosts, SITE_URL } from '@/lib/blog';

export default function sitemap(): MetadataRoute.Sitemap {
  // lastModified: use the date the page content was last meaningfully edited.
  // Do NOT set to new Date() — that tells crawlers every page changed on every
  // deploy, which wastes crawl budget and suppresses real change signals.
  // Update the relevant date when you edit a page's content.
  const posts = getAllPosts();
  return [
    { url: `${SITE_URL}/blog`, lastModified: new Date(posts[0]?.updated ?? posts[0]?.date ?? '2026-10-06'), changeFrequency: 'weekly', priority: 0.8 },
    ...posts.map((post) => ({ url: `${SITE_URL}/blog/${post.slug}`, lastModified: new Date(post.updated ?? post.date), changeFrequency: 'monthly' as const, priority: 0.7 })),
    {
      url: SITE_URL,
      lastModified: new Date('2026-10-13'),
      changeFrequency: 'weekly',
      priority: 1,
    },
    {
      url: `${SITE_URL}/how-it-works`,
      lastModified: new Date('2026-09-16'),
      changeFrequency: 'monthly',
      priority: 0.8,
    },
    {
      url: `${SITE_URL}/governance`,
      lastModified: new Date('2026-09-16'),
      changeFrequency: 'monthly',
      priority: 0.8,
    },
    {
      url: `${SITE_URL}/roadmap`,
      lastModified: new Date('2026-10-13'),
      changeFrequency: 'weekly',
      priority: 0.7,
    },
    {
      url: `${SITE_URL}/manifesto`,
      lastModified: new Date('2026-09-16'),
      changeFrequency: 'monthly',
      priority: 0.9,
    },
    {
      url: `${SITE_URL}/benchmarks`,
      lastModified: new Date('2026-10-13'),
      changeFrequency: 'monthly',
      priority: 0.7,
    },
    {
      url: `${SITE_URL}/install`,
      lastModified: new Date('2026-09-16'),
      changeFrequency: 'monthly',
      priority: 0.8,
    },
    {
      url: `${SITE_URL}/careers`,
      lastModified: new Date('2026-10-06'),
      changeFrequency: 'monthly',
      priority: 0.6,
    },
    {
      url: `${SITE_URL}/privacy`,
      lastModified: new Date('2026-10-06'),
      changeFrequency: 'yearly',
      priority: 0.3,
    },
    {
      url: `${SITE_URL}/terms`,
      lastModified: new Date('2026-10-13'),
      changeFrequency: 'yearly',
      priority: 0.3,
    },
  ];
}

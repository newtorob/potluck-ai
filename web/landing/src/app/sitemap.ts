import type { MetadataRoute } from 'next';

// Update SITE_URL when the domain is confirmed.
// NEXT_PUBLIC_SITE_URL lets preview deploys use their own origin so canonical
// URLs and sitemap entries resolve correctly. Falls back to production domain.
const SITE_URL =
  process.env['NEXT_PUBLIC_SITE_URL']?.trim().replace(/\/$/, '') ?? 'https://trypotluck.ai';

export default function sitemap(): MetadataRoute.Sitemap {
  // lastModified: use the date the page content was last meaningfully edited.
  // Do NOT set to new Date() — that tells crawlers every page changed on every
  // deploy, which wastes crawl budget and suppresses real change signals.
  // Update the relevant date when you edit a page's content.
  return [
    {
      url: SITE_URL,
      lastModified: new Date('2026-09-22'),
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
      lastModified: new Date('2026-09-16'),
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
      lastModified: new Date('2026-09-16'),
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
      lastModified: new Date('2026-09-22'),
      changeFrequency: 'monthly',
      priority: 0.6,
    },
    {
      url: `${SITE_URL}/privacy`,
      lastModified: new Date('2026-09-16'),
      changeFrequency: 'yearly',
      priority: 0.3,
    },
    {
      url: `${SITE_URL}/terms`,
      lastModified: new Date('2026-09-16'),
      changeFrequency: 'yearly',
      priority: 0.3,
    },
  ];
}

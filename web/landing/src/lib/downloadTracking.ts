/**
 * Download counting for the /dl/[platform] redirect routes.
 *
 * Each download button links to /dl/<slug> instead of the raw R2 URL. The
 * route logs platform, timestamp, coarse source and optional article to Supabase,
 * then 302s to the real file. Logging is strictly
 * fail-open: if Supabase is down, slow, or unconfigured, the download still
 * redirects.
 */

import { DOWNLOADS } from './downloads';
import type { DownloadAttribution } from './attribution';

export const DOWNLOAD_SLUGS = {
  'mac-arm': DOWNLOADS.macArm,
  'mac-intel': DOWNLOADS.macIntel,
  windows: DOWNLOADS.windows,
  'linux-deb': DOWNLOADS.linuxDeb,
  'linux-appimage': DOWNLOADS.linuxAppImage,
} as const;

export type DownloadSlug = keyof typeof DOWNLOAD_SLUGS;

export function resolveDownload(slug: string): string | null {
  return Object.hasOwn(DOWNLOAD_SLUGS, slug) ? DOWNLOAD_SLUGS[slug as DownloadSlug] : null;
}

// Crawlers and link-preview fetchers follow the /dl links and would inflate
// the count. curl/wget are deliberately NOT matched: copying the Linux link
// onto a server is a real download path.
const BOT_UA =
  /bot|crawl|spider|slurp|preview|headless|lighthouse|facebookexternalhit|whatsapp|telegram|discord|skypeuripreview|embedly|quora link/i;

export function isLikelyBot(userAgent: string | null): boolean {
  return userAgent !== null && BOT_UA.test(userAgent);
}

interface SupabaseEnv {
  url: string | undefined;
  key: string | undefined;
}

export async function logDownload(
  slug: string,
  env: SupabaseEnv = {
    url: process.env.DOWNLOADS_SUPABASE_URL,
    key: process.env.DOWNLOADS_SUPABASE_ANON_KEY,
  },
  fetchImpl: typeof fetch = fetch,
  attribution: DownloadAttribution = { source: 'unknown', article: null },
): Promise<void> {
  if (!env.url || !env.key) return;
  try {
    const init = {
      method: 'POST',
      headers: {
        apikey: env.key,
        Authorization: `Bearer ${env.key}`,
        'Content-Type': 'application/json',
      },
      signal: AbortSignal.timeout(1500),
    };
    const response = await fetchImpl(`${env.url}/rest/v1/rpc/potluck_log_attributed_download`, {
      ...init, body: JSON.stringify({ p_platform: slug, p_source: attribution.source, p_article: attribution.article }),
    });
    // Deployments can overlap: only a confirmed missing RPC is safe to retry.
    // Never retry a timeout/server error; it may already have inserted the row.
    if (response.status === 404 && (await response.json()).code === 'PGRST202') {
      await fetchImpl(`${env.url}/rest/v1/rpc/potluck_log_download`, {
        ...init, body: JSON.stringify({ p_platform: slug }),
      });
    }
  } catch {
    // Fail open: the redirect matters more than the count.
  }
}

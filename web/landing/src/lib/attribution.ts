/** Coarse, tab-scoped attribution. Never retain raw referrers, UTM values or IDs. */
export const ATTRIBUTION_KEY = 'potluck:visit:v1';
export const VISIT_TTL_MS = 30 * 60 * 1000;
export const SOURCES = ['direct', 'google', 'bing', 'duckduckgo', 'yahoo', 'x', 'reddit', 'discord', 'email', 'paid-campaign', 'campaign', 'other-referral'] as const;
export type VisitSource = (typeof SOURCES)[number];
export interface Attribution { source: VisitSource; article: string | null; expires: number }
export interface DownloadAttribution { source: VisitSource | 'unknown'; article: string | null }

/** Only bounded categories and a published slug may reach the download counter. */
export function downloadAttribution(url: URL, published: readonly string[]): DownloadAttribution {
  const source = url.searchParams.get('source') as VisitSource;
  const article = url.searchParams.get('article');
  return { source: SOURCES.includes(source) ? source : 'unknown', article: article && published.includes(article) ? article : null };
}

function knownSource(host: string): VisitSource {
  if (/^(?:www\.)?google\.(?:com|[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2})$/.test(host)) return 'google';
  for (const [domain, source] of [['bing.com', 'bing'], ['duckduckgo.com', 'duckduckgo'], ['yahoo.com', 'yahoo'], ['x.com', 'x'], ['twitter.com', 'x'], ['t.co', 'x'], ['reddit.com', 'reddit'], ['discord.com', 'discord'], ['discord.gg', 'discord']] as const) {
    if (host === domain || host.endsWith(`.${domain}`)) return source;
  }
  return 'other-referral';
}

export function classifySource(url: URL, referrer: string): VisitSource {
  const medium = url.searchParams.get('utm_medium')?.toLowerCase();
  if (['cpc', 'ppc', 'paid', 'paid_social', 'display'].includes(medium ?? '') || url.searchParams.has('gclid') || url.searchParams.has('msclkid')) return 'paid-campaign';
  if (medium === 'email') return 'email';
  const tagged = url.searchParams.get('utm_source')?.toLowerCase();
  if (tagged) {
    const aliases: Record<string, VisitSource> = { google: 'google', bing: 'bing', duckduckgo: 'duckduckgo', yahoo: 'yahoo', x: 'x', twitter: 'x', reddit: 'reddit', discord: 'discord', newsletter: 'email', email: 'email' };
    return Object.hasOwn(aliases, tagged) ? aliases[tagged]! : 'campaign';
  }
  if (!referrer) return 'direct';
  try {
    const referring = new URL(referrer);
    if (!['https:', 'http:'].includes(referring.protocol)) return 'direct';
    return referring.origin === url.origin ? 'direct' : knownSource(referring.hostname);
  } catch { return 'direct'; }
}

export function articleAtPath(pathname: string, published: readonly string[]): string | null {
  const slug = pathname.replace(/\/$/, '').match(/^\/blog\/([a-z0-9-]+)$/)?.[1];
  return slug && published.includes(slug) ? slug : null;
}

export function nextAttribution(raw: string | null, url: URL, referrer: string, published: readonly string[], now = Date.now()): Attribution {
  let previous: Attribution | null = null;
  try {
    const value = JSON.parse(raw ?? 'null') as Attribution | null;
    if (value && SOURCES.includes(value.source) && Number.isFinite(value.expires) && value.expires > now && value.expires <= now + VISIT_TTL_MS) {
      previous = { source: value.source, expires: value.expires, article: value.article && published.includes(value.article) ? value.article : null };
    }
  } catch { /* Disabled or corrupted storage must not affect navigation. */ }
  return {
    source: previous?.source ?? classifySource(url, referrer),
    article: articleAtPath(url.pathname, published) ?? previous?.article ?? null,
    expires: previous?.expires ?? now + VISIT_TTL_MS,
  };
}

export function redactAnalyticsUrl(url: string): string | null {
  try {
    const parsed = new URL(url);
    if (parsed.pathname === '/auth' || parsed.pathname.startsWith('/auth/')) return null;
    parsed.search = ''; parsed.hash = '';
    return parsed.toString();
  } catch { return null; }
}

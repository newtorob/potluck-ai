'use client';

import { Analytics } from '@vercel/analytics/react';
import { usePathname } from 'next/navigation';
import { useEffect, useRef } from 'react';
import { DOWNLOAD_LINKS } from '@/lib/downloads';
import { ATTRIBUTION_KEY, nextAttribution, redactAnalyticsUrl, type Attribution } from '@/lib/attribution';

const DOWNLOAD_PATHS = new Set(Object.values(DOWNLOAD_LINKS).filter((path) => path !== '/dl/mac-intel'));

export function SiteAnalytics({ publishedArticles }: { publishedArticles: string[] }) {
  const pathname = usePathname();
  const visit = useRef<Attribution | null>(null);

  useEffect(() => {
    // Account URLs may carry sign-in tokens. Never inspect or attribute them.
    if (pathname === '/auth' || pathname.startsWith('/auth/')) return;
    let stored: string | null = null;
    try { stored = sessionStorage.getItem(ATTRIBUTION_KEY); } catch { /* memory fallback */ }
    visit.current = nextAttribution(stored ?? JSON.stringify(visit.current), new URL(location.href), document.referrer, publishedArticles);
    try { sessionStorage.setItem(ATTRIBUTION_KEY, JSON.stringify(visit.current)); } catch { /* memory fallback */ }

    function attributeLink(link: Element) {
      const target = new URL(link.getAttribute('href')!, location.href);
      if (target.origin !== location.origin || !DOWNLOAD_PATHS.has(target.pathname)) return;
      visit.current = nextAttribution(JSON.stringify(visit.current), new URL(location.href), document.referrer, publishedArticles);
      // The redirect logs the request; no paid client-event service is needed.
      target.search = ''; target.hash = '';
      target.searchParams.set('source', visit.current.source);
      if (visit.current.article) target.searchParams.set('article', visit.current.article);
      link.setAttribute('href', `${target.pathname}${target.search}`);
    }
    document.querySelectorAll('a[href]').forEach(attributeLink);

    function onDownload(event: MouseEvent) {
      if (event.defaultPrevented) return;
      const link = event.target instanceof Element ? event.target.closest('a[href]') : null;
      if (link) attributeLink(link);
    }
    // Capture also covers dynamically mounted links and refreshes expired visits.
    // Context-menu and middle-click downloads keep the same attribution.
    document.addEventListener('click', onDownload, true);
    document.addEventListener('auxclick', onDownload, true);
    document.addEventListener('contextmenu', onDownload, true);
    return () => {
      document.removeEventListener('click', onDownload, true);
      document.removeEventListener('auxclick', onDownload, true);
      document.removeEventListener('contextmenu', onDownload, true);
    };
  }, [pathname, publishedArticles]);

  return <Analytics beforeSend={(event) => {
    const url = redactAnalyticsUrl(event.url);
    return url ? { ...event, url } : null;
  }} />;
}

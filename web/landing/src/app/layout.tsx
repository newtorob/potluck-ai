import type { Metadata, Viewport } from 'next';
import { SiteAnalytics } from '@/components/SiteAnalytics';
import { getAllPosts } from '@/lib/blog';
import '@/styles/fonts.css';
import '@/styles/globals.css';
import '@/styles/site.css';
import { Header } from '@/components/Header';
import { Footer } from '@/components/Footer';

// Preload only the shared shell and homepage faces. Editorial and code fonts
// load from our own origin when a page uses them; builds need no font downloads.
const PRELOADED_FONTS = [
  'bricolage-grotesque-normal-latin.woff2',
  'instrument-sans-normal-latin.woff2',
];

// ─── Metadata ─────────────────────────────────────────────────────────────────

export const metadata: Metadata = {
  title: {
    default: 'Potluck | Bigger models. Same computers.',
    template: '%s | Potluck',
  },
  description:
    'Potluck is open-source AI for the computers you already own. Explore model splitting, local inference, and a member-operated compute network. MIT licensed.',
  metadataBase: new URL(
    process.env['NEXT_PUBLIC_SITE_URL']?.trim().replace(/\/$/, '') ?? 'https://trypotluck.ai',
  ),
  openGraph: {
    type: 'website',
    locale: 'en_US',
    siteName: 'Potluck',
    title: 'Potluck | Bigger models. Same computers.',
    description:
      'Open-source AI on your own machines. Explore Potluck’s model-splitting work and local AI app. MIT licensed.',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Potluck | Bigger models. Same computers.',
    description:
      'Open-source AI on your own machines. Explore Potluck’s model-splitting work and local AI app. MIT licensed.',
  },
  robots: {
    index: true,
    follow: true,
  },
};

export const viewport: Viewport = {
  themeColor: '#fbfaf9',
};

// ─── Root layout ──────────────────────────────────────────────────────────────

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        {PRELOADED_FONTS.map((font) => (
          <link key={font} rel="preload" href={`/fonts/${font}`} as="font" type="font/woff2" crossOrigin="anonymous" />
        ))}
      </head>
      <body>
        {/* Skip-to-content link for keyboard users */}
        <a href="#main-content" className="skip-nav">
          Skip to main content
        </a>
        <div className="flex min-h-dvh flex-col">
          <Header />
          <main id="main-content" className="flex-1" tabIndex={-1}>
            {children}
          </main>
          <Footer />
        </div>
        <SiteAnalytics publishedArticles={getAllPosts().map((post) => post.slug)} />
      </body>
    </html>
  );
}

import type { Metadata, Viewport } from 'next';
import { Analytics } from '@vercel/analytics/react';
import '@/styles/fonts.css';
import '@/styles/globals.css';
import { Header } from '@/components/Header';
import { Footer } from '@/components/Footer';

// Pin the same self-hosted font files used by the approved design. Production
// builds must not depend on Google Fonts returning a particular URL format.
const PRELOADED_FONTS = [
  'newsreader-normal-latin.woff2',
  'newsreader-italic-latin.woff2',
  'familjen-grotesk-normal-latin.woff2',
  'spline-sans-mono-normal-latin.woff2',
];

// ─── Metadata ─────────────────────────────────────────────────────────────────

export const metadata: Metadata = {
  title: {
    default: 'Potluck | Local AI, connected by you',
    template: '%s | Potluck',
  },
  description:
    'Run open models on your hardware. Connect your own machines with Potluck, a local-first desktop app building toward a member-operated compute network.',
  metadataBase: new URL(
    process.env['NEXT_PUBLIC_SITE_URL']?.trim().replace(/\/$/, '') ?? 'https://trypotluck.ai',
  ),
  openGraph: {
    type: 'website',
    locale: 'en_US',
    siteName: 'Potluck',
    title: 'Potluck | Local AI, connected by you',
    description:
      'Run open models locally and connect your own machines. A member-operated compute network is the next chapter.',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Potluck | Local AI, connected by you',
    description:
      'Run open models locally and connect your own machines. A member-operated compute network is the next chapter.',
  },
  robots: {
    index: true,
    follow: true,
  },
};

export const viewport: Viewport = {
  // Hearth charcoal matches the site-wide dark theme so the browser chrome
  // blends in on mobile.
  themeColor: '#1A1714',
};

// ─── Root layout ──────────────────────────────────────────────────────────────

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      // 'dark' switches every token-based page onto the Hearth charcoal
      // block in globals.css — the whole site runs dark, matching the
      // desktop app and the Living Mesh landing page.
      className="dark"
    >
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
          <main id="main-content" className="flex-1">
            {children}
          </main>
          <Footer />
        </div>
        <Analytics />
      </body>
    </html>
  );
}

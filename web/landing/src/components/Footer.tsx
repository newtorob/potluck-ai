import Link from 'next/link';
import { DISCORD_INVITE, X_URL } from '@/lib/downloads';

export function Footer() {
  return (
    <footer className="site-footer border-t border-foreground/25 text-sm text-muted-foreground font-sans">
      <div className="mx-auto max-w-[1320px] px-6 md:px-10 py-10">
        <div className="flex flex-col gap-8 md:flex-row md:items-start md:justify-between">
          {/* Left — about */}
          <div className="max-w-xs">
            <p className="font-serif text-base text-foreground font-medium">Potluck</p>
            <p className="mt-2 leading-relaxed text-xs">
              Local AI today. A member-operated compute network in the making. Everyone brings what
              they can.
            </p>
          </div>

          {/* Middle — navigation */}
          <nav aria-label="Footer navigation">
            <p className="mb-3 font-mono text-[0.62rem] uppercase tracking-[0.14em] text-muted-foreground">
              Pages
            </p>
            <ul className="flex flex-col gap-2" role="list">
              {[
                { href: '/how-it-works', label: 'How it works' },
                { href: '/benchmarks', label: 'Benchmarks' },
                { href: '/governance', label: 'Governance' },
                { href: '/roadmap', label: 'Roadmap' },
                { href: '/manifesto', label: 'Manifesto' },
                { href: '/careers', label: 'Careers' },
              ].map(({ href, label }) => (
                <li key={href}>
                  <Link
                    href={href}
                    className="hover:text-primary transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm"
                  >
                    {label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>

          {/* Right — join */}
          <div>
            <p className="mb-3 font-mono text-[0.62rem] uppercase tracking-[0.14em] text-muted-foreground">
              Get involved
            </p>
            <ul className="flex flex-col gap-2" role="list">
              <li>
                <Link
                  href="/install"
                  className="hover:text-primary transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm"
                >
                  Download the app
                </Link>
              </li>
              <li>
                <a
                  href={DISCORD_INVITE}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="hover:text-primary transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm"
                >
                  Discord
                </a>
              </li>
              <li>
                <a
                  href={X_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="hover:text-primary transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm"
                >
                  X / @potluckAI
                </a>
              </li>
              <li>
                <Link
                  href="/manifesto"
                  className="hover:text-primary transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm"
                >
                  Read the manifesto
                </Link>
              </li>
            </ul>
          </div>
        </div>

        {/* Cornerstone */}
        <div className="mt-10 pt-6 border-t border-border flex flex-col items-center gap-1.5 text-center">
          <p className="font-serif text-sm text-muted-foreground">
            Potluck · Local AI, connected by you
          </p>
          <p className="font-mono text-[0.6rem] tracking-[0.12em] uppercase text-muted-foreground">
            Est. 2026 · Cookieless website analytics · No advertising trackers
          </p>
          <nav aria-label="Legal" className="mt-1.5 flex items-center gap-3 text-xs">
            <Link
              href="/privacy"
              className="hover:text-primary transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm"
            >
              Privacy
            </Link>
            <span aria-hidden className="text-muted-foreground/40">
              ·
            </span>
            <Link
              href="/terms"
              className="hover:text-primary transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm"
            >
              Terms
            </Link>
          </nav>
        </div>
      </div>
    </footer>
  );
}

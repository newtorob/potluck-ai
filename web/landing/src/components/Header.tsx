'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useState, useEffect, useRef } from 'react';

const NAV_LINKS = [
  { href: '/how-it-works', label: 'How it works' },
  { href: '/roadmap', label: 'Roadmap' },
  { href: '/manifesto', label: 'The idea' },
  { href: '/careers', label: 'Careers' },
] as const;

// Note on SSR split: the entire header is 'use client' because:
//   (a) mobile menu toggle requires useState,
//   (b) active-link highlighting requires usePathname,
//   (c) the scrolled hairline requires a scroll listener.
// All thin concerns — no data fetching, no heavy computation.

export function Header() {
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const menuButtonRef = useRef<HTMLButtonElement>(null);

  // Close menu on route change
  useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

  // A mobile menu must not leave the page inert when the viewport becomes desktop.
  useEffect(() => {
    const desktop = window.matchMedia('(min-width: 768px)');
    const closeOnDesktop = () => {
      if (desktop.matches) setMenuOpen(false);
    };
    desktop.addEventListener('change', closeOnDesktop);
    return () => desktop.removeEventListener('change', closeOnDesktop);
  }, []);

  // Hairline appears once the page scrolls, matching the landing header.
  useEffect(() => {
    function onScroll() {
      setScrolled(window.scrollY > 8);
    }
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  // Close menu on Escape key
  useEffect(() => {
    if (!menuOpen) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        setMenuOpen(false);
        menuButtonRef.current?.focus();
      }
    }
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [menuOpen]);

  // Focus trap via `inert` — when the mobile menu is open, keyboard focus
  // cannot reach content behind the menu.
  useEffect(() => {
    const main = document.getElementById('main-content');
    const footer = document.querySelector('footer');

    if (menuOpen) {
      main?.setAttribute('inert', '');
      footer?.setAttribute('inert', '');
    } else {
      main?.removeAttribute('inert');
      footer?.removeAttribute('inert');
    }

    return () => {
      main?.removeAttribute('inert');
      footer?.removeAttribute('inert');
    };
  }, [menuOpen]);

  return (
    <header
      className={[
        'sticky top-0 z-40 bg-background border-b transition-colors duration-200',
        pathname === '/' ? 'neighborhood-header' : '',
        pathname === '/careers' ? 'careers-header' : '',
        scrolled ? 'border-border' : 'border-transparent',
      ].join(' ')}
    >
      <div className="mx-auto flex max-w-[1320px] items-center justify-between px-6 md:px-10 h-16">
        {/* Wordmark — serif with the mono cornerstone tag */}
        <Link
          href="/"
          className="inline-flex items-baseline gap-2.5 font-serif text-2xl font-normal tracking-tight text-foreground hover:text-primary transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background rounded-sm"
          aria-label="Potluck home"
        >
          Potluck
          <span className="hidden lg:inline font-mono text-[0.6rem] font-normal tracking-[0.14em] text-muted-foreground">
            LOCAL AI, CONNECTED
          </span>
        </Link>

        {/* Desktop nav — mono uppercase, ember active state */}
        <nav className="hidden items-center gap-4 lg:gap-6 md:flex" aria-label="Main navigation">
          {NAV_LINKS.map(({ href, label }) => (
            <Link
              key={href}
              href={href}
              className={[
                'font-mono text-[0.68rem] uppercase tracking-[0.1em] py-3 border-b transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background rounded-sm',
                pathname === href || pathname.startsWith(href + '/')
                  ? 'text-primary border-primary/50'
                  : 'text-muted-foreground border-transparent hover:text-foreground hover:border-border',
              ].join(' ')}
              aria-current={pathname === href ? 'page' : undefined}
            >
              {label}
            </Link>
          ))}
          <Link
            href="/install"
            className="font-mono text-[0.68rem] uppercase tracking-[0.1em] text-primary border border-primary/40 rounded px-4 py-3 hover:bg-primary/10 hover:border-primary/70 transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            Get Potluck ↗
          </Link>
        </nav>

        {/* Mobile menu toggle — 44×44px minimum touch target */}
        <button
          ref={menuButtonRef}
          type="button"
          className="inline-flex items-center justify-center rounded-sm p-3 text-muted-foreground hover:text-foreground hover:bg-accent md:hidden focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          aria-expanded={menuOpen}
          aria-controls="mobile-menu"
          aria-label={menuOpen ? 'Close navigation menu' : 'Open navigation menu'}
          onClick={() => setMenuOpen((prev) => !prev)}
        >
          {menuOpen ? (
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path
                d="M4 4L16 16M16 4L4 16"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
              />
            </svg>
          ) : (
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path
                d="M3 6h14M3 10h14M3 14h14"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
              />
            </svg>
          )}
        </button>
      </div>

      {/* Mobile nav drawer */}
      {menuOpen && (
        <nav
          id="mobile-menu"
          className="border-t border-border bg-background/95 backdrop-blur-md px-6 pb-4 pt-2 md:hidden"
          aria-label="Mobile navigation"
        >
          <ul className="flex flex-col gap-1" role="list">
            {NAV_LINKS.map(({ href, label }) => (
              <li key={href}>
                <Link
                  href={href}
                  onClick={() => setMenuOpen(false)}
                  aria-current={pathname === href ? 'page' : undefined}
                  className={[
                    'block font-mono text-[0.72rem] uppercase tracking-[0.1em] rounded-sm px-3 py-3 border-b border-border/60 transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
                    pathname === href
                      ? 'text-primary'
                      : 'text-muted-foreground hover:text-foreground hover:bg-accent',
                  ].join(' ')}
                >
                  {label}
                </Link>
              </li>
            ))}
            <li className="mt-3 px-3 pb-1">
              <Link
                href="/install"
                onClick={() => setMenuOpen(false)}
                className="inline-block font-mono text-[0.72rem] uppercase tracking-[0.1em] text-primary border border-primary/40 rounded px-3 py-2 hover:bg-primary/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                Download
              </Link>
            </li>
          </ul>
        </nav>
      )}
    </header>
  );
}

'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';
import { Arrow } from './Arrow';
import { SOURCE_URL } from '@/lib/openSource';

const NAV_LINKS = [
  { href: '/how-it-works', label: 'How it works' },
  { href: '/blog', label: 'Blog' },
  { href: '/roadmap', label: 'Roadmap' },
  { href: '/manifesto', label: 'Why Potluck' },
] as const;

export function Header() {
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  useEffect(() => setMenuOpen(false), [pathname]);
  useEffect(() => {
    const desktop = window.matchMedia('(min-width: 900px)');
    const close = () => { if (desktop.matches) setMenuOpen(false); };
    desktop.addEventListener('change', close);
    return () => desktop.removeEventListener('change', close);
  }, []);
  useEffect(() => {
    if (!menuOpen) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setMenuOpen(false);
        menuButton.current?.focus();
      }
    };
    document.addEventListener('keydown', escape);
    return () => document.removeEventListener('keydown', escape);
  }, [menuOpen]);
  const active = (href: string) => pathname === href || pathname.startsWith(`${href}/`);

  return (
    <header className={`site-header${pathname === '/' ? ' site-header-home' : ''}`}>
      <div className="site-shell site-header-row">
        <Link href="/" className="site-wordmark" aria-label="Potluck home">Potluck<span>Your machines, together</span></Link>
        <nav className="site-desktop-nav" aria-label="Main navigation">
          {NAV_LINKS.map(({ href, label }) => (
            <Link key={href} href={href} aria-current={active(href) ? 'page' : undefined}>{label}</Link>
          ))}
          <a href={SOURCE_URL}>GitHub</a>
        </nav>
        <div className="site-header-actions">
          <Link href="/install" className="btn-primary">Download <Arrow /></Link>
          <button ref={menuButton} className="site-menu-toggle" type="button" aria-expanded={menuOpen} aria-controls="mobile-menu" aria-label={menuOpen ? 'Close navigation menu' : 'Open navigation menu'} onClick={() => setMenuOpen(!menuOpen)}>
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d={menuOpen ? 'm6 6 12 12M6 18 18 6' : 'M4 7h16M4 12h16M4 17h16'} stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
          </button>
        </div>
      </div>
      {/* A non-modal disclosure: links stay in normal keyboard order. */}
      <nav id="mobile-menu" className="site-mobile-nav site-shell" aria-label="Mobile navigation" hidden={!menuOpen}>
        {NAV_LINKS.map(({ href, label }) => (
          <Link key={href} href={href} onClick={() => setMenuOpen(false)} aria-current={active(href) ? 'page' : undefined}>{label}<Arrow /></Link>
        ))}
        <a href={SOURCE_URL} onClick={() => setMenuOpen(false)}>GitHub <Arrow /></a>
      </nav>
    </header>
  );
}

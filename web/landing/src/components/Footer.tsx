import Link from 'next/link';
import { SOURCE_URL, LICENSE_URL, CONTRIBUTING_URL } from '@/lib/openSource';
import { DISCORD_INVITE, X_URL } from '@/lib/downloads';

const GROUPS = [
  { title: 'Product', links: [['Download', '/install'], ['How it works', '/how-it-works'], ['Roadmap', '/roadmap']] },
  { title: 'Learn', links: [['Blog', '/blog'], ['Setup guide', '/blog/run-a-local-llm-on-another-computer'], ['Benchmarks', '/benchmarks']] },
  { title: 'Company', links: [['Why Potluck', '/manifesto'], ['Governance', '/governance'], ['Careers', '/careers'], ['Contribute', CONTRIBUTING_URL]] },
] as const;

export function Footer() {
  return (
    <footer className="site-footer">
      <div className="site-shell">
        <div className="site-footer-grid">
          <div className="site-footer-about">
            <Link href="/" className="site-wordmark">Potluck</Link>
            <p>Bigger models. Same computers.<br />A network we can build together.</p>
            <div className="site-social-links">
              <a href={DISCORD_INVITE} target="_blank" rel="noopener noreferrer">Discord</a>
              <a href={SOURCE_URL}>GitHub</a>
              <a href={X_URL} target="_blank" rel="noopener noreferrer">X / @potluckAI</a>
            </div>
          </div>
          {GROUPS.map(({ title, links }) => (
            <nav key={title} aria-label={`${title} links`}>
              <h2>{title}</h2>
              <ul>{links.map(([label, href]) => <li key={href}><Link href={href}>{label}</Link></li>)}</ul>
            </nav>
          ))}
        </div>
        <div className="site-footer-bottom">
          <p>© {new Date().getUTCFullYear()} Potluck AI · <a href={LICENSE_URL}>Open source under MIT</a></p>
          <p>Cookieless website analytics. No advertising trackers.</p>
          <nav aria-label="Legal"><Link href="/privacy">Privacy</Link><Link href="/terms">Terms</Link></nav>
        </div>
      </div>
    </footer>
  );
}

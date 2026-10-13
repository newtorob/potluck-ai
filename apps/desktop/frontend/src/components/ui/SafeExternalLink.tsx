/**
 * SafeExternalLink - the only `a:` renderer markdown-sourced content (memory
 * notes, chat replies) is allowed to use.
 *
 * Two invariants close the click-through that let a poisoned
 * `[x](file:///…)` link replace the whole BrowserWindow - and its live
 * preload contextBridge (window.potluck: cloud bearer session, mesh access)
 * - with arbitrary local content (see main.js will-navigate origin gate):
 *
 *   1. Only a fully-qualified http(s) href is ever clickable. Anything else -
 *      file:, javascript:, data:, custom schemes, relative/scheme-less
 *      paths - renders as inert text. We never construct a hyperlink (or
 *      navigate anything) from an unvalidated scheme.
 *   2. An allowed link is opened only via window.open(), never by letting the
 *      click become an in-window navigation. window.open() is intercepted by
 *      Electron's setWindowOpenHandler in main.js, which applies its own
 *      independent http(s)-only allowlist (safeOpenExternal) before ever
 *      touching shell.openExternal.
 */

import { useCallback, type AnchorHTMLAttributes, type MouseEvent, type ReactNode } from 'react';

const ALLOWED_PROTOCOLS = new Set(['http:', 'https:']);

function isSafeHref(href: string | undefined): boolean {
  if (!href) return false;
  try {
    // No base URL on purpose: relative/scheme-less hrefs (e.g. "/x", "#x")
    // throw here rather than silently resolving against the app's own
    // location, and are treated the same as any other disallowed scheme -
    // inert text.
    return ALLOWED_PROTOCOLS.has(new URL(href).protocol);
  } catch {
    return false;
  }
}

interface SafeExternalLinkProps
  extends Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'onClick' | 'target' | 'rel'> {
  children?: ReactNode;
  /** react-markdown passes the AST node on every element renderer; it must
   *  never be forwarded to the DOM. */
  node?: unknown;
}

export function SafeExternalLink({ href, children, node: _node, ...rest }: SafeExternalLinkProps) {
  const safe = isSafeHref(href);

  const handleClick = useCallback(
    (e: MouseEvent<HTMLAnchorElement>) => {
      // Always prevent the default anchor click - that is the in-window
      // navigation path we are closing off. The only way out is explicit
      // window.open(), which Electron hands to setWindowOpenHandler.
      e.preventDefault();
      if (safe && href) {
        window.open(href, '_blank', 'noopener,noreferrer');
      }
    },
    [safe, href],
  );

  if (!safe) {
    return <span {...rest}>{children}</span>;
  }

  return (
    <a href={href} onClick={handleClick} target="_blank" rel="noopener noreferrer" {...rest}>
      {children}
    </a>
  );
}

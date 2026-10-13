'use client';

import type { ComponentPropsWithoutRef } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

// Renders a legal document (privacy / terms) from its markdown source, styled
// to the site's serif-heading / sans-body system (matches the manifesto page).
// The markdown is the source of truth in src/content/legal — edit that, not
// this component. Kept deliberately plain: no scroll animation, no client state
// beyond what react-markdown needs, so the text is readable the instant it
// paints (legal pages should never feel gated behind motion).

type MdProps<T extends keyof JSX.IntrinsicElements> = ComponentPropsWithoutRef<T>;

const components = {
  h1: ({ children }: MdProps<'h1'>) => (
    <h1 className="site-reading-title">
      {children}
    </h1>
  ),
  h2: ({ children }: MdProps<'h2'>) => (
    <h2 className="font-serif text-2xl font-light text-foreground mt-12 mb-3">{children}</h2>
  ),
  h3: ({ children }: MdProps<'h3'>) => (
    <h3 className="font-serif text-xl font-light text-foreground mt-8 mb-2">{children}</h3>
  ),
  p: ({ children }: MdProps<'p'>) => <p className="mb-5">{children}</p>,
  ul: ({ children }: MdProps<'ul'>) => (
    <ul className="mb-5 list-disc space-y-2 pl-6">{children}</ul>
  ),
  ol: ({ children }: MdProps<'ol'>) => (
    <ol className="mb-5 list-decimal space-y-2 pl-6">{children}</ol>
  ),
  li: ({ children }: MdProps<'li'>) => <li className="leading-[1.7]">{children}</li>,
  a: ({ children, href }: MdProps<'a'>) => (
    <a
      href={href}
      className="text-foreground underline underline-offset-4 transition-opacity hover:opacity-70"
    >
      {children}
    </a>
  ),
  strong: ({ children }: MdProps<'strong'>) => (
    <strong className="font-medium text-foreground">{children}</strong>
  ),
  hr: () => <hr className="border-border my-8" />,
  blockquote: ({ children }: MdProps<'blockquote'>) => (
    <blockquote className="border-l-2 border-border pl-4 italic text-muted-foreground">
      {children}
    </blockquote>
  ),
  table: ({ children }: MdProps<'table'>) => (
    <div className="mb-5 overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">{children}</table>
    </div>
  ),
  th: ({ children }: MdProps<'th'>) => (
    <th className="border-b border-foreground/25 py-2 pr-4 font-medium text-foreground">
      {children}
    </th>
  ),
  td: ({ children }: MdProps<'td'>) => (
    <td className="border-b border-border py-2 pr-4 align-top">{children}</td>
  ),
};

export function LegalDocument({ eyebrow, markdown }: { eyebrow: string; markdown: string }) {
  return (
    <article className="site-page">
      <div className="reading-width mx-auto">
        <p className="eyebrow-mono mb-6">{eyebrow}</p>
        <div className="rail-rule mb-12" />
        <div className="font-sans text-base leading-[1.78] text-muted-foreground">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
            {markdown}
          </ReactMarkdown>
        </div>
      </div>
    </article>
  );
}

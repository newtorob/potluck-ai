'use client';
import Link from 'next/link';
import { useState } from 'react';
import { Arrow } from './Arrow';
import { SOURCE_URL } from '@/lib/openSource';

export function ArticleActions({ url, variant = 'download' }: { url: string; variant?: 'download' | 'build-notes' | 'open-source' }) {
  const buildNote = variant === 'build-notes';
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  async function copy() {
    try { await navigator.clipboard.writeText(url); setCopied(true); setFailed(false); }
    catch { setFailed(true); }
  }
  return (
    <div className="article-actions">
      {variant === 'open-source' ? <a className="btn-primary" href={SOURCE_URL}>View source on GitHub <Arrow /></a> : <Link className="btn-primary" href={buildNote ? '/how-it-works#model-splitting' : '/install'}>{buildNote ? 'How model splitting works' : 'Download Potluck'} <Arrow /></Link>}
      <button className="btn-outline" type="button" onClick={copy}>{copied ? 'Link copied' : 'Copy article link'}</button>
      <span className="article-share-status" role="status">{copied ? 'Article link copied to clipboard.' : failed ? 'Copy this link to share the article:' : ''}</span>
      {failed && <a className="article-share-url" href={url}>{url}</a>}
    </div>
  );
}

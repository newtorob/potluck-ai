import { ImageResponse } from 'next/og';
import { notFound } from 'next/navigation';
import { getPost } from '@/lib/blog';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

export const alt = 'Potluck blog';
export const size = { width: 1200, height: 630 };
export const contentType = 'image/png';

export default async function ArticleImage({ params }: { params: Promise<{ slug: string }> }) {
  const post = getPost((await params).slug);
  if (!post) notFound();
  const [newsreader, bricolage, instrument] = await Promise.all([
    readFile(path.join(process.cwd(), 'public/fonts/newsreader-share.ttf')),
    readFile(path.join(process.cwd(), 'public/fonts/bricolage-share.ttf')),
    readFile(path.join(process.cwd(), 'public/fonts/instrument-share.ttf')),
  ]);
  return new ImageResponse(
    <div style={{ width: '100%', height: '100%', display: 'flex', flexDirection: 'column', justifyContent: 'space-between', background: '#fbfaf9', color: '#1a1a1a', borderLeft: '16px solid #b43b0e', padding: '60px 64px 56px', fontFamily: 'Instrument Sans' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 23 }}><span style={{ fontFamily: 'Bricolage Grotesque', fontWeight: 600, fontSize: 36, letterSpacing: '-1.5px' }}>Potluck</span><span style={{ color: '#b43b0e' }}>{post.category}</span></div>
      <div style={{ display: 'flex', fontFamily: 'Newsreader', fontSize: 76, lineHeight: 1.1, letterSpacing: '-2px', maxWidth: 1000 }}>{post.title}</div>
      <div style={{ display: 'flex', justifyContent: 'space-between', borderTop: '2px solid #eae5e1', paddingTop: 24, fontSize: 22 }}><span>By {post.author}</span><span style={{ color: '#b43b0e' }}>trypotluck.ai/blog</span></div>
    </div>, {
      ...size,
      fonts: [
        { name: 'Newsreader', data: newsreader, weight: 400, style: 'normal' },
        { name: 'Bricolage Grotesque', data: bricolage, weight: 600, style: 'normal' },
        { name: 'Instrument Sans', data: instrument, weight: 400, style: 'normal' },
      ],
    },
  );
}

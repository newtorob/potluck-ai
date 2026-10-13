import type { Metadata } from 'next';
import Link from 'next/link';
import { notFound } from 'next/navigation';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { getAllPosts, getPost, formatDate, SITE_URL } from '@/lib/blog';
import { BlogArtwork } from '@/components/BlogArtwork';
import { ArticleActions } from '@/components/ArticleActions';
import '@/styles/blog.css';

type Props = { params: Promise<{ slug: string }> };
export function generateStaticParams() { return getAllPosts().map(({ slug }) => ({ slug })); }
export const dynamicParams = false;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const post = getPost((await params).slug);
  if (!post) notFound();
  return {
    title: post.title, description: post.description,
    authors: [{ name: post.author }], alternates: { canonical: `/blog/${post.slug}` },
    openGraph: { type: 'article', title: post.title, description: post.description, url: `/blog/${post.slug}`, publishedTime: post.date, modifiedTime: post.updated ?? post.date, authors: [post.author], images: [{ url: post.image, width: 1200, height: 630, alt: post.title }] },
    twitter: { card: 'summary_large_image', title: post.title, description: post.description, images: [post.image] },
  };
}

export default async function ArticlePage({ params }: Props) {
  const post = getPost((await params).slug);
  if (!post) notFound();
  const url = `${SITE_URL}/blog/${post.slug}`;
  const buildNote = post.category === 'Build notes';
  const openSource = post.diagram === 'open-source';
  const schema = { '@context': 'https://schema.org', '@type': 'BlogPosting', headline: post.title, description: post.description, image: `${SITE_URL}${post.image}`, datePublished: post.date, dateModified: post.updated ?? post.date, author: { '@type': 'Person', name: post.author }, publisher: { '@type': 'Organization', name: 'Potluck AI' }, mainEntityOfPage: url };
  return (
    <article className="site-page blog-article">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(schema).replace(/</g, '\\u003c') }} />
      <nav className="article-breadcrumb" aria-label="Breadcrumb"><Link href="/blog">Blog</Link><span aria-hidden="true">/</span><span>{post.category}</span></nav>
      <header className="article-intro">
        <h1>{post.title}</h1>
        <p className="article-meta">By {post.author}<span>·</span><time dateTime={post.date}>{formatDate(post.date)}</time><span>·</span>{post.readingMinutes} min read</p>
        {post.updated && <p className="article-meta">Updated <time dateTime={post.updated}>{formatDate(post.updated)}</time></p>}
      </header>
      <div className={`article-diagram${openSource ? ' article-diagram-source' : ''}`}><BlogArtwork kind={post.diagram} /></div>
      <div className="article-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
          a: ({ href, children }) => href?.startsWith('/') ? <Link href={href}>{children}</Link> : <a href={href}>{children}</a>,
          table: ({ children }) => <div className="article-table" role="region" aria-label="Article reference table" tabIndex={0}><table>{children}</table></div>,
        }}>{post.content}</ReactMarkdown>
      </div>
      <footer className="article-next">
        <h2>{openSource ? 'Build with us.' : buildNote ? 'Follow the model-splitting work.' : 'Try it on your own machines.'}</h2>
        <p>{openSource ? 'Read the source, try it on your hardware, or find a place to contribute.' : buildNote ? 'This test used development builds. Read how splitting works and what is available to try.' : 'The install page has the downloads, requirements, and platform notes.'}</p>
        <ArticleActions url={url} variant={openSource ? 'open-source' : buildNote ? 'build-notes' : 'download'} />
      </footer>
    </article>
  );
}

import type { Metadata } from 'next';
import { getAllPosts } from '@/lib/blog';
import { PageIntro } from '@/components/PageIntro';
import { BlogPreview } from '@/components/BlogPreview';
import '@/styles/blog.css';

export const metadata: Metadata = {
  title: 'Blog',
  description: 'Build notes, practical local AI guides, and updates from the open-source Potluck project.',
  alternates: { canonical: '/blog' },
};

export default function BlogPage() {
  const posts = getAllPosts();
  return (
    <div className="site-page blog-index">
      <PageIntro title="The Potluck blog">What we're building, what we've tested, and what we're learning from running AI on our own hardware.</PageIntro>
      {posts.length ? posts.map((post) => <BlogPreview key={post.slug} post={post} />) : <p>Our first guide is on the way. In the meantime, <a className="text-link" href="/how-it-works">see how Potluck works</a>.</p>}
    </div>
  );
}

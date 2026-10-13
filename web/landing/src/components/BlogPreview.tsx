import Link from 'next/link';
import { type BlogPost, formatDate } from '@/lib/blog';
import { Arrow } from './Arrow';
import { BlogArtwork } from './BlogArtwork';

export function BlogPreview({ post }: { post: BlogPost }) {
  return (
    <Link className="blog-feature" href={`/blog/${post.slug}`}>
      <div className="blog-feature-art"><BlogArtwork kind={post.diagram} /></div>
      <div className="blog-feature-copy">
        <h2>{post.title}</h2>
        <p className="article-meta">{post.category} <span>·</span> {post.readingMinutes} min read</p>
        <p>{post.description}</p>
        <p className="article-meta">{post.author} <span>·</span> <time dateTime={post.date}>{formatDate(post.date)}</time></p>
        <span className="text-action">Read the article <Arrow /></span>
      </div>
    </Link>
  );
}

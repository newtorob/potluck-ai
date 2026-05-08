/**
 * MessageBubble — renders a single chat message with markdown support.
 *
 * User messages: right-aligned, primary-tinted bubble.
 * Assistant messages: left-aligned, card bubble with markdown + code highlighting.
 * Error messages: left-aligned, destructive-tinted bubble with "Load a model" CTA.
 * Streaming: editorial caret (▎) with caret-blink while status === 'streaming'.
 * Hover: copy-to-clipboard button.
 *
 * Editorial reading flow:
 *   - Assistant prose body uses 15px / leading-relaxed for comfort
 *   - User bubble keeps tight rhythm — short messages typical
 *   - Code blocks get a slightly raised muted surface, not the content bg
 *   - Inline code uses font-mono at 0.92em so the line height stays even
 */

import { useState } from 'react';
import { Check, Copy, AlertCircle } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { cn } from '@/components/ui/cn';
import type { AppMessage } from '@/stores/chatStore';

interface MessageBubbleProps {
  message: AppMessage;
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user';
  const isStreaming = message.status === 'streaming';
  const isError = message.status === 'error';

  return (
    <article
      aria-label={`${message.role} message`}
      className={cn(
        'flex w-full animate-fade-in-up',
        isUser ? 'justify-end' : 'justify-start',
      )}
    >
      <div
        className={cn(
          'group relative max-w-[80%] rounded-2xl text-[15px] leading-relaxed',
          // User: ember tint, asymmetric tight corner
          isUser && 'bg-primary text-primary-foreground rounded-br-sm px-4 py-2.5 shadow-hearth-sm',
          // Assistant: softer card, hairline border, asymmetric tight corner
          !isUser && !isError &&
            'bg-card text-card-foreground border border-border/60 rounded-bl-sm px-5 py-3.5',
          // Error: destructive-tinted; same shape as assistant
          isError &&
            'bg-destructive/8 text-destructive border border-destructive/30 rounded-bl-sm px-5 py-3.5',
        )}
      >
        {isError ? (
          <ErrorContent message={message} />
        ) : isUser ? (
          <PlainContent content={message.content} />
        ) : (
          <MarkdownContent content={message.content} isStreaming={isStreaming} />
        )}

        {/* Copy button — visible on hover for non-empty messages */}
        {message.content.length > 0 && !isError && (
          <CopyButton text={message.content} isUser={isUser} />
        )}
      </div>
    </article>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function PlainContent({ content }: { content: string }) {
  return (
    <p className="whitespace-pre-wrap break-words leading-relaxed">
      {content}
    </p>
  );
}

function MarkdownContent({ content, isStreaming }: { content: string; isStreaming: boolean }) {
  return (
    <div
      className={cn(
        'prose prose-sm dark:prose-invert max-w-none break-words',
        // Editorial overrides for the assistant reading surface
        'prose-p:my-2 prose-p:leading-relaxed',
        'prose-headings:font-serif prose-headings:tracking-display prose-headings:mt-5 prose-headings:mb-2',
        'prose-strong:text-foreground prose-strong:font-semibold',
        'prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5',
        'prose-blockquote:border-l-2 prose-blockquote:border-primary/40 prose-blockquote:not-italic prose-blockquote:text-muted-foreground',
        'prose-a:text-primary prose-a:no-underline hover:prose-a:underline',
        'prose-pre:p-0 prose-pre:bg-transparent',
        'prose-code:before:content-none prose-code:after:content-none',
      )}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          // Open external links in the system browser
          a: ({ href, children, node: _node, ...props }) => (
            <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
              {children}
            </a>
          ),
          // Inline vs block code: language class signals fenced block
          code: ({ className, children, node: _node, ...props }) => {
            const isBlock = typeof className === 'string' && className.includes('language-');
            if (isBlock) {
              return (
                <code className={className} {...props}>
                  {children}
                </code>
              );
            }
            return (
              <code
                className={cn(
                  'rounded bg-muted/80 px-1.5 py-0.5 font-mono text-[0.85em]',
                  'text-foreground/90 border border-border/40',
                )}
                {...props}
              >
                {children}
              </code>
            );
          },
          // Wrap pre/code blocks for horizontal scroll + warm surface
          pre: ({ children, node: _node, ...props }) => (
            <pre
              className={cn(
                'my-3 overflow-x-auto rounded-lg border border-border/40',
                'bg-muted/50 p-3.5 text-[13px] leading-relaxed',
              )}
              {...props}
            >
              {children}
            </pre>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
      {isStreaming && (
        <span
          aria-hidden="true"
          className="ml-0.5 inline-block animate-caret-blink text-primary"
        >
          ▎
        </span>
      )}
    </div>
  );
}

function ErrorContent({ message }: { message: AppMessage }) {
  const navigate = useNavigate();
  const isNoModelError =
    message.errorText?.toLowerCase().includes('no model') === true ||
    message.errorText?.toLowerCase().includes('503') === true ||
    message.errorText?.toLowerCase().includes('not loaded') === true;

  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-start gap-2">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <p className="leading-snug">
          {message.errorText ?? 'An error occurred. Please try again.'}
        </p>
      </div>
      {isNoModelError && (
        <button
          type="button"
          onClick={() => navigate('/models')}
          className={cn(
            'self-start rounded-md border border-destructive/40 bg-destructive/5 px-3 py-1 text-xs font-medium',
            'hover:bg-destructive/10 transition-colors duration-fast',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
        >
          Load a model
        </button>
      )}
    </div>
  );
}

function CopyButton({ text, isUser }: { text: string; isUser: boolean }) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard API not available — silently ignore
    }
  }

  return (
    <button
      type="button"
      onClick={() => void handleCopy()}
      aria-label={copied ? 'Copied' : 'Copy message'}
      title={copied ? 'Copied' : 'Copy'}
      className={cn(
        'absolute -top-2 right-2 flex h-6 w-6 items-center justify-center rounded-md',
        'border border-border bg-popover text-muted-foreground shadow-hearth-sm',
        'opacity-0 transition-opacity duration-fast group-hover:opacity-100',
        'hover:text-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        isUser && 'right-2',
      )}
    >
      {copied ? (
        <Check className="h-3 w-3 text-sage" aria-hidden="true" />
      ) : (
        <Copy className="h-3 w-3" aria-hidden="true" />
      )}
    </button>
  );
}

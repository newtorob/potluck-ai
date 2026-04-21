/**
 * MessageBubble — renders a single chat message with markdown support.
 *
 * User messages: right-aligned, primary-tinted bubble.
 * Assistant messages: left-aligned, card bubble with markdown + code highlighting.
 * Error messages: left-aligned, destructive-tinted bubble with "Load a model" CTA.
 * Streaming: blinking cursor (▎) appended while status === 'streaming'.
 * Hover: copy-to-clipboard button.
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
      className={cn('flex w-full', isUser ? 'justify-end' : 'justify-start')}
    >
      <div
        className={cn(
          'group relative max-w-[80%] rounded-2xl px-4 py-3 text-sm',
          isUser && 'bg-primary text-primary-foreground rounded-br-sm',
          !isUser && !isError && 'bg-card text-card-foreground border border-border rounded-bl-sm',
          isError && 'bg-destructive/10 text-destructive border border-destructive/30 rounded-bl-sm',
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
  return <p className="whitespace-pre-wrap break-words leading-relaxed">{content}</p>;
}

function MarkdownContent({ content, isStreaming }: { content: string; isStreaming: boolean }) {
  return (
    <div className="prose prose-sm dark:prose-invert max-w-none break-words">
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
          // Distinguish inline vs block code by the presence of a language class
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
                className="rounded bg-muted px-1 py-0.5 font-mono text-xs"
                {...props}
              >
                {children}
              </code>
            );
          },
          // Wrap pre/code blocks for horizontal scroll
          pre: ({ children, node: _node, ...props }) => (
            <pre
              className="overflow-x-auto rounded-lg bg-muted/60 p-3 text-xs"
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
          className="ml-0.5 inline-block animate-pulse text-muted-foreground"
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
    <div className="flex flex-col gap-2">
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
            'self-start rounded-md border border-destructive/40 px-3 py-1 text-xs font-medium',
            'hover:bg-destructive/10 transition-colors',
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
      aria-label="Copy message"
      className={cn(
        'absolute -top-2 right-2 flex h-6 w-6 items-center justify-center rounded-md',
        'border border-border bg-popover text-muted-foreground shadow-sm',
        'opacity-0 transition-opacity group-hover:opacity-100',
        'hover:text-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        isUser && 'right-2',
      )}
    >
      {copied ? (
        <Check className="h-3 w-3 text-emerald-500" aria-hidden="true" />
      ) : (
        <Copy className="h-3 w-3" aria-hidden="true" />
      )}
    </button>
  );
}

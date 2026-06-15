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
import { Network, Cpu, Home } from 'lucide-react';
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
      data-testid={`chat-message-${message.role}`}
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

        {/* "What left my machine" receipt. Renders on every completed
            assistant message so silence is never the only signal that a
            prompt stayed home — the affirmative "stayed on this machine"
            line is the trust guarantee. Local messages carry no meta
            (the local /chat/completions path emits none), so an absent
            meta is itself the local case. Open-pool routing stays
            identity-blind (manifesto blinded-routing rule): never a peer
            name, only that it left the device. */}
        {!isUser && !isError && !isStreaming && message.content.length > 0 && (
          <RouteReceipt meta={message.meta} />
        )}
      </div>
    </article>
  );
}

/** Three honest privacy states for where a response was produced:
 *   local — nothing left this machine (the local sidecar; no meta emitted).
 *   mine  — left this box but only to your own hardware over the private mesh
 *           (a household peer, or the coordinator routing to one of your peers).
 *   pool  — left this machine to another member's machine in the open pool;
 *           identity-blind, so never a peer name. */
type RoutePrivacy = 'local' | 'mine' | 'pool';

function classifyRoute(meta: AppMessage['meta']): RoutePrivacy {
  if (!meta || meta.served_by === 'local') return 'local';
  if (
    meta.served_by === 'household' ||
    (meta.served_by === 'contributor' && meta.served_scope === 'mine')
  ) {
    return 'mine';
  }
  return 'pool';
}

function RouteReceipt({ meta }: { meta: AppMessage['meta'] }) {
  const route = classifyRoute(meta);

  const parts: string[] = [];
  if (meta && typeof meta.duration_ms === 'number' && meta.duration_ms > 0) {
    parts.push(meta.duration_ms >= 1000 ? `${(meta.duration_ms / 1000).toFixed(1)}s` : `${meta.duration_ms}ms`);
  }
  if (meta && typeof meta.output_tokens === 'number' && meta.output_tokens > 0) {
    parts.push(`${meta.output_tokens} tokens`);
  }

  let Icon = Cpu;
  let label = 'Stayed on this machine';
  let tone = 'text-sage';
  if (route === 'mine') {
    Icon = Home;
    label = meta?.served_peer ? `Ran on ${meta.served_peer}, your hardware` : 'Ran on your own hardware';
    tone = 'text-primary/70';
  } else if (route === 'pool') {
    Icon = Network;
    label = 'Left this machine, open pool';
    tone = 'text-primary/70';
  }

  return (
    <div
      className="mt-3 flex items-center gap-1.5 border-t border-border/40 pt-2 text-[11px] text-muted-foreground/80"
      aria-label="Privacy receipt"
    >
      <Icon className={cn('h-3 w-3', tone)} aria-hidden="true" />
      <span className="font-serif italic">{label}</span>
      {parts.length > 0 && (
        <>
          <span aria-hidden="true">·</span>
          <span className="tabular-nums">{parts.join(' · ')}</span>
        </>
      )}
    </div>
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

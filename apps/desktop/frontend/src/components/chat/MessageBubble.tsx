/**
 * MessageBubble — renders a single chat message with markdown support.
 *
 * User messages: right-aligned, primary-tinted bubble.
 * Assistant messages: left-aligned, card bubble with markdown + code highlighting.
 * Error messages: left-aligned, destructive-tinted bubble with a recovery CTA
 *   matched to the failure (see lib/chatErrorRecovery).
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
import { SafeExternalLink } from '@/components/ui/SafeExternalLink';
import type { AppMessage } from '@/stores/chatStore';
import { classifyChatError } from '@/lib/chatErrorRecovery';

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

        {/* Copy button — visible on hover for non-empty messages. An errored
            reply keeps whatever streamed in before it failed, and that text is
            as worth copying as a finished one. */}
        {message.content.length > 0 && <CopyButton text={message.content} isUser={isUser} />}

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

        {/* A reply the user stopped, or one hydrated from history as partial,
            must not read as a finished answer. */}
        {!isUser && message.status === 'partial' && (
          <p className="mt-2 text-[11px] italic text-muted-foreground/80">Stopped early</p>
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

// ---------------------------------------------------------------------------
// Receipt content per route — exact copy from spec, no embellishment.
// ---------------------------------------------------------------------------

interface ReceiptContent {
  title: string;
  body: string;
}

function buildReceiptContent(meta: AppMessage['meta']): ReceiptContent {
  if (!meta || meta.served_by === 'local') {
    return {
      title: 'Stayed on this machine',
      body: 'Your prompt, the reply, and the model all ran on this device. Nothing left your machine.',
    };
  }

  if (meta.served_by === 'household') {
    const peerSuffix = meta.served_peer ? ` ${meta.served_peer}` : '';
    return {
      title: `Ran on your machine${peerSuffix}`,
      body: 'Sent encrypted over your private mesh to one of your own machines, and nowhere else. It never touched the open network, and no account or identity was attached.',
    };
  }

  if (meta.served_by === 'contributor' && meta.served_scope === 'mine') {
    return {
      title: 'Ran on one of your machines',
      body: 'Routed through the coordinator to one of your own machines. It stayed within your machines and did not go to the open pool.',
    };
  }

  // contributor + pool (or contributor with no scope)
  return {
    title: 'Answered by the open pool',
    body: 'A verified contributor\'s machine answered this. It processed your prompt to generate the reply, so that machine saw your request text. The request carried no account or identity, so they do not know it came from you, and it traveled over the encrypted mesh.',
  };
}

function RouteReceipt({ meta }: { meta: AppMessage['meta'] }) {
  const [expanded, setExpanded] = useState(false);
  const route = classifyRoute(meta);
  const { title, body } = buildReceiptContent(meta);

  // Collapsed badge: icon + short label + optional stats
  const parts: string[] = [];
  if (meta && typeof meta.duration_ms === 'number' && meta.duration_ms > 0) {
    parts.push(meta.duration_ms >= 1000 ? `${(meta.duration_ms / 1000).toFixed(1)}s` : `${meta.duration_ms}ms`);
  }
  if (meta && typeof meta.output_tokens === 'number' && meta.output_tokens > 0) {
    parts.push(`${meta.output_tokens} tok`);
  }

  let Icon = Cpu;
  let badgeTone = 'text-sage';
  // Collapsed label mirrors the receipt title but stays short enough for the badge row.
  let badgeLabel = 'This machine';
  if (route === 'mine') {
    Icon = Home;
    badgeTone = 'text-primary/70';
    badgeLabel = meta?.served_peer ? `Your machine · ${meta.served_peer}` : 'Your hardware';
  } else if (route === 'pool') {
    Icon = Network;
    badgeTone = 'text-muted-foreground/70';
    badgeLabel = 'Open pool';
  }

  // Expanded panel tone: sage for local/mine (reassuring), muted for pool (honest, not alarming).
  const panelTone =
    route === 'pool'
      ? 'border-border/50 bg-muted/30'
      : 'border-sage/25 bg-sage/5';

  const receiptId = `receipt-body-${Math.random().toString(36).slice(2, 9)}`;

  return (
    <div className="mt-3 border-t border-border/40 pt-2">
      {/* Collapsed badge row — always visible */}
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        aria-controls={receiptId}
        className={cn(
          'flex w-full items-center gap-1.5 text-[11px] text-muted-foreground/80',
          'rounded-sm transition-colors duration-fast',
          'hover:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
          'cursor-pointer select-none',
        )}
      >
        <Icon className={cn('h-3 w-3 shrink-0', badgeTone)} aria-hidden="true" />
        <span className="font-serif italic">{badgeLabel}</span>
        {parts.length > 0 && (
          <>
            <span aria-hidden="true" className="text-muted-foreground/40">·</span>
            <span className="tabular-nums">{parts.join(' · ')}</span>
          </>
        )}
        {/* Chevron rotates on expand */}
        <svg
          className={cn(
            'ml-auto h-2.5 w-2.5 shrink-0 text-muted-foreground/40 transition-transform duration-base',
            expanded && 'rotate-180',
          )}
          viewBox="0 0 10 10"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          aria-hidden="true"
        >
          <path d="M2 3.5 5 6.5 8 3.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {/* Expanded receipt panel */}
      {expanded && (
        <div
          id={receiptId}
          role="region"
          aria-label="Privacy receipt detail"
          className={cn(
            'mt-2 rounded-md border px-3 py-2.5 text-[11.5px] leading-relaxed',
            panelTone,
          )}
        >
          <p className="mb-1 font-medium text-foreground/80">{title}</p>
          <p className="text-muted-foreground">{body}</p>

          {/* Stats detail line — only when values exist */}
          {(parts.length > 0 || (meta?.finish_reason !== undefined && meta.finish_reason !== null)) && (
            <p className="mt-2 flex flex-wrap gap-x-3 gap-y-0.5 tabular-nums text-[10.5px] text-muted-foreground/60">
              {typeof meta?.output_tokens === 'number' && meta.output_tokens > 0 && (
                <span>{meta.output_tokens} tokens</span>
              )}
              {typeof meta?.duration_ms === 'number' && meta.duration_ms > 0 && (
                <span>
                  {meta.duration_ms >= 1000
                    ? `${(meta.duration_ms / 1000).toFixed(1)}s`
                    : `${meta.duration_ms}ms`}
                </span>
              )}
              {meta?.finish_reason !== undefined && meta.finish_reason !== null && (
                <span>{meta.finish_reason}</span>
              )}
            </p>
          )}
        </div>
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
          // Assistant replies can carry model-generated or pool-routed
          // markdown — untrusted the same way memory content is. Only
          // fully-qualified http(s) links are clickable, and they only ever
          // open via window.open()/setWindowOpenHandler, never an in-window
          // navigation. See SafeExternalLink.
          a: SafeExternalLink,
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
  // errorText can arrive from persisted history or from a lookup that missed,
  // so it is only trusted once it is actually a string. Anything else is
  // treated as absent rather than allowed to throw on .toLowerCase().
  const errorText = typeof message.errorText === 'string' ? message.errorText : null;
  // Not every 503 is a missing model: routing failures share the status code,
  // and sending someone to the model library cannot fix a peer that dropped.
  const recovery = classifyChatError(errorText);
  // A reply can fail after tokens have already been read. Those tokens are the
  // answer the user has; the error line explains why it stops there.
  const hasPartialAnswer = message.content.length > 0;

  return (
    <div className="flex flex-col gap-2.5">
      {hasPartialAnswer && (
        <div className="text-card-foreground">
          <MarkdownContent content={message.content} isStreaming={false} />
        </div>
      )}
      <div
        className={cn(
          'flex items-start gap-2',
          hasPartialAnswer && 'border-t border-destructive/25 pt-2.5',
        )}
      >
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <p className="leading-snug">
          {errorText ?? 'This reply did not finish.'}
        </p>
      </div>
      {recovery.action && (
        <button
          type="button"
          onClick={() => navigate(recovery.route)}
          className={cn(
            'self-start rounded-md border border-destructive/40 bg-destructive/5 px-3 py-1 text-xs font-medium',
            'hover:bg-destructive/10 transition-colors duration-fast',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
        >
          {recovery.label}
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

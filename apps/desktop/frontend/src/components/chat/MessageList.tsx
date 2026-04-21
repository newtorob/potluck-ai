/**
 * MessageList — scrollable conversation thread with smart auto-scroll.
 *
 * Auto-scrolls to bottom when new tokens arrive, but only if the user
 * hasn't manually scrolled up. A "New messages ↓" pill appears when the
 * user is scrolled up while streaming is active.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowDown } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { MessageBubble } from './MessageBubble';
import type { AppMessage } from '@/stores/chatStore';

interface MessageListProps {
  messages: AppMessage[];
  isStreaming: boolean;
}

/** Pixel distance from bottom before we consider the user "at the bottom". */
const SCROLL_THRESHOLD = 80;

export function MessageList({ messages, isStreaming }: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const [userScrolledUp, setUserScrolledUp] = useState(false);
  // Track the id of the first message to detect conversation switches
  const firstMessageId = messages[0]?.id ?? null;

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    bottomRef.current?.scrollIntoView({ behavior, block: 'end' });
  }, []);

  const isAtBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_THRESHOLD;
  }, []);

  // Detect manual scroll
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    const onScroll = () => {
      setUserScrolledUp(!isAtBottom());
    };

    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, [isAtBottom]);

  // Auto-scroll when new content arrives (only if user hasn't scrolled up)
  useEffect(() => {
    if (!userScrolledUp) {
      scrollToBottom('smooth');
    }
  }, [messages, userScrolledUp, scrollToBottom]);

  // Snap to bottom when conversation switches (first message id changes)
  useEffect(() => {
    scrollToBottom('instant');
    setUserScrolledUp(false);
  }, [firstMessageId, scrollToBottom]);

  return (
    <div className="relative flex-1 overflow-hidden">
      <div
        ref={scrollRef}
        className="h-full overflow-y-auto px-4 py-6 sm:px-6"
      >
        <div className="mx-auto max-w-3xl flex flex-col gap-4">
          {messages.map((message) => (
            <MessageBubble key={message.id} message={message} />
          ))}
          <div ref={bottomRef} className="h-1" aria-hidden="true" />
        </div>
      </div>

      {/* "New messages" pill */}
      {userScrolledUp && isStreaming && (
        <div className="absolute bottom-4 left-1/2 -translate-x-1/2">
          <button
            type="button"
            onClick={() => {
              setUserScrolledUp(false);
              scrollToBottom('smooth');
            }}
            aria-label="Scroll to new messages"
            className={cn(
              'flex items-center gap-1.5 rounded-full border border-border bg-popover',
              'px-3 py-1.5 text-xs font-medium text-foreground shadow-lg',
              'hover:bg-accent transition-colors',
            )}
          >
            <ArrowDown className="h-3 w-3" aria-hidden="true" />
            New messages
          </button>
        </div>
      )}
    </div>
  );
}

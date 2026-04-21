import { MessageSquare } from 'lucide-react';

/**
 * Chat view — top-level route for /chat.
 *
 * Placeholder scaffold. Real implementation per guide §4.5:
 *   - MessageList: scrollable conversation history
 *   - MessageBubble: per-message markdown rendering
 *   - Composer: textarea with shift+enter, send, stop-generation
 *   - ModelSelector: dropdown bound to modelStore.activeModelId
 *   - useChat hook drives streaming via localApi.streamChat + SSE parser
 */
export function ChatView() {
  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-border px-6 py-4">
        <MessageSquare className="h-5 w-5 text-muted-foreground" />
        <h1 className="text-sm font-semibold">Chat</h1>
        {/* TODO: ModelSelector goes here */}
      </div>

      {/* Message list skeleton */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        {/* Skeleton rows simulate a conversation while real UI is built */}
        {[80, 60, 90, 50].map((width, i) => (
          <div
            key={i}
            className={`flex ${i % 2 === 0 ? 'justify-start' : 'justify-end'}`}
          >
            <div
              className="h-9 animate-pulse rounded-xl bg-muted"
              style={{ width: `${width}%`, maxWidth: '480px' }}
            />
          </div>
        ))}
      </div>

      {/* Empty state — visible when no conversation is active */}
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 pointer-events-none">
        <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10">
          <MessageSquare className="h-7 w-7 text-primary" />
        </div>
        <p className="text-base font-medium text-foreground">Chat coming soon</p>
        <p className="text-sm text-muted-foreground">
          Streaming chat with local LLMs — guide §4.5
        </p>
      </div>

      {/* Composer placeholder */}
      <div className="border-t border-border px-6 py-4">
        <div className="flex items-center gap-3 rounded-xl border border-input bg-card px-4 py-3">
          <div className="h-4 w-full animate-pulse rounded bg-muted" />
        </div>
      </div>
    </div>
  );
}

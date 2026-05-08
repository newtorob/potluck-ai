/**
 * Composer — auto-growing textarea with send/stop controls.
 *
 * - Enter → send; Shift+Enter → newline; Cmd/Ctrl+Enter → send
 * - Esc → abort streaming
 * - Textarea grows up to ~12 lines then scrolls
 * - Send button disabled when empty or no model is loaded
 * - During streaming: send morphs into a stop button
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react';
import { Send, Square } from 'lucide-react';
import { cn } from '@/components/ui/cn';

interface ComposerProps {
  onSend: (content: string) => void;
  onStop: () => void;
  isStreaming: boolean;
  /** Disable sending when no model is loaded */
  modelLoaded: boolean;
}

/** Approximate max height for ~12 lines at 20px line-height */
const MAX_HEIGHT_PX = 240;

export function Composer({ onSend, onStop, isStreaming, modelLoaded }: ComposerProps) {
  const [value, setValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT_PX)}px`;
  }, [value]);

  // Re-focus after streaming ends
  useEffect(() => {
    if (!isStreaming) {
      textareaRef.current?.focus();
    }
  }, [isStreaming]);

  const submit = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || isStreaming || !modelLoaded) return;
    onSend(trimmed);
    setValue('');
    // Reset height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  }, [value, isStreaming, modelLoaded, onSend]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Escape' && isStreaming) {
        e.preventDefault();
        onStop();
        return;
      }

      if (e.key === 'Enter') {
        const isMeta = e.metaKey || e.ctrlKey;
        if (isMeta || !e.shiftKey) {
          e.preventDefault();
          submit();
        }
        // Shift+Enter falls through and inserts a newline naturally
      }
    },
    [isStreaming, onStop, submit],
  );

  const canSend = value.trim().length > 0 && modelLoaded && !isStreaming;
  const placeholder = !modelLoaded
    ? 'Load a model to start chatting…'
    : isStreaming
    ? 'Generating…'
    : 'Message (Enter to send, Shift+Enter for newline)';

  return (
    {/* Slight elevation off pure background — card/40 reads as "lifted
        but not loud" against the warm-charcoal canvas, anchoring the
        composer to the page instead of floating in a void. */}
    <div className="border-t border-border bg-card/40 px-4 py-3 sm:px-6">
      <div
        className={cn(
          'mx-auto flex max-w-3xl items-end gap-2 rounded-xl border border-input bg-card',
          'px-3 py-2 shadow-sm transition-shadow',
          'focus-within:ring-2 focus-within:ring-ring',
        )}
      >
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={1}
          disabled={isStreaming || !modelLoaded}
          placeholder={placeholder}
          aria-label="Chat message"
          aria-multiline="true"
          className={cn(
            'flex-1 resize-none bg-transparent text-sm text-foreground',
            'placeholder:text-muted-foreground',
            'focus:outline-none',
            'disabled:opacity-50 disabled:cursor-not-allowed',
            'overflow-y-auto',
          )}
          style={{ maxHeight: MAX_HEIGHT_PX }}
        />

        {isStreaming ? (
          <button
            type="button"
            onClick={onStop}
            aria-label="Stop generation"
            className={cn(
              'flex h-8 w-8 shrink-0 items-center justify-center rounded-lg',
              'bg-destructive text-destructive-foreground',
              'hover:bg-destructive/90 transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            <Square className="h-3.5 w-3.5 fill-current" aria-hidden="true" />
          </button>
        ) : (
          <button
            type="button"
            onClick={submit}
            disabled={!canSend}
            aria-label="Send message"
            className={cn(
              'flex h-8 w-8 shrink-0 items-center justify-center rounded-lg transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              canSend
                ? 'bg-primary text-primary-foreground hover:bg-primary/90'
                : 'bg-muted text-muted-foreground cursor-not-allowed',
            )}
          >
            <Send className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        )}
      </div>

      <p className="mx-auto mt-1.5 max-w-3xl text-center text-[11px] text-muted-foreground/60">
        Runs locally — your messages never leave this device.
      </p>
    </div>
  );
}

/**
 * Composer — auto-growing textarea with send/stop controls.
 *
 * - Enter → send; Shift+Enter → newline; Cmd/Ctrl+Enter → also sends
 * - Esc → abort streaming
 * - Textarea grows up to ~12 lines then scrolls
 * - Send button disabled when empty or no model is loaded
 * - During streaming: send morphs into a stop button
 * - Slash-key (/) focus shortcut from anywhere on the chat view
 *
 * Hearth chrome: lifted card surface, ember focus ring on focus-within,
 * subdued helper hint with keyboard glyphs. The composer is the most-
 * touched control in the app — it earns its visual weight.
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
import type { RoutingScope } from '@/stores/chatStore';

interface ComposerProps {
  onSend: (content: string) => void;
  onStop: () => void;
  isStreaming: boolean;
  /** Disable sending when no inference path is available (no local model AND
   *  no remote scope active). */
  modelLoaded: boolean;
  /** Active routing scope — drives the privacy-honesty footer copy. */
  routingScope: RoutingScope;
}

/** Approximate max height for ~12 lines at 20px line-height */
const MAX_HEIGHT_PX = 240;

export function Composer({ onSend, onStop, isStreaming, modelLoaded, routingScope }: ComposerProps) {
  const [value, setValue] = useState('');
  const [isFocused, setIsFocused] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT_PX)}px`;
  }, [value]);

  // Re-focus after streaming ends so the user can type the next prompt
  useEffect(() => {
    if (!isStreaming) {
      textareaRef.current?.focus();
    }
  }, [isStreaming]);

  // Slash-key focus shortcut (skip if user is already typing into a field)
  useEffect(() => {
    function handler(e: globalThis.KeyboardEvent) {
      if (e.key !== '/') return;
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) {
        return;
      }
      if (isStreaming || !modelLoaded) return;
      e.preventDefault();
      textareaRef.current?.focus();
    }
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [isStreaming, modelLoaded]);

  const submit = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || isStreaming || !modelLoaded) return;
    onSend(trimmed);
    setValue('');
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
      : 'Ask anything. Press / to focus, Enter to send, Shift+Enter for a newline.';

  return (
    <div className="border-t border-border bg-card/40 px-4 py-3 sm:px-6">
      {/* Lifted card surface — anchors the composer to the page instead
          of floating in a void. focus-within drives the ember ring. */}
      <div
        className={cn(
          'mx-auto flex max-w-3xl items-end gap-2 rounded-xl border border-input bg-card',
          'px-3 py-2.5 transition-shadow duration-base',
          isFocused
            ? 'shadow-hearth border-primary/30'
            : 'shadow-hearth-sm hover:border-border',
        )}
      >
        <textarea
          ref={textareaRef}
          data-testid="chat-composer-input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onFocus={() => setIsFocused(true)}
          onBlur={() => setIsFocused(false)}
          rows={1}
          disabled={isStreaming || !modelLoaded}
          placeholder={placeholder}
          aria-label="Chat message"
          aria-multiline="true"
          className={cn(
            'flex-1 resize-none bg-transparent text-[15px] leading-6 text-foreground',
            'placeholder:text-muted-foreground/70',
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
            aria-label="Stop generation (Esc)"
            title="Stop (Esc)"
            className={cn(
              'flex h-9 w-9 shrink-0 items-center justify-center rounded-lg',
              'bg-destructive text-destructive-foreground',
              'hover:bg-destructive/90 transition-colors duration-fast',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card',
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
            title="Send (Enter)"
            className={cn(
              'flex h-9 w-9 shrink-0 items-center justify-center rounded-lg transition-all duration-fast',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card',
              canSend
                ? 'bg-primary text-primary-foreground hover:bg-primary/90 active:scale-95 shadow-hearth-sm'
                : 'bg-muted text-muted-foreground cursor-not-allowed',
            )}
          >
            <Send className="h-4 w-4" aria-hidden="true" />
          </button>
        )}
      </div>

      <ComposerHint
        isStreaming={isStreaming}
        modelLoaded={modelLoaded}
        routingScope={routingScope}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// ComposerHint — small line of editorial copy under the composer
// ---------------------------------------------------------------------------

interface ComposerHintProps {
  isStreaming: boolean;
  modelLoaded: boolean;
  routingScope: RoutingScope;
}

function ComposerHint({ isStreaming, modelLoaded, routingScope }: ComposerHintProps) {
  // The hint switches voice depending on routing scope. The privacy line
  // accurately describes where the prompt actually goes — never overclaims.
  // When no inference path is ready, fall back to a "pick a model" nudge.
  let body: React.ReactNode;
  if (!modelLoaded) {
    body = (
      <>
        Pick a model in <span className="font-medium text-muted-foreground">Models</span> and
        we're ready.
      </>
    );
  } else if (isStreaming) {
    body = (
      <>
        Press <Kbd>Esc</Kbd> to stop generating.
      </>
    );
  } else if (routingScope === 'household') {
    body = (
      <>
        Runs on one of your own machines over your private mesh, encrypted between your devices. Stays within your machines.
      </>
    );
  } else if (routingScope === 'circle') {
    body = (
      <>
        Runs on a machine in a group you chose and allowed. That machine sees your prompt, but it is one you picked.
      </>
    );
  } else if (routingScope === 'cooperative') {
    body = (
      <>
        Runs on any verified member's machine. The machine that answers sees your prompt to generate the reply, but the request carries no identity, so they do not know it came from you.
      </>
    );
  } else {
    body = (
      <>
        Runs on this machine. Nothing leaves your device.
      </>
    );
  }

  return (
    <p className="mx-auto mt-2 flex max-w-3xl items-center justify-center gap-1.5 text-[11px] text-muted-foreground/60">
      {body}
    </p>
  );
}

function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd
      className={cn(
        'inline-flex h-4 min-w-[1rem] items-center justify-center rounded border border-border/70',
        'bg-muted px-1 font-mono text-[10px] font-medium text-muted-foreground',
      )}
    >
      {children}
    </kbd>
  );
}

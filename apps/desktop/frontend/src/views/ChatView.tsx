/**
 * ChatView — main chat interface route (/chat).
 *
 * Layout:
 *   Top bar:   ModelSelector + loaded-model info (context, temp, max_tokens)
 *   Middle:    MessageList (auto-scroll, smart scroll-up detection)
 *   Bottom:    Composer (send/stop, keyboard shortcuts)
 *   Empty state: centered card with model name + 3 example-prompt pills
 */

import { type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { Thermometer, Hash, Layers } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { MessageList } from '@/components/chat/MessageList';
import { Composer } from '@/components/chat/Composer';
import { ModelSelector } from '@/components/chat/ModelSelector';
import { useChat } from '@/hooks/useChat';
import { useSettings } from '@/hooks/useSettings';
import { useHealth } from '@/hooks/useHealth';

// ---------------------------------------------------------------------------
// Example prompts for empty state
// ---------------------------------------------------------------------------

// First-impression prompts. Tone matters here — these are the moment a
// new user infers the app's character. Replaced the generic
// quantum/CSV/vegan trio with prompts that lean into the manifesto's
// voice (curiosity, craft, useful work, a touch of warmth).
const EXAMPLE_PROMPTS = [
  'What should I cook tonight with what’s already in my fridge?',
  'Help me draft an honest two-paragraph note to a friend I’ve been ignoring',
  'Walk me through how a co-op differs from a corporation, plainly',
];

// ---------------------------------------------------------------------------
// ChatView
// ---------------------------------------------------------------------------

export function ChatView() {
  const navigate = useNavigate();
  const {
    messages,
    isStreaming,
    send,
    abort,
  } = useChat();

  const { settings, isLoaded: settingsLoaded } = useSettings();
  const { modelLoaded, nCtxLoaded } = useHealth();

  const isEmpty = messages.length === 0;

  async function handleSend(content: string) {
    await send(content);
  }

  function goToHardwareSettings() {
    navigate('/settings#hardware');
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* ------------------------------------------------------------------ */}
      {/* Top bar                                                             */}
      {/* ------------------------------------------------------------------ */}
      <header className="flex shrink-0 flex-col border-b border-border bg-background px-4 py-2.5 sm:px-6">
        <div className="flex items-center gap-3">
          <ModelSelector disabled={isStreaming} />

          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            {/* Temp chip */}
            <InfoChip
              icon={<Thermometer className="h-3 w-3" />}
              aria-label="Edit temperature in settings"
              onClick={goToHardwareSettings}
            >
              {!settingsLoaded || !settings ? (
                <span className="inline-block h-3 w-6 animate-pulse rounded bg-muted" aria-hidden="true" />
              ) : (
                `Temp ${settings.runtime.temperature.toFixed(1)}`
              )}
            </InfoChip>

            {/* Max tokens chip */}
            <InfoChip
              icon={<Hash className="h-3 w-3" />}
              aria-label="Edit max output tokens in settings"
              onClick={goToHardwareSettings}
            >
              {!settingsLoaded || !settings ? (
                <span className="inline-block h-3 w-8 animate-pulse rounded bg-muted" aria-hidden="true" />
              ) : (
                `Max ${settings.runtime.max_tokens.toLocaleString()}`
              )}
            </InfoChip>

            {/* Context size chip */}
            <InfoChip
              icon={<Layers className="h-3 w-3" />}
              aria-label="Edit context size in settings"
              onClick={goToHardwareSettings}
            >
              {!settingsLoaded || !settings ? (
                <span className="inline-block h-3 w-8 animate-pulse rounded bg-muted" aria-hidden="true" />
              ) : nCtxLoaded != null ? (
                `Ctx ${nCtxLoaded.toLocaleString()}`
              ) : (
                <span
                  className="italic opacity-50"
                  title="Applies when a model is loaded"
                >
                  {`Ctx ${settings.runtime.context_size.toLocaleString()}`}
                </span>
              )}
            </InfoChip>
          </div>
        </div>
      </header>

      {/* ------------------------------------------------------------------ */}
      {/* Main content                                                        */}
      {/* ------------------------------------------------------------------ */}
      {isEmpty ? (
        <EmptyState
          modelLoaded={modelLoaded}
          onSelectPrompt={(p) => void handleSend(p)}
          onGoToModels={() => navigate('/models')}
        />
      ) : (
        <MessageList messages={messages} isStreaming={isStreaming} />
      )}

      {/* ------------------------------------------------------------------ */}
      {/* Composer                                                            */}
      {/* ------------------------------------------------------------------ */}
      <Composer
        onSend={(content) => void handleSend(content)}
        onStop={abort}
        isStreaming={isStreaming}
        modelLoaded={modelLoaded}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// InfoChip
// ---------------------------------------------------------------------------

interface InfoChipProps {
  icon: ReactNode;
  children: ReactNode;
  'aria-label': string;
  onClick: () => void;
}

function InfoChip({ icon, children, 'aria-label': ariaLabel, onClick }: InfoChipProps) {
  return (
    <button
      type="button"
      role="button"
      aria-label={ariaLabel}
      onClick={onClick}
      className={cn(
        'flex items-center gap-1',
        'rounded px-1.5 py-0.5 transition-colors',
        'cursor-pointer hover:bg-accent hover:text-accent-foreground',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
      )}
    >
      {icon}
      {children}
    </button>
  );
}

// ---------------------------------------------------------------------------
// EmptyState
// ---------------------------------------------------------------------------

interface EmptyStateProps {
  modelLoaded: boolean;
  onSelectPrompt: (prompt: string) => void;
  onGoToModels: () => void;
}

function EmptyState({ modelLoaded, onSelectPrompt, onGoToModels }: EmptyStateProps) {
  return (
    <div className="relative flex flex-1 flex-col items-center justify-center gap-8 px-4 grain">
      {/* Centered greeting. The serif heading is the moment the new
          aesthetic announces itself — Direction-B "warm + handcrafted"
          versus the previous default-shadcn cold sans heading. */}
      <div className="relative z-10 flex flex-col items-center gap-4 text-center">
        <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/12 ring-1 ring-primary/20">
          <span
            className="font-serif text-3xl leading-none text-primary select-none"
            style={{ transform: 'translateY(2px)' }}
            aria-hidden="true"
          >
            P
          </span>
        </div>
        <div className="max-w-md">
          <h2 className="font-serif text-3xl leading-tight tracking-display text-foreground">
            {modelLoaded ? 'What can I help you with?' : 'No model loaded yet'}
          </h2>
          <p className="mt-2 text-sm text-muted-foreground">
            {modelLoaded
              ? 'Ask anything. The conversation runs on your machine; nothing leaves it.'
              : 'Pick a model to load locally and we’ll be ready.'}
          </p>
        </div>
      </div>

      {!modelLoaded && (
        <button
          type="button"
          onClick={onGoToModels}
          className={cn(
            'relative z-10 rounded-lg bg-primary px-5 py-2.5 text-sm font-medium text-primary-foreground',
            'shadow-sm hover:bg-primary/90 transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          )}
        >
          Browse models
        </button>
      )}

      {modelLoaded && (
        <div className="relative z-10 flex w-full max-w-2xl flex-col gap-2">
          {EXAMPLE_PROMPTS.map((prompt) => (
            <button
              key={prompt}
              type="button"
              onClick={() => onSelectPrompt(prompt)}
              className={cn(
                'group flex items-center justify-between gap-3 rounded-xl border border-border/60 bg-card/60 px-4 py-3',
                'text-left text-[15px] text-foreground/90 transition-colors',
                'hover:border-primary/30 hover:bg-card hover:text-foreground',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              )}
            >
              <span>{prompt}</span>
              <span
                className="font-serif italic text-sm text-muted-foreground/70 group-hover:text-primary transition-colors"
                aria-hidden="true"
              >
                ask &rarr;
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

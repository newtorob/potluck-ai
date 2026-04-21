/**
 * ChatView — main chat interface route (/chat).
 *
 * Layout:
 *   Top bar:   ModelSelector + loaded-model info (context, temp, max_tokens)
 *   Middle:    MessageList (auto-scroll, smart scroll-up detection)
 *   Bottom:    Composer (send/stop, keyboard shortcuts)
 *   Empty state: centered card with model name + 3 example-prompt pills
 */

import { type ReactNode, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Thermometer, Hash, Layers } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { MessageList } from '@/components/chat/MessageList';
import { Composer } from '@/components/chat/Composer';
import { ModelSelector } from '@/components/chat/ModelSelector';
import { useChat } from '@/hooks/useChat';
import { localApi } from '@/lib/localApi';
import type { PotluckSettings } from '@/lib/localApi';

// ---------------------------------------------------------------------------
// Example prompts for empty state
// ---------------------------------------------------------------------------

const EXAMPLE_PROMPTS = [
  'Explain quantum entanglement in simple terms',
  'Write a Python function to parse a CSV file',
  'What are the pros and cons of a vegan diet?',
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

  const [settings, setSettings] = useState<PotluckSettings | null>(null);
  const [modelLoaded, setModelLoaded] = useState(false);

  // Fetch settings for the info row (temperature, max_tokens, context)
  useEffect(() => {
    let cancelled = false;
    void localApi.getSettings().then((s) => { if (!cancelled) setSettings(s); }).catch(() => {});
    return () => { cancelled = true; };
  }, []);

  // Track whether a model is currently loaded
  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const health = await localApi.health();
        if (!cancelled) setModelLoaded(health.model_loaded);
      } catch {
        if (!cancelled) setModelLoaded(false);
      }
    };
    void check();
    const interval = setInterval(() => void check(), 30_000);
    const onFocus = () => void check();
    window.addEventListener('focus', onFocus);
    return () => {
      cancelled = true;
      clearInterval(interval);
      window.removeEventListener('focus', onFocus);
    };
  }, []);

  const isEmpty = messages.length === 0;

  async function handleSend(content: string) {
    await send(content);
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* ------------------------------------------------------------------ */}
      {/* Top bar                                                             */}
      {/* ------------------------------------------------------------------ */}
      <header className="flex shrink-0 flex-col border-b border-border bg-background px-4 py-2.5 sm:px-6">
        <div className="flex items-center gap-3">
          <ModelSelector disabled={isStreaming} />

          {settings && (
            <div className="flex items-center gap-3 text-xs text-muted-foreground">
              <InfoChip
                icon={<Thermometer className="h-3 w-3" />}
                label={`Temp ${settings.runtime.temperature}`}
              />
              <InfoChip
                icon={<Hash className="h-3 w-3" />}
                label={`Max ${settings.runtime.max_tokens}`}
              />
              <InfoChip
                icon={<Layers className="h-3 w-3" />}
                label={`Ctx ${settings.runtime.context_size}`}
              />
            </div>
          )}
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

function InfoChip({ icon, label }: { icon: ReactNode; label: string }) {
  return (
    <span className="flex items-center gap-1">
      {icon}
      {label}
    </span>
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
    <div className="flex flex-1 flex-col items-center justify-center gap-6 px-4">
      <div className="flex flex-col items-center gap-3 text-center">
        <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10">
          <span className="text-2xl font-bold text-primary select-none">P</span>
        </div>
        <div>
          <h2 className="text-lg font-semibold text-foreground">
            {modelLoaded ? 'Start chatting' : 'No model loaded'}
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {modelLoaded
              ? 'Ask anything. Runs entirely on your device.'
              : 'Load a model to begin your conversation.'}
          </p>
        </div>
      </div>

      {!modelLoaded && (
        <button
          type="button"
          onClick={onGoToModels}
          className={cn(
            'rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground',
            'hover:bg-primary/90 transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
        >
          Browse models
        </button>
      )}

      {modelLoaded && (
        <div className="flex flex-wrap justify-center gap-2">
          {EXAMPLE_PROMPTS.map((prompt) => (
            <button
              key={prompt}
              type="button"
              onClick={() => onSelectPrompt(prompt)}
              className={cn(
                'rounded-full border border-border bg-card px-4 py-2 text-sm text-foreground',
                'hover:bg-accent hover:text-accent-foreground transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              )}
            >
              {prompt}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

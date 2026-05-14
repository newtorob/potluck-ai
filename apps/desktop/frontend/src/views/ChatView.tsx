/**
 * ChatView — main chat interface route (/chat).
 *
 * Layout:
 *   Top bar:   ModelSelector + loaded-model info (context, temp, max_tokens)
 *   Middle:    MessageList (auto-scroll, smart scroll-up detection)
 *   Bottom:    Composer (send/stop, keyboard shortcuts)
 *   Empty state: centered card with model name + 3 example-prompt pills
 */

import { type ReactNode, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Thermometer, Hash, Layers, Network, Cpu, Home } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { MessageList } from '@/components/chat/MessageList';
import { Composer } from '@/components/chat/Composer';
import { ModelSelector } from '@/components/chat/ModelSelector';
import { useChat } from '@/hooks/useChat';
import { useSettings } from '@/hooks/useSettings';
import { useHealth } from '@/hooks/useHealth';
import { useCoordinatorHealth } from '@/hooks/useCoordinatorHealth';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { useChatStore } from '@/stores/chatStore';
import { useMeshStore } from '@/stores/meshStore';
import { PotluckLogo } from '@/components/brand/PotluckLogo';

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

  // Network-mode routing toggle. When on, the latest user prompt routes
  // through the coordinator (POST /jobs/stream) to an eligible
  // contributor peer instead of the local sidecar's /chat/completions.
  const networkMode = useChatStore((s) => s.networkMode);
  const networkModelSlug = useChatStore((s) => s.networkModelSlug);
  const setNetworkMode = useChatStore((s) => s.setNetworkMode);

  // Household-mode routing toggle. When on, the prompt goes directly to a
  // peer in the user's household mesh (POST /peer/chat/completions over
  // the WireGuard IP). Distinct from networkMode: household = machines you
  // paired yourself, no coordinator routing, full conversation history
  // forwarded. Mutually exclusive with networkMode — chatStore enforces.
  const householdMode = useChatStore((s) => s.householdMode);
  const setHouseholdMode = useChatStore((s) => s.setHouseholdMode);

  // Household-peer availability — mesh store polls the sidecar; we gate
  // the toggle on having at least one reachable peer.
  const householdPeersReachable = useMeshStore(
    (s) => s.peers.some((p) => p.reachable),
  );
  // Start the mesh store on mount so the toggle has live data even when
  // the user hasn't visited /household yet. start() is idempotent.
  const meshStart = useMeshStore((s) => s.start);
  useEffect(() => {
    meshStart();
  }, [meshStart]);

  // Coordinator reachability — gates the Network toggle. If the user has
  // network mode on AND the coordinator drops, we auto-flip them back to
  // local rather than letting them hit send and get an opaque error.
  const { reachable: coordinatorReachable, lastCheckedAt } = useCoordinatorHealth();

  useEffect(() => {
    if (networkMode && !coordinatorReachable && lastCheckedAt !== null) {
      setNetworkMode(false);
    }
  }, [networkMode, coordinatorReachable, lastCheckedAt, setNetworkMode]);

  // Auto-clear household mode if no peers are reachable anymore.
  useEffect(() => {
    if (householdMode && !householdPeersReachable) {
      setHouseholdMode(false);
    }
  }, [householdMode, householdPeersReachable, setHouseholdMode]);

  // In network OR household mode the local model isn't required — a peer
  // hosts the weights. Send is gated on "any of the three is available."
  const sendReady = modelLoaded || networkMode || householdMode;

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
      {/* Top bar — unified ViewHeader with model selector + parameter chips */}
      {/* ------------------------------------------------------------------ */}
      <ViewHeader
        title="Chat"
        tools={
          <>
            <ModelSelector disabled={isStreaming || networkMode || householdMode} />

            {/* Routing-mode toggles. Three exclusive states:
                  - Local      (default): this machine's Python sidecar
                  - Household  : a peer in the user's household mesh
                  - Network    : a contributor peer via the coordinator
                Household + Network buttons are mutually exclusive — clicking
                one auto-clears the other (enforced in chatStore.set*Mode).
                Local is the implicit fallback when neither is active. */}

            {/* Household button — strict private routing to a paired machine.
                Only enabled when at least one household peer is reachable. */}
            <button
              type="button"
              onClick={() => setHouseholdMode(!householdMode)}
              disabled={isStreaming || !householdPeersReachable}
              aria-pressed={householdMode}
              aria-label={householdMode
                ? 'Household mode on — serving via one of your paired machines. Click to go local.'
                : !householdPeersReachable
                  ? 'Household mode unavailable — no paired machines reachable. Set one up in Household.'
                  : 'Route to a machine in your household'}
              title={!householdPeersReachable
                ? 'No household peers reachable — pair a machine in Household first'
                : householdMode
                  ? 'Routing to your household. Click to go local.'
                  : 'Route to your household (private — no coordinator involved)'}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium',
                'transition-colors duration-fast border',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                householdMode
                  ? 'border-primary/30 bg-primary/10 text-primary hover:bg-primary/15'
                  : 'border-border/60 text-muted-foreground hover:text-foreground hover:border-border',
                (isStreaming || !householdPeersReachable) && 'cursor-not-allowed opacity-50',
              )}
            >
              <Home className="h-3 w-3" aria-hidden="true" />
              <span>Household</span>
            </button>

            {/* Network-mode toggle — routes through the coordinator to an
                eligible contributor peer (cooperative-wide blinded path). */}
            <button
              type="button"
              onClick={() => setNetworkMode(!networkMode)}
              disabled={isStreaming || !coordinatorReachable}
              aria-pressed={networkMode}
              aria-label={networkMode
                ? `Network mode on - serving via ${networkModelSlug}. Click to switch back to local.`
                : !coordinatorReachable
                  ? 'Network mode unavailable — coordinator unreachable. Run `make up` to start it.'
                  : 'Switch to network mode (route via a contributor peer)'}
              title={!coordinatorReachable
                ? 'Coordinator unreachable — start it with `make up`'
                : networkMode
                  ? `Network: ${networkModelSlug}. Click to go local.`
                  : 'Route via the contributor network'}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium',
                'transition-colors duration-fast border',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                networkMode
                  ? 'border-primary/30 bg-primary/10 text-primary hover:bg-primary/15'
                  : 'border-border/60 text-muted-foreground hover:text-foreground hover:border-border',
                (isStreaming || !coordinatorReachable) && 'cursor-not-allowed opacity-50',
              )}
            >
              {networkMode ? <Network className="h-3 w-3" aria-hidden="true" /> : <Cpu className="h-3 w-3" aria-hidden="true" />}
              <span>{networkMode ? 'Network' : 'Local'}</span>
            </button>

            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <InfoChip
                icon={<Thermometer className="h-3 w-3" />}
                aria-label="Edit temperature in settings"
                onClick={goToHardwareSettings}
              >
                {!settingsLoaded || !settings ? (
                  <span className="inline-block h-3 w-6 animate-hearth-pulse rounded bg-muted" aria-hidden="true" />
                ) : (
                  `Temp ${settings.runtime.temperature.toFixed(1)}`
                )}
              </InfoChip>

              <InfoChip
                icon={<Hash className="h-3 w-3" />}
                aria-label="Edit max output tokens in settings"
                onClick={goToHardwareSettings}
              >
                {!settingsLoaded || !settings ? (
                  <span className="inline-block h-3 w-8 animate-hearth-pulse rounded bg-muted" aria-hidden="true" />
                ) : (
                  `Max ${settings.runtime.max_tokens.toLocaleString()}`
                )}
              </InfoChip>

              <InfoChip
                icon={<Layers className="h-3 w-3" />}
                aria-label="Edit context size in settings"
                onClick={goToHardwareSettings}
              >
                {!settingsLoaded || !settings ? (
                  <span className="inline-block h-3 w-8 animate-hearth-pulse rounded bg-muted" aria-hidden="true" />
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
          </>
        }
      />

      {/* ------------------------------------------------------------------ */}
      {/* Main content                                                        */}
      {/* ------------------------------------------------------------------ */}
      {isEmpty ? (
        <EmptyState
          modelLoaded={sendReady}
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
        modelLoaded={sendReady}
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
        'flex items-center gap-1 tabular-nums',
        'rounded px-1.5 py-0.5',
        'transition-colors duration-fast',
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
      <div className="relative z-10 flex flex-col items-center gap-4 text-center animate-fade-in-up">
        {/* Brandmark with ember halo — matches FirstRun and Membership */}
        <div className="relative h-14 w-14">
          <div
            className="absolute inset-0 rounded-2xl bg-primary/15 blur-xl"
            aria-hidden="true"
          />
          <div className="relative flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/12 ring-1 ring-primary/20">
            <PotluckLogo size={32} className="text-primary" aria-label="Potluck" />
          </div>
        </div>
        <div className="max-w-md">
          <h2 className="font-serif text-3xl leading-tight tracking-display text-foreground">
            {modelLoaded ? 'What can I help you with?' : 'No model loaded yet'}
          </h2>
          <p className="mt-2 text-sm leading-relaxed text-muted-foreground reading-measure mx-auto">
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
            'shadow-hearth-sm hover:bg-primary/90 active:scale-[0.98]',
            'transition-all duration-fast',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          )}
        >
          Browse models
        </button>
      )}

      {modelLoaded && (
        <div
          className="relative z-10 flex w-full max-w-2xl flex-col gap-2"
          role="list"
          aria-label="Example prompts"
        >
          {EXAMPLE_PROMPTS.map((prompt, idx) => (
            <button
              key={prompt}
              type="button"
              role="listitem"
              onClick={() => onSelectPrompt(prompt)}
              style={{ animationDelay: `${idx * 60}ms` }}
              className={cn(
                'group flex items-center justify-between gap-3 rounded-xl border border-border/60 bg-card/60 px-4 py-3',
                'text-left text-[15px] text-foreground/90',
                'shadow-hearth-sm',
                'transition-all duration-fast',
                'hover:border-primary/35 hover:bg-card hover:text-foreground hover:shadow-hearth',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                'animate-fade-in-up',
              )}
            >
              <span>{prompt}</span>
              <span
                className="font-serif italic text-sm text-muted-foreground/70 group-hover:text-primary transition-colors duration-fast"
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

/**
 * ChatView — main chat interface route (/chat).
 *
 * Layout:
 *   Top bar:   ModelSelector + loaded-model info (context, temp, max_tokens)
 *   Middle:    MessageList (auto-scroll, smart scroll-up detection)
 *   Bottom:    Composer (send/stop, keyboard shortcuts)
 *   Empty state: centered card with model name + 3 example-prompt pills
 */

import { type ReactNode, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Thermometer, Hash, Layers, Cpu, Home, Globe, Users, ChevronDown, Check } from 'lucide-react';
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
import type { RoutingScope } from '@/stores/chatStore';
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
  'Explain the tradeoffs of running AI locally versus in the cloud, plainly',
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

  // Primary routing scope — the four-tier trust gradient.
  const routingScope = useChatStore((s) => s.routingScope);
  const setRoutingScope = useChatStore((s) => s.setRoutingScope);
  const networkModelSlug = useChatStore((s) => s.networkModelSlug);

  // Household-peer availability — mesh store polls the sidecar; we gate
  // the household tier on having at least one reachable peer.
  const householdPeersReachable = useMeshStore(
    (s) => s.peers.some((p) => p.reachable),
  );
  // Start the mesh store on mount so the selector has live data even when
  // the user hasn't visited /household yet. start() is idempotent.
  const meshStart = useMeshStore((s) => s.start);
  useEffect(() => {
    meshStart();
  }, [meshStart]);

  // Coordinator reachability — gates the cooperative tier. If the user has
  // cooperative scope active AND the coordinator drops, fall back to local
  // rather than letting them hit send and get an opaque error.
  const { reachable: coordinatorReachable, lastCheckedAt } = useCoordinatorHealth();

  useEffect(() => {
    if (routingScope === 'cooperative' && !coordinatorReachable && lastCheckedAt !== null) {
      setRoutingScope('local');
    }
  }, [routingScope, coordinatorReachable, lastCheckedAt, setRoutingScope]);

  // Auto-clear household scope if no peers are reachable anymore.
  useEffect(() => {
    if (routingScope === 'household' && !householdPeersReachable) {
      setRoutingScope('local');
    }
  }, [routingScope, householdPeersReachable, setRoutingScope]);

  // Send is possible when there's an inference path: local model, a
  // reachable household peer, or a reachable coordinator.
  const networkMode = routingScope === 'cooperative';
  const householdMode = routingScope === 'household';
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

            {/* Four-tier scope selector — replaces the old pair of binary toggles. */}
            <ScopeSelector
              scope={routingScope}
              onChange={setRoutingScope}
              disabled={isStreaming}
              householdAvailable={householdPeersReachable}
              cooperativeAvailable={coordinatorReachable}
              networkModelSlug={networkModelSlug}
            />

            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <InfoChip
                icon={<Thermometer className="h-3 w-3" />}
                aria-label="Edit temperature in settings"
                onClick={goToHardwareSettings}
              >
                {!settingsLoaded || !settings ? (
                  <span className="inline-block h-3 w-6 animate-hearth-pulse rounded bg-muted" aria-hidden="true" />
                ) : (
                  // Temperatures above the valid 0–2 range are a "use the model's
                  // own default" sentinel (e.g. 999) — show that, not the raw number.
                  settings.runtime.temperature > 2
                    ? 'Temp default'
                    : `Temp ${settings.runtime.temperature.toFixed(1)}`
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
                    {settings.runtime.context_size === 0
                      ? 'Ctx auto'
                      : `Ctx ${settings.runtime.context_size.toLocaleString()}`}
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
        routingScope={routingScope}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// ScopeSelector — four-tier routing scope picker
// ---------------------------------------------------------------------------

interface ScopeTierConfig {
  scope: RoutingScope;
  label: string;
  shortLabel: string;
  icon: typeof Cpu;
  hint: string;
  /** Returns undefined when the tier is available, or a short unavailability reason. */
  unavailableReason?: string;
  /** 'phase2' tiers are shown but always disabled with a setup nudge. */
  phase?: 'phase2';
}

interface ScopeSelectorProps {
  scope: RoutingScope;
  onChange: (scope: RoutingScope) => void;
  disabled: boolean;
  householdAvailable: boolean;
  cooperativeAvailable: boolean;
  networkModelSlug: string;
}

function ScopeSelector({
  scope,
  onChange,
  disabled,
  householdAvailable,
  cooperativeAvailable,
  networkModelSlug,
}: ScopeSelectorProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // Close on outside click or Escape.
  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (
        triggerRef.current && !triggerRef.current.contains(e.target as Node) &&
        menuRef.current && !menuRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    }
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', handleClick);
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('mousedown', handleClick);
      document.removeEventListener('keydown', handleKey);
    };
  }, [open]);

  const tiers: ScopeTierConfig[] = [
    {
      scope: 'local',
      label: 'Local only',
      shortLabel: 'Local',
      icon: Cpu,
      hint: 'Runs on this machine. Nothing leaves your device.',
    },
    {
      scope: 'household',
      label: 'My machines',
      shortLabel: 'My machines',
      icon: Home,
      hint: 'Runs on one of your own machines over your private mesh, encrypted between your devices. Stays within your machines.',
      ...(!householdAvailable && {
        unavailableReason: 'No paired machines reachable. Pair a machine in Household first.',
      }),
    },
    {
      scope: 'circle',
      label: 'Trusted circle',
      shortLabel: 'Circle',
      icon: Users,
      hint: 'Runs on a machine in a group you have chosen and allowed. The machine that answers sees your prompt. Set up a circle to use this.',
      phase: 'phase2' as const,
    },
    {
      scope: 'cooperative',
      label: 'Cooperative network',
      shortLabel: 'Network',
      icon: Globe,
      hint: `Runs on any verified member's machine. The machine that answers sees your prompt to generate the reply, but the request carries no identity, so they do not know it came from you.${networkModelSlug ? ` Model: ${networkModelSlug}.` : ''}`,
      ...(!cooperativeAvailable && {
        unavailableReason: 'Coordinator unreachable. Start it with `make up`.',
      }),
    },
  ];

  const active = tiers.find((t) => t.scope === scope) ?? tiers[0]!;
  const ActiveIcon = active.icon;

  // Tier-level disabled logic: globally disabled (streaming) OR tier-specific unavailability.
  function isTierInteractable(tier: ScopeTierConfig): boolean {
    if (disabled) return false;
    if (tier.phase === 'phase2') return false;
    if (tier.unavailableReason !== undefined) return false;
    return true;
  }

  function handleSelect(tier: ScopeTierConfig) {
    if (!isTierInteractable(tier)) return;
    onChange(tier.scope);
    setOpen(false);
  }

  // Keyboard navigation within the menu.
  function handleMenuKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    const items = menuRef.current?.querySelectorAll<HTMLButtonElement>('button[data-tier]');
    if (!items || items.length === 0) return;
    const current = document.activeElement as HTMLButtonElement | null;
    const currentIdx = current ? Array.from(items).indexOf(current) : -1;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      const next = items[(currentIdx + 1) % items.length];
      next?.focus();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      const prev = items[(currentIdx - 1 + items.length) % items.length];
      prev?.focus();
    } else if (e.key === 'Tab') {
      setOpen(false);
    }
  }

  return (
    <div className="relative">
      <button
        ref={triggerRef}
        type="button"
        data-testid="scope-selector-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Routing scope: ${active.label}. Click to change.`}
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium',
          'transition-colors duration-fast border',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          scope !== 'local'
            ? 'border-primary/30 bg-primary/10 text-primary hover:bg-primary/15'
            : 'border-border/60 text-muted-foreground hover:text-foreground hover:border-border',
          disabled && 'cursor-not-allowed opacity-50',
        )}
      >
        <ActiveIcon className="h-3 w-3" aria-hidden="true" />
        <span>{active.shortLabel}</span>
        <ChevronDown
          className={cn('h-2.5 w-2.5 transition-transform duration-fast', open && 'rotate-180')}
          aria-hidden="true"
        />
      </button>

      {open && (
        <div
          ref={menuRef}
          role="listbox"
          aria-label="Routing scope"
          onKeyDown={handleMenuKeyDown}
          className={cn(
            'absolute right-0 top-full z-50 mt-1.5',
            'w-80 rounded-xl border border-border bg-popover shadow-hearth',
            'py-1.5',
          )}
        >
          {tiers.map((tier) => {
            const TierIcon = tier.icon;
            const isSelected = tier.scope === scope;
            const interactable = isTierInteractable(tier);
            const isPhase2 = tier.phase === 'phase2';

            return (
              <button
                key={tier.scope}
                type="button"
                role="option"
                data-tier={tier.scope}
                aria-selected={isSelected}
                aria-disabled={!interactable}
                disabled={!interactable}
                onClick={() => handleSelect(tier)}
                className={cn(
                  'flex w-full flex-col gap-0.5 px-3 py-2.5 text-left',
                  'transition-colors duration-fast',
                  'focus-visible:outline-none focus-visible:bg-accent',
                  interactable
                    ? 'cursor-pointer hover:bg-accent'
                    : 'cursor-not-allowed opacity-60',
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <TierIcon
                      className={cn(
                        'h-3.5 w-3.5 shrink-0',
                        isSelected ? 'text-primary' : 'text-muted-foreground',
                      )}
                      aria-hidden="true"
                    />
                    <span
                      className={cn(
                        'text-xs font-medium',
                        isSelected ? 'text-primary' : 'text-foreground',
                        !interactable && 'text-muted-foreground',
                      )}
                    >
                      {tier.label}
                    </span>
                    {isPhase2 && (
                      <span className="rounded bg-muted px-1 py-0.5 text-[9px] font-medium uppercase tracking-wide text-muted-foreground/70">
                        soon
                      </span>
                    )}
                  </div>
                  {isSelected && (
                    <Check className="h-3 w-3 shrink-0 text-primary" aria-hidden="true" />
                  )}
                </div>
                <p className="pl-[22px] text-[11px] leading-snug text-muted-foreground">
                  {tier.unavailableReason !== undefined
                    ? tier.unavailableReason
                    : tier.hint}
                </p>
              </button>
            );
          })}
        </div>
      )}
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
              ? 'Ask anything. Runs on your machine by default; the network is opt-in per message.'
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

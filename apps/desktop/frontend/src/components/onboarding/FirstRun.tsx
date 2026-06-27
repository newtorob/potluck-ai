/**
 * FirstRun — 5-step onboarding wizard.
 *
 * Steps:
 *   1. Welcome       — product intro
 *   2. Hardware      — detect specs + recommend tier
 *   3. ModelPick     — choose and install a model
 *   4. Loading       — wait for model to load into runtime
 *   5. Ready         — navigate to /chat
 *
 * Completion flag: localStorage key `potluck.onboarding_completed = 'true'`
 * This key is read by App.tsx to gate whether to show FirstRun.
 *
 * Keyboard: Enter advances (unless a button has focus), Esc shows confirm exit.
 */

import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowRight,
  Loader2,
  CheckCircle2,
  X,
} from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { PotluckLogo } from '@/components/brand/PotluckLogo';
import { HardwareDetect } from './HardwareDetect';
import { ModelPick } from './ModelPick';
import { ConnectStep } from './ConnectStep';
import { useHardware } from '@/hooks/useHardware';
import { useModelStore } from '@/stores/modelStore';
import type { CatalogModel, ModelTier } from '@/stores/modelStore';
import type { DownloadProgressRecord } from '@/stores/modelStore';

// ---------------------------------------------------------------------------
// Step definitions
// ---------------------------------------------------------------------------

type Step = 'welcome' | 'hardware' | 'pick' | 'loading' | 'connect' | 'ready';

const STEP_ORDER: Step[] = ['welcome', 'hardware', 'pick', 'loading', 'connect', 'ready'];

const STEP_LABELS: Record<Step, string> = {
  welcome: 'Welcome',
  hardware: 'Hardware',
  pick: 'Choose model',
  loading: 'Loading',
  connect: 'Your machines',
  ready: 'Ready',
};

// ---------------------------------------------------------------------------
// Onboarding completion helper
// ---------------------------------------------------------------------------

const ONBOARDING_KEY = 'potluck.onboarding_completed';

function markOnboardingComplete() {
  localStorage.setItem(ONBOARDING_KEY, 'true');
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface FirstRunProps {
  /** Full model catalog — passed from App so the store is shared */
  models: CatalogModel[];
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function FirstRun({ models }: FirstRunProps) {
  const navigate = useNavigate();
  const { hardware, isLoading: hwLoading, error: hwError } = useHardware();

  const store = useModelStore();

  const [step, setStep] = useState<Step>('welcome');
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [showExitConfirm, setShowExitConfirm] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Pre-select the recommended tier when hardware and models are both available
  useEffect(() => {
    if (hardware && models.length > 0 && selectedSlug === null) {
      const tier = hardware.recommended_tier;
      const match = models.find((m) => m.tier === tier) ?? models[0];
      if (match) setSelectedSlug(match.slug);
    }
  }, [hardware, models, selectedSlug]);

  // Build per-tier model map (first model per tier from catalog)
  const tierModels: Record<ModelTier, CatalogModel | undefined> = {
    small: models.find((m) => m.tier === 'small'),
    medium: models.find((m) => m.tier === 'medium'),
    large: models.find((m) => m.tier === 'large'),
  };

  const selectedModel = models.find((m) => m.slug === selectedSlug) ?? null;
  const selectedProgress: DownloadProgressRecord | undefined = selectedModel?.download_progress;
  const isInstalling = selectedSlug !== null && (store.installing[selectedSlug] ?? false);

  // Advance from 'pick' to 'loading' once download completes and model is installed
  useEffect(() => {
    if (step !== 'pick') return;
    if (!selectedSlug) return;
    if (!selectedProgress) return;

    if (selectedProgress.status === 'complete') {
      // Start loading the model into the runtime
      setStep('loading');
      setLoadError(null);
      void store.load(selectedSlug).then(() => {
        setStep('connect');
      }).catch((err) => {
        setLoadError(err instanceof Error ? err.message : 'Failed to load model');
        // Stay on loading step so user sees the error
      });
    }
  }, [step, selectedSlug, selectedProgress, store]);

  // Auto-advance from loading to connect when activeModelSlug is set
  useEffect(() => {
    if (step === 'loading' && store.activeModelSlug === selectedSlug && selectedSlug !== null) {
      setStep('connect');
    }
  }, [step, store.activeModelSlug, selectedSlug]);

  // Keyboard: Enter advances, Esc triggers confirm exit
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setShowExitConfirm(true);
        return;
      }
      // Only handle Enter when no interactive element is focused
      if (e.key === 'Enter') {
        const tag = document.activeElement?.tagName;
        if (tag === 'BUTTON' || tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
        handleAdvance();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  });

  const handleAdvance = useCallback(() => {
    const idx = STEP_ORDER.indexOf(step);
    if (idx < STEP_ORDER.length - 1) {
      const nextStep = STEP_ORDER[idx + 1];
      if (nextStep !== undefined) setStep(nextStep);
    }
  }, [step]);

  const handleInstall = useCallback(() => {
    if (!selectedSlug) return;
    void store.install(selectedSlug);
  }, [selectedSlug, store]);

  const handleSkip = useCallback(() => {
    markOnboardingComplete();
    navigate('/chat');
  }, [navigate]);

  const handleFinish = useCallback(() => {
    markOnboardingComplete();
    navigate('/chat');
  }, [navigate]);

  const handleConfirmExit = useCallback(() => {
    markOnboardingComplete();
    navigate('/chat');
  }, [navigate]);

  const canAdvanceFromPick =
    selectedSlug !== null &&
    selectedModel?.installed === true &&
    !isInstalling;

  const downloadComplete =
    selectedProgress?.status === 'complete' || selectedModel?.installed === true;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background"
      role="dialog"
      aria-modal="true"
      aria-label="Welcome to Potluck AI"
    >
      {/* Exit confirm overlay */}
      {showExitConfirm && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/80 backdrop-blur-sm animate-fade-in">
          <div className="w-full max-w-sm rounded-2xl border border-border bg-popover p-6 shadow-hearth-lg space-y-4 animate-fade-in-up">
            <p className="font-serif text-base text-foreground">Skip setup?</p>
            <p className="text-xs text-muted-foreground leading-relaxed">
              You can install a model later from the Models page.
            </p>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={handleConfirmExit}
                className="flex-1 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors duration-fast shadow-hearth-sm"
              >
                Skip for now
              </button>
              <button
                type="button"
                onClick={() => setShowExitConfirm(false)}
                className="flex-1 rounded-lg border border-border bg-card px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground hover:border-primary/40 transition-colors duration-fast"
              >
                Continue setup
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Wizard card */}
      <div className="relative w-full max-w-lg rounded-2xl border border-border bg-card shadow-hearth-lg animate-fade-in-up">
        {/* Close button */}
        <button
          type="button"
          onClick={() => setShowExitConfirm(true)}
          className={cn(
            'absolute right-4 top-4 rounded-md p-1 text-muted-foreground',
            'hover:text-foreground hover:bg-accent transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
          aria-label="Close onboarding"
        >
          <X className="h-4 w-4" />
        </button>

        {/* Step indicator */}
        <div className="flex items-center justify-center gap-2 border-b border-border px-6 py-4">
          {STEP_ORDER.map((s, i) => {
            const isCurrent = s === step;
            const isComplete = STEP_ORDER.indexOf(step) > i;
            return (
              <div key={s} className="flex items-center gap-2">
                <div
                  className={cn(
                    'flex h-6 w-6 items-center justify-center rounded-full text-[11px] font-medium tabular-nums',
                    'transition-all duration-base ease-out-soft',
                    isCurrent
                      ? 'bg-primary text-primary-foreground shadow-hearth-sm ring-2 ring-primary/30'
                      : isComplete
                        ? 'bg-primary/15 text-primary ring-1 ring-primary/25'
                        : 'bg-muted text-muted-foreground/70',
                  )}
                  aria-label={`Step ${i + 1}: ${STEP_LABELS[s]}`}
                  aria-current={isCurrent ? 'step' : undefined}
                >
                  {isComplete ? <CheckCircle2 className="h-3.5 w-3.5" /> : i + 1}
                </div>
                {i < STEP_ORDER.length - 1 && (
                  <div
                    className={cn(
                      'h-px w-6 transition-colors duration-base',
                      isComplete ? 'bg-primary/40' : 'bg-border/70',
                    )}
                  />
                )}
              </div>
            );
          })}
        </div>

        {/* Step content */}
        <div className="px-6 py-6">
          {step === 'welcome' && <WelcomeStep />}
          {step === 'hardware' && (
            <HardwareStep hardware={hardware} isLoading={hwLoading} error={hwError} />
          )}
          {step === 'pick' && (
            <PickStep
              tierModels={tierModels}
              selectedSlug={selectedSlug}
              hardware={hardware}
              downloadProgress={selectedProgress}
              isInstalling={isInstalling}
              onSelect={setSelectedSlug}
              downloadComplete={downloadComplete}
            />
          )}
          {step === 'loading' && <LoadingStep error={loadError} />}
          {step === 'connect' && <ConnectStep />}
          {step === 'ready' && <ReadyStep />}
        </div>

        {/* Footer actions */}
        <div className="flex items-center justify-between border-t border-border px-6 py-4">
          <button
            type="button"
            onClick={step === 'connect' ? () => setStep('ready') : handleSkip}
            className={cn(
              'text-xs text-muted-foreground/80 hover:text-foreground',
              'transition-colors duration-fast rounded-sm',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60',
            )}
          >
            {step === 'ready'
              ? ''
              : step === 'connect'
                ? 'Skip for now'
                : 'Skip, I\'ll pick later'}
          </button>

          {step === 'welcome' && (
            <PrimaryButton onClick={handleAdvance}>
              Get started <ArrowRight className="h-4 w-4" />
            </PrimaryButton>
          )}

          {step === 'hardware' && (
            <PrimaryButton onClick={handleAdvance} disabled={hwLoading}>
              Continue <ArrowRight className="h-4 w-4" />
            </PrimaryButton>
          )}

          {step === 'pick' && (
            <>
              {!isInstalling && !downloadComplete && (
                <PrimaryButton onClick={handleInstall} disabled={!selectedSlug}>
                  Install model <ArrowRight className="h-4 w-4" />
                </PrimaryButton>
              )}
              {(isInstalling || (selectedProgress && selectedProgress.status === 'downloading')) && (
                <span className="text-xs text-muted-foreground">Downloading…</span>
              )}
              {selectedProgress?.status === 'verifying' && (
                <span className="text-xs text-muted-foreground">Verifying…</span>
              )}
              {canAdvanceFromPick && (
                <PrimaryButton onClick={() => {
                  setStep('loading');
                  setLoadError(null);
                  if (selectedSlug) {
                    void store.load(selectedSlug).then(() => {
                      setStep('connect');
                    }).catch((err) => {
                      setLoadError(err instanceof Error ? err.message : 'Failed to load model');
                    });
                  }
                }}>
                  Load model <ArrowRight className="h-4 w-4" />
                </PrimaryButton>
              )}
            </>
          )}

          {step === 'loading' && loadError && (
            <PrimaryButton onClick={() => {
              if (selectedSlug) {
                setLoadError(null);
                void store.load(selectedSlug).then(() => {
                  setStep('connect');
                }).catch((err) => {
                  setLoadError(err instanceof Error ? err.message : 'Failed to load model');
                });
              }
            }}>
              Retry
            </PrimaryButton>
          )}

          {step === 'connect' && (
            <PrimaryButton onClick={() => setStep('ready')}>
              Continue <ArrowRight className="h-4 w-4" />
            </PrimaryButton>
          )}

          {step === 'ready' && (
            <PrimaryButton onClick={handleFinish}>
              Start chatting <ArrowRight className="h-4 w-4" />
            </PrimaryButton>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step sub-components
// ---------------------------------------------------------------------------

function WelcomeStep() {
  return (
    <div className="space-y-5 text-center animate-fade-in-up">
      {/* Ember-tinted square with a soft glow halo — telegraphs warmth + alive */}
      <div className="relative mx-auto h-16 w-16">
        <div className="absolute inset-0 rounded-2xl bg-primary/15 blur-xl" aria-hidden="true" />
        <div className="relative flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/12 ring-1 ring-primary/20">
          <PotluckLogo size={36} className="text-primary" aria-label="Potluck" />
        </div>
      </div>
      <div className="space-y-3">
        <h1 className="font-serif text-3xl tracking-display leading-tight text-foreground">
          Welcome to Potluck AI
        </h1>
        <p className="text-sm text-muted-foreground leading-relaxed reading-measure mx-auto">
          Local-first AI that runs open-source models entirely on your device. By default your
          prompts stay on your machine: no subscriptions to corporate labs, no data harvesting,
          just fast private inference you own and control. Using the network is always opt-in.
        </p>
        <p className="text-xs italic text-muted-foreground/80 leading-relaxed">
          This setup detects your hardware and installs a model. Takes about 2–10 minutes.
        </p>
      </div>
    </div>
  );
}

function HardwareStep({
  hardware,
  isLoading,
  error,
}: {
  hardware: ReturnType<typeof useHardware>['hardware'];
  isLoading: boolean;
  error: string | null;
}) {
  return (
    <div className="space-y-4 animate-fade-in">
      <div className="space-y-1.5">
        <h2 className="font-serif text-2xl tracking-display leading-tight text-foreground">
          Your hardware
        </h2>
        <p className="text-sm leading-relaxed text-muted-foreground">
          We detected your system specs and selected the best model tier for your machine.
        </p>
      </div>
      <HardwareDetect hardware={hardware} isLoading={isLoading} error={error} />
    </div>
  );
}

function PickStep({
  tierModels,
  selectedSlug,
  hardware,
  downloadProgress,
  isInstalling,
  onSelect,
  downloadComplete,
}: {
  tierModels: Record<ModelTier, CatalogModel | undefined>;
  selectedSlug: string | null;
  hardware: ReturnType<typeof useHardware>['hardware'];
  downloadProgress: DownloadProgressRecord | undefined;
  isInstalling: boolean;
  onSelect: (slug: string) => void;
  downloadComplete: boolean | undefined;
}) {
  return (
    <div className="space-y-4 animate-fade-in">
      <div className="space-y-1.5">
        <h2 className="font-serif text-2xl tracking-display leading-tight text-foreground">
          Choose your first model
        </h2>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Select a tier. You can install additional models later from the Models page.
          {downloadComplete && (
            <span className="ml-1 inline-flex items-center gap-1 font-medium text-sage">
              <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
              Download complete
            </span>
          )}
        </p>
      </div>
      <ModelPick
        tierModels={tierModels}
        selectedSlug={selectedSlug}
        hardware={hardware}
        downloadProgress={downloadProgress}
        isInstalling={isInstalling}
        onSelect={onSelect}
      />
    </div>
  );
}

function LoadingStep({ error }: { error: string | null }) {
  return (
    <div className="flex flex-col items-center gap-5 py-6 text-center animate-fade-in">
      {error ? (
        <>
          <div className="relative h-14 w-14">
            <div
              className="absolute inset-0 rounded-2xl bg-destructive/15 blur-xl"
              aria-hidden="true"
            />
            <div className="relative flex h-14 w-14 items-center justify-center rounded-2xl bg-destructive/10 ring-1 ring-destructive/25">
              <X className="h-7 w-7 text-destructive" aria-hidden="true" />
            </div>
          </div>
          <div className="space-y-1.5 reading-measure">
            <p className="font-serif text-xl tracking-display text-foreground">
              Failed to load model
            </p>
            <p className="text-xs leading-relaxed text-destructive">{error}</p>
          </div>
        </>
      ) : (
        <>
          <div className="relative h-14 w-14">
            <div
              className="absolute inset-0 rounded-full bg-primary/15 blur-xl animate-hearth-pulse"
              aria-hidden="true"
            />
            <div className="relative flex h-14 w-14 items-center justify-center">
              <Loader2 className="h-9 w-9 animate-spin text-primary" aria-hidden="true" />
            </div>
          </div>
          <div className="space-y-1.5 reading-measure">
            <p className="font-serif text-xl tracking-display text-foreground">
              Loading model into memory
            </p>
            <p className="text-xs leading-relaxed text-muted-foreground">
              This takes 5–30 seconds depending on model size and your hardware. Just once per
              session.
            </p>
          </div>
        </>
      )}
    </div>
  );
}

function ReadyStep() {
  return (
    <div className="flex flex-col items-center gap-4 py-4 text-center animate-fade-in-up">
      {/* Sage halo for the "alive and ready" terminal state */}
      <div className="relative h-16 w-16">
        <div className="absolute inset-0 rounded-full bg-sage/25 blur-xl animate-hearth-pulse" aria-hidden="true" />
        <div className="relative flex h-16 w-16 items-center justify-center rounded-full bg-sage/15 ring-1 ring-sage/30">
          <CheckCircle2 className="h-8 w-8 text-sage" aria-hidden="true" />
        </div>
      </div>
      <div className="space-y-2">
        <h2 className="font-serif text-2xl tracking-display text-foreground">You're all set</h2>
        <p className="text-sm leading-relaxed text-muted-foreground reading-measure mx-auto">
          Your model is loaded and ready. Inference runs locally by default, so your conversations
          stay on this device unless you choose to use the network.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// PrimaryButton
// ---------------------------------------------------------------------------

function PrimaryButton({
  onClick,
  disabled,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        'flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm font-medium',
        'bg-primary text-primary-foreground shadow-hearth-sm',
        'hover:bg-primary/90 active:scale-[0.98]',
        'transition-all duration-fast',
        'disabled:opacity-50 disabled:cursor-not-allowed disabled:active:scale-100',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card',
      )}
    >
      {children}
    </button>
  );
}

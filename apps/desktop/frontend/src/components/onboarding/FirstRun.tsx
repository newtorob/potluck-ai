/**
 * FirstRun — 5-step onboarding wizard.
 *
 * Steps:
 *   1. Welcome       — product intro
 *   2. Hardware      — detect specs + recommend tier
 *   3. ModelPick     — choose and install a model
 *   4. Loading       — wait for model to load into runtime
 *   5. Connect       — mesh / machines
 *   6. Ready         — compact readiness netcheck + navigate to /chat
 *
 * Completion flag: localStorage key `potluck.onboarding_completed = 'true'`
 * This key is read by App.tsx to gate whether to show FirstRun.
 *
 * Keyboard: Enter advances (unless a button has focus), Esc shows confirm exit.
 */

import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useUiPrefsStore } from '@/stores/uiPrefsStore';
import { useNavigate } from 'react-router-dom';
import {
  ArrowRight,
  Loader2,
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Info,
  X,
  Cpu,
  Network,
  Plug,
} from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { PotluckLogo } from '@/components/brand/PotluckLogo';
import { HardwareDetect } from './HardwareDetect';
import { ModelPick } from './ModelPick';
import { ConnectStep } from './ConnectStep';
import { useHardware } from '@/hooks/useHardware';
import { useHealth } from '@/hooks/useHealth';
import { useModelStore } from '@/stores/modelStore';
import { useMeshStore } from '@/stores/meshStore';
import { deriveDiagnostics } from '@/lib/meshDiagnostics';
import { deriveReadiness, type ReadinessReport } from '@/lib/readiness';
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

function markOnboardingComplete() {
  // Writes localStorage + fires a durable PATCH to the sidecar config so the
  // flag survives localStorage wipes, reinstalls, and dev-vs-packaged launches.
  // Also updates the live uiPrefsStore so App.tsx re-evaluates showFirstRun
  // reactively without a reload.
  useUiPrefsStore.getState().setOnboardingCompleted(true);
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
  const { sidecarUp, modelLoaded, activeModelId, nCtxLoaded } = useHealth();

  const meshStatus = useMeshStore((s) => s.status);
  const meshPeers = useMeshStore((s) => s.peers);
  const meshBridge = useMeshStore((s) => s.bridge);

  // Select stable slices/actions — never subscribe to the whole store.
  // A whole-store dep in the download-complete effect re-fired load() on every
  // zustand write and hit React's maximum update depth during onboarding.
  const install = useModelStore((s) => s.install);
  const load = useModelStore((s) => s.load);
  const activeModelSlug = useModelStore((s) => s.activeModelSlug);
  const installing = useModelStore((s) => s.installing);

  const [step, setStep] = useState<Step>('welcome');
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [showExitConfirm, setShowExitConfirm] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Guard so download-complete only kicks load() once per slug.
  const loadStartedForSlug = useRef<string | null>(null);

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
  const downloadStatus = selectedProgress?.status;
  const isInstalling = selectedSlug !== null && (installing[selectedSlug] ?? false);

  // Advance from 'pick' to 'loading' once download completes and model is installed
  useEffect(() => {
    if (step !== 'pick') return;
    if (!selectedSlug) return;
    if (downloadStatus !== 'complete') return;
    if (loadStartedForSlug.current === selectedSlug) return;

    loadStartedForSlug.current = selectedSlug;
    setStep('loading');
    setLoadError(null);
    void load(selectedSlug)
      .then(() => {
        setStep('connect');
      })
      .catch((err) => {
        // Allow retry from the loading-step error UI
        loadStartedForSlug.current = null;
        setLoadError(err instanceof Error ? err.message : 'Failed to load model');
      });
  }, [step, selectedSlug, downloadStatus, load]);

  // Auto-advance from loading to connect when activeModelSlug is set
  useEffect(() => {
    if (step === 'loading' && activeModelSlug === selectedSlug && selectedSlug !== null) {
      setStep('connect');
    }
  }, [step, activeModelSlug, selectedSlug]);

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
    void install(selectedSlug);
  }, [selectedSlug, install]);

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

  // Compute readiness for the ready step — mirrors ReadinessView's useMemo
  // exactly, including exactOptionalPropertyTypes-safe conditional spreads.
  const readinessReport: ReadinessReport = useMemo(() => {
    const mesh = deriveDiagnostics({ bridge: meshBridge, status: meshStatus, peers: meshPeers });
    // Only spread optional fields when the signal is present — passing
    // `prop: undefined` fails exactOptionalPropertyTypes.
    const vpnConflict =
      meshStatus !== null
        ? { conflict: meshStatus.vpn_conflict, detail: meshStatus.vpn_detail }
        : undefined;
    const wgPortListening = meshStatus !== null ? meshStatus.wg_port_listening : undefined;
    return deriveReadiness({
      sidecarUp,
      modelLoaded,
      activeModelId,
      nCtxLoaded,
      hardware,
      enrolled: meshStatus?.node_id != null,
      mesh,
      peers: meshPeers,
      ...(vpnConflict !== undefined ? { vpnConflict } : {}),
      ...(wgPortListening !== undefined ? { wgPortListening } : {}),
    });
  }, [
    meshBridge,
    meshStatus,
    meshPeers,
    sidecarUp,
    modelLoaded,
    activeModelId,
    nCtxLoaded,
    hardware,
  ]);

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
          {step === 'ready' && (
            <ReadyStep report={readinessReport} onFinish={handleFinish} />
          )}
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
                    loadStartedForSlug.current = selectedSlug;
                    void load(selectedSlug).then(() => {
                      setStep('connect');
                    }).catch((err) => {
                      loadStartedForSlug.current = null;
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
                loadStartedForSlug.current = selectedSlug;
                void load(selectedSlug).then(() => {
                  setStep('connect');
                }).catch((err) => {
                  loadStartedForSlug.current = null;
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

// ---------------------------------------------------------------------------
// Severity style map for compact readiness rows — a minimal subset of the
// full SEV table in ReadinessView (which is file-private there).
// ---------------------------------------------------------------------------

const COMPACT_SEV = {
  ok: {
    Icon: CheckCircle2,
    tone: 'text-emerald-600 dark:text-emerald-400',
  },
  info: {
    Icon: Info,
    tone: 'text-muted-foreground',
  },
  warn: {
    Icon: AlertTriangle,
    tone: 'text-amber-600 dark:text-amber-400',
  },
  error: {
    Icon: XCircle,
    tone: 'text-red-600 dark:text-red-400',
  },
} as const;

function ReadyStep({
  report,
  onFinish,
}: {
  report: ReadinessReport;
  onFinish: () => void;
}) {
  const navigate = useNavigate();

  const machineChecks = report.checks.filter((c) => c.group === 'machine');
  const networkChecks = report.checks.filter((c) => c.group === 'network');

  // Gaps are any check that isn't 'ok' — shown with label + fix.
  const hasGaps = report.checks.some((c) => c.severity === 'warn' || c.severity === 'error');

  function handleViewReadiness() {
    onFinish(); // marks onboarding complete
    navigate('/readiness');
  }

  function handleOpenConnect() {
    onFinish(); // marks onboarding complete
    navigate('/settings');
  }

  return (
    <div className="flex flex-col gap-4 animate-fade-in-up">
      {/* Header */}
      <div className="flex flex-col items-center gap-3 text-center">
        <div className="relative h-14 w-14">
          <div
            className="absolute inset-0 rounded-full bg-sage/25 blur-xl animate-hearth-pulse"
            aria-hidden="true"
          />
          <div className="relative flex h-14 w-14 items-center justify-center rounded-full bg-sage/15 ring-1 ring-sage/30">
            <CheckCircle2 className="h-7 w-7 text-sage" aria-hidden="true" />
          </div>
        </div>
        <div className="space-y-1">
          <h2 className="font-serif text-2xl tracking-display text-foreground">
            {report.machineReady ? 'Your machine is ready' : 'Almost there'}
          </h2>
          <p className="text-sm leading-relaxed text-muted-foreground reading-measure mx-auto">
            {report.machineReady && !hasGaps
              ? 'Inference runs locally by default. Your conversations stay on this device unless you choose to use the network.'
              : 'Inference runs locally by default. Any gaps below are non-blocking — you can fix them after you start.'}
          </p>
        </div>
      </div>

      {/* Compact readiness summary */}
      <div className="space-y-2">
        <ReadinessGroup
          icon={<Cpu className="size-3 text-muted-foreground" aria-hidden />}
          label="This machine"
          checks={machineChecks}
        />
        {networkChecks.length > 0 && (
          <ReadinessGroup
            icon={<Network className="size-3 text-muted-foreground" aria-hidden />}
            label="Network"
            checks={networkChecks}
          />
        )}
      </div>

      {/* Coding-agent on-ramp. Planted here so the MCP path isn't undiscovered;
          the actual setup lives in Settings, so this is a pointer, not a step. */}
      <div className="rounded-lg border border-border/70 bg-card px-3 py-2.5">
        <div className="flex items-start gap-2.5">
          <Plug className="mt-0.5 size-3.5 shrink-0 text-primary" aria-hidden />
          <div className="min-w-0 flex-1 space-y-0.5">
            <p className="text-xs font-medium text-foreground leading-snug">
              Bring your coding agents
            </p>
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              Potluck can serve your memory and tools to agents like Claude Code and
              Cursor over MCP. Set it up any time in{' '}
              <button
                type="button"
                onClick={handleOpenConnect}
                className="underline underline-offset-2 hover:text-foreground transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring rounded-sm"
              >
                Settings, under Connect tools
              </button>
              .
            </p>
          </div>
        </div>
      </div>

      {/* Pointer to full Readiness view — only after onboarding completes */}
      <p className="text-center text-[11px] leading-relaxed text-muted-foreground">
        Run a full netcheck anytime from{' '}
        <button
          type="button"
          onClick={handleViewReadiness}
          className="underline underline-offset-2 hover:text-foreground transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring rounded-sm"
        >
          Readiness
        </button>
        .
      </p>
    </div>
  );
}

function ReadinessGroup({
  icon,
  label,
  checks,
}: {
  icon: React.ReactNode;
  label: string;
  checks: ReadinessReport['checks'];
}) {
  if (checks.length === 0) return null;
  return (
    <div className="overflow-hidden rounded-lg border border-border/70 bg-card">
      <div className="flex items-center gap-1.5 border-b border-border/70 px-3 py-1.5">
        {icon}
        <span className="text-[10px] font-medium uppercase tracking-hearth-label text-muted-foreground">
          {label}
        </span>
      </div>
      <ul className="divide-y divide-border/50">
        {checks.map((check) => {
          const sev = COMPACT_SEV[check.severity];
          const Icon = sev.Icon;
          return (
            <li key={check.code} className="flex items-start gap-2.5 px-3 py-2">
              <Icon
                className={cn('mt-0.5 size-3.5 shrink-0', sev.tone)}
                aria-hidden
              />
              <div className="min-w-0 flex-1">
                <p className="text-xs font-medium text-foreground leading-snug">
                  {check.title}
                </p>
                {(check.severity === 'warn' || check.severity === 'error') && check.fix && (
                  <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
                    <span className="text-foreground/60">Fix: </span>
                    {check.fix}
                  </p>
                )}
              </div>
            </li>
          );
        })}
      </ul>
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

/**
 * HouseholdView - /household route.
 *
 * The visible payoff for the Phase G mesh stack. Surfaces:
 *   1. **Status header** - mesh up/down + node identity (truncated mesh IP).
 *   2. **Roster** - every peer in the household with reachability badges.
 *   3. **Add another machine** CTA - opens the AddMachineWizard.
 *   4. **Onboarding for unpaired machines** - "be first" or "join existing".
 *   5. **Unpair** - destructive action, confirms inline.
 *
 * Privacy invariant:
 *   This view is allowed to surface per-peer identity (node_name, mesh_ip,
 *   reachability mode) - that's exactly what a household roster is for.
 *   What this view must not do is leak peer identity into the /network
 *   aggregate view or into error toasts for unrelated peers.
 *
 * The view subscribes to the meshStore on mount and unsubscribes on
 * unmount; the store handles polling cadence + visibility-pause.
 */

import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import {
  Home,
  Plus,
  LogOut,
  Link2,
  RadioTower,
  AlertCircle,
  Monitor,
  Loader2,
  WifiOff,
  ShieldOff,
  Maximize2,
  CheckCircle2,
  ArrowRight,
  Cpu,
  KeyRound,
  Network,
  Wifi,
  Cable,
  Trash2,
} from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';
import { useMeshStore } from '@/stores/meshStore';
import { useConnectionStore } from '@/stores/connectionStore';
import { useChatStore } from '@/stores/chatStore';
import { useHealth } from '@/hooks/useHealth';
import { localApi } from '@/lib/localApi';
import {
  getBigModelEligiblePeers,
  getPipelineEligiblePeers,
  probePeer,
  type PeerCapabilities,
} from '@/lib/householdInference';
import { MeshMap } from '@/components/mesh/MeshMap';
import {
  computeCapacity,
  speedLabel,
  speedHint,
  speedColorClasses,
  type CapacitySnapshot,
  type CapacityResult,
} from '@/lib/capacity';
import { useModelStore, type ModelTier } from '@/stores/modelStore';
import {
  meshClient,
  issuePairingToken,
  getCurrentHousehold,
  removeMachine,
  MeshApiError,
} from '@/lib/meshClient';
import { relativeTime } from '@/lib/timeFormat';
import { formatModelName } from '@/lib/modelName';
import type { MeshPeer, MeshStatus, HouseholdMachine } from '@/types/mesh';
import { AddMachineWizard } from '@/components/household/AddMachineWizard';
import { MeshEnablePanel } from '@/components/household/MeshEnablePanel';
import { CoordinatorMismatchBanner } from '@/components/household/CoordinatorMismatchBanner';
import { MeshServiceOutdatedBanner } from '@/components/household/MeshServiceOutdatedBanner';
import { describeCoordinatorAuthError } from '@/lib/coordinatorErrorMessage';
import { ContributionReceiptsCard } from '@/components/household/ContributionReceiptsCard';
import { deriveDiagnostics, getConnectedPeers, type PeerDiagnostic } from '@/lib/meshDiagnostics';
import { PreflightBanner } from '@/components/household/PreflightBanner';
import { contributionReceiptsEnabled } from '@/lib/appPrefs';

// ---------------------------------------------------------------------------
// Root
// ---------------------------------------------------------------------------

export function HouseholdView() {
  const status = useMeshStore((s) => s.status);
  const peers = useMeshStore((s) => s.peers);
  const bridge = useMeshStore((s) => s.bridge);
  const daemonStatus = useMeshStore((s) => s.daemonStatus);
  const lastError = useMeshStore((s) => s.lastError);
  const start = useMeshStore((s) => s.start);
  const refresh = useMeshStore((s) => s.refresh);

  const [wizardOpen, setWizardOpen] = useState(false);

  useEffect(() => {
    // The store is started app-wide (App.tsx) to keep the StatusBar mesh pill
    // live across views; start() here is an idempotent safety net. We do not
    // stop() on unmount, since that would kill the app-wide poll.
    start();
  }, [start]);

  // --- bridge-missing (running outside Electron) ---
  if (bridge === 'bridge_missing') {
    return (
      <div className="flex h-full flex-col">
        <HouseholdHeader status={null} />
        <EmptyState
          icon={ShieldOff}
          title="Mesh is only available in the desktop app"
          description={
            <>
              You're seeing this view in a regular browser. To set up a
              household, open Potluck through the desktop app. The mesh
              sidecar runs only there.
            </>
          }
        />
      </div>
    );
  }

  // --- mesh daemon needs enabling / approval / is starting up ---
  // (the "app brings up the mesh" flow - macOS SMAppService today; see
  // MeshEnablePanel + main.js's mesh-daemon-supervision section)
  if (
    bridge === 'daemon_not_installed' ||
    bridge === 'daemon_requires_approval' ||
    bridge === 'daemon_starting' ||
    bridge === 'daemon_failed' ||
    bridge === 'daemon_unsupported'
  ) {
    return (
      <div className="flex h-full flex-col">
        <HouseholdHeader status={null} />
        <MeshEnablePanel bridge={bridge} daemonStatus={daemonStatus} onChanged={() => void refresh()} />
      </div>
    );
  }

  // --- sidecar offline (bridge present, socket not reachable, no daemon
  // info available to explain why - dev mode without the daemon bridge, a
  // platform daemon supervision doesn't cover, or a rare timing race) ---
  if (bridge === 'sidecar_offline') {
    return (
      <div className="flex h-full flex-col">
        <HouseholdHeader status={null} />
        <EmptyState
          icon={WifiOff}
          tone="warning"
          title="Mesh sidecar isn't running"
          description={
            <>
              The Go mesh sidecar isn't responding on the local socket.
              In development, start it from{' '}
              <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
                apps/desktop/go/potluck-mesh
              </code>
              {' '}with{' '}
              <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
                make run
              </code>
              . If you're running a packaged build, try restarting Potluck.
            </>
          }
        />
      </div>
    );
  }

  // --- still bootstrapping ---
  if (status === null) {
    return (
      <div className="flex h-full flex-col">
        <HouseholdHeader status={null} />
        <div className="flex flex-1 items-center justify-center">
          <Loader2
            className="h-5 w-5 animate-spin text-muted-foreground"
            aria-label="Loading mesh status"
          />
        </div>
      </div>
    );
  }

  const isPaired = status.node_id !== null && status.mesh_ip !== null;

  return (
    <>
      <div className="flex h-full flex-col overflow-hidden">
        <HouseholdHeader status={status} />

        <div className="flex-1 overflow-y-auto px-6 py-6 space-y-8">
          <MeshServiceOutdatedBanner />
          <CoordinatorMismatchBanner />

          {lastError !== null && (
            <ErrorBanner message={lastError} />
          )}

          {!isPaired ? (
            <UnpairedPanels onPaired={() => void refresh()} />
          ) : (
            <PairedView
              status={status}
              peers={peers}
              onAddMachine={() => setWizardOpen(true)}
              onUnpaired={() => void refresh()}
            />
          )}

          {/* Privacy footer - same treatment as /network */}
          <p
            className="pt-2 text-xs text-muted-foreground max-w-2xl"
            aria-label="Privacy note"
          >
            Your prompts travel directly between your machines over encrypted tunnels.
            The coordinator stores enrollment details, including machine names and
            mesh addresses, to connect them. Other members cannot browse your household.
          </p>
        </div>
      </div>

      <AddMachineWizard
        open={wizardOpen}
        onClose={() => {
          setWizardOpen(false);
          // Refresh once on close so a newly-paired peer shows up promptly.
          void refresh();
        }}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Header - title + status pill
// ---------------------------------------------------------------------------

function HouseholdHeader({ status }: { status: MeshStatus | null }) {
  const peers = useMeshStore((s) => s.peers);
  const bridge = useMeshStore((s) => s.bridge);
  const report = deriveDiagnostics({ bridge, status, peers });
  const connectedCount = getConnectedPeers({ bridge, status, peers }).length;
  const meshUp = bridge === 'reachable' && status?.mesh_status === 'running';

  return (
    <ViewHeader
      eyebrow="Private mesh"
      title="My Machines"
      actions={<Link to="/readiness" className="rounded-md border border-border px-3 py-2 text-xs font-medium text-foreground hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Check readiness</Link>}
      description={
        <span className="flex flex-col gap-1">
          <span>Your machines and how they connect to each other.</span>
          <span className="flex items-center gap-3">
            <span className="flex items-center gap-1.5">
              <span
                className={cn(
                  'inline-block h-2 w-2 rounded-full',
                  report.overall === 'error' ? 'bg-destructive'
                    : report.overall === 'warn' ? 'bg-amber-500'
                    : meshUp && connectedCount > 0 ? 'bg-sage animate-hearth-pulse'
                    : 'bg-muted-foreground/40',
                )}
                aria-hidden="true"
              />
              {meshUp
                ? status?.node_id
                  ? `${peers.length} enrolled · ${connectedCount} connected`
                  : 'Ready to set up'
                : status?.mesh_status === 'error'
                  ? 'Mesh error'
                  : status?.mesh_status === 'starting'
                    ? 'Starting…'
                    : status?.node_id ? report.headline : 'Mesh not set up'}
            </span>
            {status?.mesh_ip && (
              <span
                className="font-mono text-xs text-muted-foreground"
                title="Your mesh IP within this household"
              >
                {status.mesh_ip}
              </span>
            )}
          </span>
        </span>
      }
    />
  );
}

// ---------------------------------------------------------------------------
// Unpaired state - two onboarding paths
// ---------------------------------------------------------------------------

const primaryButtonClass = cn(
  'inline-flex h-9 items-center justify-center gap-1.5 rounded-lg px-3.5',
  'bg-primary text-primary-foreground text-sm font-medium',
  'hover:bg-primary/90 transition-colors',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
  'disabled:cursor-not-allowed disabled:opacity-60',
);

const smallButtonClass = cn(
  'inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium',
  'bg-primary text-primary-foreground hover:bg-primary/90 transition-colors',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
  'disabled:cursor-not-allowed disabled:opacity-60',
);

/** Shared "mint a pairing token for MY household + redeem it locally" action.
 *  Setting up the first machine and one-tap-joining your existing household are
 *  the exact same coordinator dance (idempotent household + pairing token +
 *  local /pair); only the surrounding copy differs. Includes the stale-key
 *  recovery (a leftover wg.key marks the sidecar 'enrolled' even after the
 *  coordinator forgot it) and the wrong-coordinator "switch and retry" path. */
function useJoinHousehold(onPaired: () => void) {
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [switchToUrl, setSwitchToUrl] = useState<string | null>(null);

  async function join() {
    setError(null);
    setSwitchToUrl(null);
    setWorking(true);
    try {
      const token = await issuePairingToken();
      try {
        await meshClient.pair({ pairing_token: token.token });
      } catch (err) {
        if (err instanceof MeshApiError && err.code === 'already_enrolled') {
          await meshClient.unpair();
          const fresh = await issuePairingToken();
          await meshClient.pair({ pairing_token: fresh.token });
        } else {
          throw err;
        }
      }
      onPaired();
    } catch (err) {
      const info = describeCoordinatorAuthError(err, {
        coordinatorUrl: useConnectionStore.getState().coordinatorUrl,
        sidecarReportedUrl: useConnectionStore.getState().sidecarReportedUrl,
      });
      setError(info.message);
      setSwitchToUrl(info.switchToUrl ?? null);
    } finally {
      setWorking(false);
    }
  }

  function switchAndRetry() {
    if (switchToUrl === null) return;
    useConnectionStore.getState().setCoordinatorUrl(switchToUrl);
    void join();
  }

  return { working, error, switchToUrl, join, switchAndRetry };
}

/** Tier 1: when the signed-in account already has a household, offer a one-tap
 *  join with no code. Otherwise treat this as the first machine. Pasting an
 *  invite code (for joining a DIFFERENT account's household) is demoted to a
 *  secondary link so it never competes with the primary path. */
function UnpairedPanels({ onPaired }: { onPaired: () => void }) {
  const [detect, setDetect] = useState<
    | { status: 'loading' }
    | { status: 'has_household'; machines: HouseholdMachine[] }
    | { status: 'first' }
  >({ status: 'loading' });
  const [showInvite, setShowInvite] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await getCurrentHousehold();
        if (cancelled) return;
        setDetect(
          res.household && res.machines.length > 0
            ? { status: 'has_household', machines: res.machines }
            : { status: 'first' },
        );
      } catch {
        // Detection is best-effort: if the coordinator is unreachable, fall
        // back to the manual paths rather than blocking onboarding.
        if (!cancelled) setDetect({ status: 'first' });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (detect.status === 'loading') {
    return (
      <div className="flex items-center justify-center gap-2.5 rounded-2xl border border-border bg-card p-8 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        Checking your household…
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {detect.status === 'has_household' ? (
        <JoinYourHouseholdPanel machines={detect.machines} onPaired={onPaired} />
      ) : (
        <BeFirstPanel onPaired={onPaired} />
      )}
      {showInvite ? (
        <JoinExistingPanel onPaired={onPaired} />
      ) : (
        <button
          type="button"
          onClick={() => setShowInvite(true)}
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
        >
          <KeyRound className="h-3.5 w-3.5" aria-hidden="true" />
          Have an invite code from someone else?
        </button>
      )}
    </div>
  );
}

/** One-tap "join your own household" - no code. Reuses the same pairing action
 *  as first-machine setup. */
function JoinYourHouseholdPanel({
  machines,
  onPaired,
}: {
  machines: HouseholdMachine[];
  onPaired: () => void;
}) {
  const { working, error, switchToUrl, join, switchAndRetry } = useJoinHousehold(onPaired);
  const names = machines.map((m) => m.node_name).filter(Boolean);
  const summary =
    names.length === 1
      ? names[0]
      : names.length === 2
        ? `${names[0]} and ${names[1]}`
        : `${names[0]} and ${names.length - 1} others`;

  return (
    <div className="rounded-2xl border border-primary/25 bg-card p-5 space-y-3 shadow-hearth-sm">
      <div className="flex items-center gap-2.5">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/12 ring-1 ring-primary/20">
          <Network className="h-4 w-4 text-primary" aria-hidden="true" />
        </div>
        <h2 className="font-serif text-base leading-tight tracking-display text-foreground">
          Join your household
        </h2>
      </div>
      <p className="text-sm text-muted-foreground leading-snug">
        You&apos;re already signed in on {summary}. Add this machine to your mesh
        with one tap. No code needed.
      </p>

      <ul className="space-y-1">
        {machines.slice(0, 4).map((m) => (
          <li key={m.node_id} className="flex items-center gap-2 text-sm text-foreground">
            <Monitor className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
            <span>{m.node_name}</span>
            {m.platform && <span className="text-xs text-muted-foreground">· {m.platform}</span>}
          </li>
        ))}
      </ul>

      {error !== null && (
        <div className="space-y-1.5">
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
          {switchToUrl !== null && (
            <button type="button" onClick={switchAndRetry} disabled={working} className={smallButtonClass}>
              Switch and try again
            </button>
          )}
        </div>
      )}

      <button type="button" onClick={() => void join()} disabled={working} className={primaryButtonClass}>
        {working ? (
          <>
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Joining…
          </>
        ) : (
          <>
            <Plus className="h-3.5 w-3.5" />
            Join household
          </>
        )}
      </button>
    </div>
  );
}

function BeFirstPanel({ onPaired }: { onPaired: () => void }) {
  const { working, error, switchToUrl, join, switchAndRetry } = useJoinHousehold(onPaired);

  return (
    <div className="rounded-2xl border border-primary/25 bg-card p-5 space-y-3 shadow-hearth-sm">
      <div className="flex items-center gap-2.5">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/12 ring-1 ring-primary/20">
          <Home className="h-4 w-4 text-primary" aria-hidden="true" />
        </div>
        <h2 className="font-serif text-base leading-tight tracking-display text-foreground">
          Set up a new household
        </h2>
      </div>
      <p className="text-sm text-muted-foreground leading-snug">
        This will be the first machine in your household. You can add more
        later. Any machine with Potluck installed can join.
      </p>

      {error !== null && (
        <div className="space-y-1.5">
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
          {switchToUrl !== null && (
            <button
              type="button"
              onClick={switchAndRetry}
              disabled={working}
              className={smallButtonClass}
            >
              Switch and try again
            </button>
          )}
        </div>
      )}

      <button type="button" onClick={() => void join()} disabled={working} className={primaryButtonClass}>
        {working ? (
          <>
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Setting up…
          </>
        ) : (
          <>
            <Home className="h-3.5 w-3.5" />
            Set up household
          </>
        )}
      </button>
    </div>
  );
}

/** Server-side cap on wrong-PIN guesses per broadcast session
 *  (`internal/lanpair.maxRevealAttempts`). Once hit, the inviter cancels the
 *  broadcast and stops answering - there's no distinct "exhausted" response,
 *  so we track our own count client-side purely for messaging. */
const MAX_PIN_ATTEMPTS = 5;

export function JoinExistingPanel({ onPaired }: { onPaired: () => void }) {
  const [tokenInput, setTokenInput] = useState('');
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [discovered, setDiscovered] = useState<{ hostname: string; fingerprint: string } | null>(
    null,
  );
  const [pinInput, setPinInput] = useState('');
  const [pinError, setPinError] = useState<string | null>(null);
  const [pinAttempts, setPinAttempts] = useState(0);

  // Poll the mesh sidecar for a pairing invite broadcast on the local
  // network, so a machine being invited from a nearby machine doesn't have
  // to type the full token - just the short PIN shown on the inviter.
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const d = await meshClient.discoveredPairing();
        setDiscovered((prev) => {
          if (!d.found) return null;
          const next = { hostname: d.hostname, fingerprint: d.fingerprint };
          // A new invite (different fingerprint) gets a clean attempt count -
          // the old broadcast session, and whatever PIN went with it, is gone.
          if (!prev || prev.fingerprint !== next.fingerprint) {
            setPinAttempts(0);
            setPinError(null);
            setPinInput('');
          }
          return next;
        });
      } catch {
        if (!cancelled) setDiscovered(null);
      }
    };
    void poll();
    const id = setInterval(() => void poll(), 2500);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  async function join(rawToken: string) {
    const trimmed = rawToken.trim();
    if (trimmed.length === 0) return;
    setError(null);
    setWorking(true);
    try {
      await meshClient.pair({ pairing_token: trimmed });
      setTokenInput('');
      onPaired();
    } catch (err) {
      if (err instanceof MeshApiError) {
        if (err.code === 'coordinator_error') {
          setError(
            'Token rejected. It may have expired (tokens last one hour) or already been used.',
          );
        } else if (err.code === 'already_enrolled') {
          setError(
            'This machine still has credentials from a previous setup. Click "Leave household" first, then try joining again.',
          );
        } else if (err.code === 'pair_in_progress') {
          setError('A pairing attempt is already running. Please wait a moment.');
        } else {
          setError(err.message);
        }
      } else {
        setError(err instanceof Error ? err.message : 'Pairing failed.');
      }
    } finally {
      setWorking(false);
    }
  }

  function handleJoin(e: React.FormEvent) {
    e.preventDefault();
    void join(tokenInput);
  }

  const capped = pinAttempts >= MAX_PIN_ATTEMPTS;

  // Plain function, not a form-submit handler: the PIN input/button live
  // inside the outer token-paste <form>, and nested <form> elements are
  // invalid HTML, so the PIN "Join" button is type="button" and the input's
  // onKeyDown calls this directly on Enter instead of bubbling to the form.
  async function handlePinSubmit() {
    if (!discovered || capped) return;
    const pin = pinInput.trim();
    if (pin.length === 0) return;
    setPinError(null);
    setWorking(true);
    try {
      const { token } = await meshClient.revealPairing(discovered.fingerprint, pin);
      setPinAttempts(0);
      await join(token);
    } catch (err) {
      const nextAttempts = pinAttempts + 1;
      setPinAttempts(nextAttempts);
      if (err instanceof MeshApiError && err.code === 'reveal_failed') {
        if (nextAttempts >= MAX_PIN_ATTEMPTS) {
          setPinError('Too many attempts. Restart pairing on the other device.');
        } else if (/incorrect/i.test(err.message)) {
          const left = MAX_PIN_ATTEMPTS - nextAttempts;
          setPinError(`Incorrect PIN. ${left} attempt${left === 1 ? '' : 's'} left.`);
        } else if (/timed out|no response/i.test(err.message)) {
          setPinError(
            'No response from the other device. Make sure it\'s still showing the PIN, or restart pairing there.',
          );
        } else {
          setPinError(err.message);
        }
      } else if (err instanceof MeshApiError) {
        setPinError(err.message);
      } else {
        setPinError(err instanceof Error ? err.message : 'Could not verify PIN.');
      }
    } finally {
      setWorking(false);
    }
  }

  return (
    <form
      onSubmit={handleJoin}
      className="rounded-2xl border border-border/60 bg-card p-5 space-y-3 shadow-hearth-sm"
    >
      <div className="flex items-center gap-2.5">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-muted ring-1 ring-border/60">
          <Plus className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
        </div>
        <h2 className="font-serif text-base leading-tight tracking-display text-foreground">
          Join an existing household
        </h2>
      </div>
      <p className="text-sm text-muted-foreground leading-snug">
        Paste the pairing token you generated on another machine. Tokens
        expire after an hour and can only be used once.
      </p>

      {discovered && (
        <div className="rounded-lg border border-sage/30 bg-sage/8 p-3 space-y-2.5">
          <p className="flex items-center gap-1.5 text-sm text-foreground">
            <RadioTower className="h-3.5 w-3.5 shrink-0 text-sage" aria-hidden="true" />
            <span>
              Found an invite on this network from{' '}
              <span className="font-medium">{discovered.hostname || 'a nearby machine'}</span>.
            </span>
          </p>
          <p className="text-xs text-muted-foreground leading-snug">
            Enter the PIN shown on that machine's screen to confirm it's really
            the one you're pairing with.
          </p>
          <div className="flex items-stretch gap-2">
            <label htmlFor="join-pin" className="sr-only">
              Confirmation PIN
            </label>
            <input
              id="join-pin"
              type="text"
              inputMode="numeric"
              autoComplete="off"
              spellCheck={false}
              value={pinInput}
              onChange={(e) => setPinInput(e.target.value.replace(/[^0-9]/g, '').slice(0, 6))}
              onKeyDown={(e) => {
                // Enter submits the PIN directly rather than bubbling up to
                // the outer token-paste <form>.
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void handlePinSubmit();
                }
              }}
              placeholder="123456"
              disabled={working || capped}
              className={cn(
                'w-28 rounded-lg border border-input bg-background px-3 py-2',
                'font-mono text-sm tracking-widest text-foreground placeholder:text-muted-foreground',
                'focus:outline-none focus:ring-2 focus:ring-ring',
                'disabled:cursor-not-allowed disabled:opacity-60',
              )}
            />
            <button
              type="button"
              onClick={() => void handlePinSubmit()}
              disabled={working || capped || pinInput.trim().length === 0}
              className={cn(
                'inline-flex h-9 shrink-0 items-center justify-center gap-1.5 rounded-lg px-3',
                'bg-primary text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                'disabled:cursor-not-allowed disabled:opacity-60',
              )}
            >
              {working ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : null}
              Join {discovered.hostname || 'this machine'}
            </button>
          </div>
          {pinError !== null && (
            <p className="text-xs text-destructive" role="alert">
              {pinError}
            </p>
          )}
        </div>
      )}

      <div className="space-y-2">
        <label
          htmlFor="join-token"
          className="font-sans text-xs font-medium text-muted-foreground tracking-hearth-label"
        >
          Pairing token
        </label>
        <input
          id="join-token"
          type="text"
          value={tokenInput}
          onChange={(e) => setTokenInput(e.target.value)}
          placeholder="Paste here…"
          autoComplete="off"
          spellCheck={false}
          disabled={working}
          className={cn(
            'w-full rounded-lg border border-input bg-background px-3 py-2',
            'font-mono text-sm text-foreground placeholder:text-muted-foreground',
            'focus:outline-none focus:ring-2 focus:ring-ring',
            'disabled:cursor-not-allowed disabled:opacity-60',
          )}
        />
      </div>

      {error !== null && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={working || tokenInput.trim().length === 0}
        className={cn(
          'inline-flex h-9 items-center justify-center gap-1.5 rounded-lg px-3.5',
          'border border-border bg-card text-sm font-medium text-foreground',
          'hover:bg-accent transition-colors',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          'disabled:cursor-not-allowed disabled:opacity-60',
        )}
      >
        {working ? (
          <>
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Joining…
          </>
        ) : (
          'Join household'
        )}
      </button>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Paired state - this machine + roster
// ---------------------------------------------------------------------------

function PairedView({
  status,
  peers,
  onAddMachine,
  onUnpaired,
}: {
  status: MeshStatus;
  peers: MeshPeer[];
  onAddMachine: () => void;
  onUnpaired: () => void;
}) {
  const bridge = useMeshStore((s) => s.bridge);
  const report = useMemo(
    () => deriveDiagnostics({ bridge, status, peers }),
    [bridge, status, peers],
  );

  // Probe each reachable peer's live capabilities (model, hardware, load) so the
  // roster shows what each machine can actually do, not just whether it's up.
  // Reuses the same /peer/capabilities probe the capacity + big-model cards use.
  const connectedPeers = useMemo(() => getConnectedPeers({ bridge, status, peers }), [bridge, status, peers]);
  const [capsByNode, setCapsByNode] = useState<Record<string, PeerCapabilities>>({});
  useEffect(() => {
    let cancelled = false;
    const reachable = connectedPeers;
    if (reachable.length === 0) {
      setCapsByNode({});
      return;
    }
    void (async () => {
      const probes = await Promise.all(reachable.map((p) => probePeer(p)));
      if (cancelled) return;
      const map: Record<string, PeerCapabilities> = {};
      reachable.forEach((p, i) => {
        const caps = probes[i];
        if (caps) map[p.node_id] = caps;
      });
      setCapsByNode(map);
    })();
    return () => {
      cancelled = true;
    };
  }, [connectedPeers]);

  return (
    <div className="space-y-8">
      {/* Connectivity health - named, root-cause-suppressed. */}
      <PreflightBanner report={report} />

      {/* Hero payoff - leads with the differentiator, not the status plumbing. */}
      <MeshPayoffCard peers={connectedPeers} enrolledCount={peers.length} capsByNode={capsByNode} onAddMachine={onAddMachine} />

      {/* Household map - Deco-style radial view of the mesh, each machine
          coloured by the same diagnostics the roster list uses. */}
      <section aria-label="Household mesh map">
        <SectionHeader>Your household mesh</SectionHeader>
        <MeshMap status={status} peers={peers} report={report} capsByNode={capsByNode} />
      </section>

      {/* Contribution receipts - contributor side made visible. Hidden when
          the user turns off the preference in Settings → Privacy. */}
      {contributionReceiptsEnabled() && (
        <section aria-label="Contribution receipts">
          <SectionHeader>What your machines contributed</SectionHeader>
          <ContributionReceiptsCard />
        </section>
      )}

      {/* This machine + Add CTA */}
      <section aria-label="This machine">
        <SectionHeader>This machine</SectionHeader>
        <div className="flex items-center justify-between rounded-xl border border-primary/25 bg-card p-4">
          <div className="flex items-center gap-3 min-w-0">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/12 ring-1 ring-primary/20">
              <Monitor className="h-4 w-4 text-primary" aria-hidden="true" />
            </div>
            <div className="min-w-0">
              <p className="font-medium text-foreground truncate">
                {/* mesh_ip is the only stable identifier we have for the
                    local node from /status - it doesn't include the
                    node_name (that lives on the server side). */}
                {status.mesh_ip}
              </p>
              <p className="text-xs text-muted-foreground">
                {status.coordinator_connection === 'connected'
                  ? 'Connected to the coordinator'
                  : status.coordinator_connection === 'connecting'
                    ? 'Reconnecting to the coordinator…'
                    : 'Coordinator unreachable'}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onAddMachine}
            className={cn(
              'inline-flex shrink-0 h-9 items-center justify-center gap-1.5 rounded-lg px-3',
              'bg-primary text-primary-foreground text-sm font-medium',
              'hover:bg-primary/90 transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            <Plus className="h-3.5 w-3.5" />
            Add another machine
          </button>
        </div>
      </section>

      {/* Roster */}
      <section aria-label="Other machines">
        <SectionHeader>Other machines in this household</SectionHeader>
        {peers.length === 0 ? (
          <div className="rounded-xl border border-border/60 bg-card p-4">
            <p className="text-sm text-muted-foreground">
              You're the only machine in this household right now. Click{' '}
              <span className="font-medium text-foreground">Add another machine</span>{' '}
              to invite one more. Installations on the same Wi-Fi, on a
              friend's house with Tailscale, or anywhere with internet all
              work.
            </p>
          </div>
        ) : (
          <ul className="space-y-2">
            {peers.map((peer) => (
              <PeerRow
                key={peer.node_id}
                peer={peer}
                diag={report.perPeer[peer.node_id]}
                caps={capsByNode[peer.node_id]}
                onRemoved={onUnpaired}
              />
            ))}
          </ul>
        )}
      </section>

      {/* Capacity - "your network can run X fast / Y good / Z slow".
          Renders only when at least one peer is reachable; computes from
          live /peer/capabilities probes. */}
      {connectedPeers.length > 0 && (
        <section aria-label="Network capacity">
          <SectionHeader>Your network can run</SectionHeader>
          <CapacityCard peers={connectedPeers} />
        </section>
      )}

      {/* Big Model Mode - gated on at least one peer offering rpc compute.
          The component itself probes on mount + shows nothing if none. */}
      <section aria-label="Big Model Mode">
        <BigModelModeCard peers={connectedPeers} />
      </section>

      {/* Pipeline mode - split a model's layers across machines (WAN-viable
          sibling of Big Model Mode). Self-hides when no peer is eligible. */}
      <section aria-label="Pipeline mode">
        <PipelineModeCard peers={connectedPeers} />
      </section>

      {/* Routing strategy - only matters when there's more than one peer */}
      {peers.length > 0 && (
        <section aria-label="Routing strategy">
          <SectionHeader>How requests get routed</SectionHeader>
          <RoutingStrategyControl />
        </section>
      )}

      {/* Leave household */}
      <section aria-label="Leave household">
        <SectionHeader>Leave this household</SectionHeader>
        <UnpairControl onUnpaired={onUnpaired} />
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Mesh payoff hero - the "type small, run big" differentiator, up top
// ---------------------------------------------------------------------------

/**
 * The hero card the My Machines view leads with. It states the one thing
 * single-machine local-AI tools (LM Studio, Ollama) can't do - run a model
 * too big for the machine you're typing on by routing to another machine you
 * own - and shows the live route as named chips. Qualitative on purpose; the
 * CapacityCard below carries the quantitative "what can it run" numbers.
 */
function MeshPayoffCard({
  peers,
  enrolledCount,
  capsByNode,
  onAddMachine,
}: {
  peers: MeshPeer[];
  enrolledCount: number;
  capsByNode: Record<string, PeerCapabilities>;
  onAddMachine: () => void;
}) {
  const reachable = peers.filter((p) => capsByNode[p.node_id]?.available_for_inference);
  const live = reachable.length > 0;
  const shownNames = reachable.slice(0, 3).map((p) => p.node_name);
  const extra = reachable.length - shownNames.length;

  return (
    <div className="rounded-2xl border border-primary/25 bg-gradient-to-br from-primary/[0.07] via-card to-card p-6 shadow-hearth-sm">
      <h2 className="font-serif text-2xl leading-tight tracking-display text-foreground">
        Type small, run big.
      </h2>
      <p className="mt-2 max-w-2xl text-sm leading-relaxed text-muted-foreground">
        {live ? (
          <>
            Ask from this machine using a model loaded on{' '}
            <span className="font-medium text-foreground">
              {reachable.length === 1
                ? shownNames[0]
                : `${reachable.length} of your other machines`}
            </span>
            , over your encrypted household connection.
          </>
        ) : peers.length > 0 ? (
          <>
            {peers.length} machine{peers.length === 1 ? ' is' : 's are'} connected.
            To serve a request, a machine also needs a loaded model and Contribute compute enabled.
            Check its readiness below.
          </>
        ) : enrolledCount > 0 ? (
          <>
            Your other machines are enrolled but have no active connection. Open Potluck
            on both machines and use Check readiness to find what needs attention.
          </>
        ) : (
          <>
            Right now it's just this machine. Pair a second one and your laptop can borrow your
            desktop's GPU and use models loaded there, privately over your own mesh.
          </>
        )}
      </p>

      {/* Mini route visual: this machine → your other machine(s) */}
      <div className="mt-5 flex flex-wrap items-center gap-2.5">
        <span className="inline-flex items-center gap-1.5 rounded-lg border border-primary/25 bg-primary/10 px-2.5 py-1.5 text-xs font-medium text-foreground">
          <Monitor className="h-3.5 w-3.5 text-primary" aria-hidden="true" />
          This machine
        </span>
        <ArrowRight className="h-4 w-4 shrink-0 text-primary/70" aria-hidden="true" />
        {live ? (
          <>
            {shownNames.map((name) => (
              <span
                key={name}
                className="inline-flex items-center gap-1.5 rounded-lg border border-border/60 bg-card px-2.5 py-1.5 text-xs text-foreground"
              >
                <Cpu className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
                {name}
              </span>
            ))}
            {extra > 0 && (
              <span className="text-xs text-muted-foreground">+{extra} more</span>
            )}
          </>
        ) : enrolledCount > 0 ? (
          <Link to="/readiness" className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs font-medium text-foreground hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            Check connections
          </Link>
        ) : (
          <button
            type="button"
            onClick={onAddMachine}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-lg border border-dashed border-primary/40 px-2.5 py-1.5',
              'text-xs font-medium text-primary hover:bg-primary/10 transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            Add a machine
          </button>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Capacity card - "your network can run X fast / Y good / Z slow"
// ---------------------------------------------------------------------------

// Fallback model size (billions of params) per catalog tier, used only
// when a slug carries no explicit "Nb" token. Mirrors the representative
// sizes in lib/capacity SIZE_BUCKETS.
const TIER_REPRESENTATIVE_PARAMS_B: Record<ModelTier, number> = {
  small: 3,
  medium: 8,
  large: 32,
};

/**
 * CapacityCard - surface what the household's live peer set can actually
 * run, bucketed by model size. Probes each reachable peer on mount and
 * whenever the roster changes. Honesty rule: conservative buckets only;
 * "good" never lies, "unavailable" gives an actionable upgrade hint.
 */
function CapacityCard({ peers }: { peers: MeshPeer[] }) {
  const [snapshot, setSnapshot] = useState<CapacitySnapshot | null>(null);
  const [probing, setProbing] = useState(true);
  const models = useModelStore((s) => s.models);

  // Content fingerprint of the catalog (slug + tier only). The store
  // re-allocates the `models` array on every fetch - including the 1s
  // download-progress poll - so depending on `models` directly would
  // re-probe every peer each tick. Keying on this stable string rebuilds
  // the resolver only when the catalog's relevant content actually changes.
  const catalogKey = useMemo(
    () => models.map((m) => `${m.slug}:${m.recommended_tier}`).join('|'),
    [models],
  );

  // Map a model slug to its real parameter count (billions) using our own
  // catalog. All household machines ship the same models.json, so a peer's
  // loaded slug resolves here; an unknown slug returns null and
  // computeCapacity falls back to the conservative static table for that
  // peer. We size by the slug's explicit "Nb" token (exact, quant-agnostic)
  // and fall back to the tier's representative size only when the slug
  // carries no number (e.g. phi-3.5-mini). MoE slugs ("...-a3b") are sized
  // by ACTIVE params, since they run at active-, not total-parameter speed.
  const slugParamsB = useMemo(() => {
    const map = new Map<string, number>();
    for (const m of models) {
      const active = m.slug.match(/[-_]a(\d+(?:\.\d+)?)b\b/i);
      const total = m.slug.match(/(\d+(?:\.\d+)?)b(?:[-_]|$)/i);
      const params = active
        ? parseFloat(active[1]!)
        : total
          ? parseFloat(total[1]!)
          : TIER_REPRESENTATIVE_PARAMS_B[m.recommended_tier];
      if (params != null && params > 0) map.set(m.slug, params);
    }
    return (slug: string): number | null => map.get(slug) ?? null;
    // Rebuild only when catalog content changes, not on every array re-alloc.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalogKey]);

  useEffect(() => {
    let cancelled = false;
    setProbing(true);
    void (async () => {
      try {
        const result = await computeCapacity(peers, slugParamsB);
        if (!cancelled) setSnapshot(result);
      } catch {
        if (!cancelled) setSnapshot(null);
      } finally {
        if (!cancelled) setProbing(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [peers, slugParamsB]);

  if (probing && !snapshot) {
    return (
      <div className="rounded-lg border border-border/60 bg-card p-4">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Probing peers for capability...
        </div>
      </div>
    );
  }

  if (!snapshot || snapshot.peerCount === 0) {
    return (
      <div className="rounded-lg border border-border/60 bg-card p-4 text-sm text-muted-foreground">
        No reachable peers. Capacity unavailable.
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-border/60 bg-card p-4">
      <ul className="flex flex-wrap gap-2" aria-label="Network capacity by model size">
        {snapshot.buckets.map((bucket) => (
          <CapacityPill key={bucket.bucket} result={bucket} />
        ))}
      </ul>
      <p className="mt-3 text-xs text-muted-foreground">
        Buckets reflect the best single peer for each model size. fast = &gt;20 tok/s, good = 8-20, slow = 1-8.
        {snapshot.buckets.some((b) => b.measured) && (
          <> A filled dot marks a live benchmark; the rest are conservative estimates.</>
        )}
        {snapshot.bigModelPoolSize >= 2 && (
          <> {snapshot.bigModelPoolSize} peers can pool for Big Model Mode.</>
        )}
      </p>
    </div>
  );
}

function CapacityPill({ result }: { result: CapacityResult }) {
  const titleParts: string[] = [`${result.label} model · ${speedLabel(result.speed)} (${speedHint(result.speed)})`];
  if (result.measured) {
    titleParts.push('measured from a live benchmark on a peer');
  } else if (result.speed !== 'unavailable') {
    titleParts.push('conservative estimate (no benchmark yet)');
  }
  if (result.bestPeerHost && result.speed !== 'unavailable') {
    titleParts.push(`best peer: ${result.bestPeerHost}`);
  }
  if (result.bigModelEligible) {
    titleParts.push('unlocked via Big Model Mode pooling (capacity, not speed)');
  }
  return (
    <li
      className={cn(
        'flex items-baseline gap-1.5 rounded-md border px-2.5 py-1 text-xs',
        speedColorClasses(result.speed),
      )}
      title={titleParts.join(' • ')}
    >
      <span className="font-semibold">{result.label}</span>
      <span className="opacity-80">{speedLabel(result.speed)}</span>
      {result.measured && (
        <span
          className="h-1.5 w-1.5 self-center rounded-full bg-current opacity-90"
          aria-hidden="true"
        />
      )}
      {result.bigModelEligible && (
        <span className="text-xs opacity-70" aria-label="via Big Model pooling">·pool</span>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Big Model Mode card
// ---------------------------------------------------------------------------

/**
 * Big Model Mode - load the local model with one or more household peers
 * acting as remote ggml-backend RPC devices. Weights + KV + compute get
 * spread across the local machine and the chosen peers.
 *
 * Honesty rule: this is for capacity (running models bigger than any single
 * machine holds), not speed. The UX must reflect that.
 */
function BigModelModeCard({ peers }: { peers: MeshPeer[] }) {
  const { activeModelId, loadedRpcServers, sidecarUp } = useHealth();
  const [eligible, setEligible] = useState<
    { peer: MeshPeer; capabilities: PeerCapabilities }[]
  >([]);
  const [probing, setProbing] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Probe peers for available_for_big_model on mount + whenever the peer
  // roster changes. Cheap - single GET /peer/capabilities per peer.
  useEffect(() => {
    let cancelled = false;
    setProbing(true);
    void (async () => {
      try {
        const result = await getBigModelEligiblePeers(peers);
        if (!cancelled) setEligible(result);
      } catch {
        if (!cancelled) setEligible([]);
      } finally {
        if (!cancelled) setProbing(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [peers]);

  const isOn = (loadedRpcServers?.length ?? 0) > 0;

  // Don't render the card at all when nothing's eligible and not already
  //running - keeps the household view uncluttered for users who haven't
  // set up rpc-server on any peer.
  if (!probing && eligible.length === 0 && !isOn) {
    return null;
  }

  async function handleToggle() {
    setError(null);
    if (!sidecarUp || !activeModelId) {
      setError(
        'Load a model in the Models view first. Big Model Mode reloads whatever is currently loaded with RPC peers participating.',
      );
      return;
    }

    setWorking(true);
    try {
      if (isOn) {
        // Disable - reload model with no rpc_servers.
        await localApi.loadModel(activeModelId, {});
      } else {
        // Enable - reload model distributed across eligible peers.
        const rpcServers = eligible
          .map((e) => e.capabilities.rpc_endpoint)
          .filter((s): s is string => Boolean(s));
        if (rpcServers.length === 0) {
          setError('No eligible peers found to host RPC compute.');
          return;
        }
        await localApi.loadModel(activeModelId, { rpc_servers: rpcServers });
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      // 501 from the engine means its llama.cpp build has no support for remote workers.
      if (msg.includes('501') || msg.toLowerCase().includes('patched')) {
        setError(
          'This engine build cannot run a model across machines. See docs/managed-split-models.md.',
        );
      } else {
        setError(msg);
      }
    } finally {
      setWorking(false);
    }
  }

  return (
    <div
      className={cn(
        'rounded-2xl border p-5 space-y-4 shadow-hearth-sm',
        isOn ? 'border-primary/25 bg-primary/5' : 'border-border/60 bg-card',
      )}
    >
      <div className="flex items-start gap-3">
        <div
          className={cn(
            'flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ring-1',
            isOn ? 'bg-primary/15 ring-primary/30' : 'bg-muted ring-border/60',
          )}
        >
          {isOn ? (
            <CheckCircle2 className="h-4 w-4 text-primary" aria-hidden="true" />
          ) : (
            <Maximize2 className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          )}
        </div>
        <div className="min-w-0 flex-1">
          <h2 className="font-serif text-base leading-tight tracking-display text-foreground">
            Big Model Mode
          </h2>
          <p className="mt-0.5 text-sm text-muted-foreground leading-snug">
            Run a model that's too big for any single machine in your household
            by spreading its weights across multiple peers. Slower per token,
            but lets you load larger models.
          </p>
        </div>
      </div>

      {/* Status / peer list */}
      <div className="rounded-lg border border-border/40 bg-background/40 p-3">
        {probing ? (
          <p className="text-xs text-muted-foreground flex items-center gap-1.5">
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
            Probing household peers…
          </p>
        ) : isOn ? (
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-foreground">
              Active: model split across {loadedRpcServers?.length ?? 0} peer
              {(loadedRpcServers?.length ?? 0) === 1 ? '' : 's'} + this machine
            </p>
            <ul className="space-y-0.5">
              {(loadedRpcServers ?? []).map((ep) => (
                <li
                  key={ep}
                  className="font-mono text-xs text-muted-foreground"
                >
                  {ep}
                </li>
              ))}
            </ul>
          </div>
        ) : eligible.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No eligible peers found. A peer is eligible when it's running
            contributor mode AND has <code className="font-mono">rpc-server</code>
            {' '}listening (see the headless contributor docs).
          </p>
        ) : (
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-foreground">
              {eligible.length} peer{eligible.length === 1 ? '' : 's'} eligible
            </p>
            <ul className="space-y-0.5">
              {eligible.map(({ peer, capabilities }) => (
                <li key={peer.node_id} className="text-xs text-muted-foreground">
                  <span className="font-medium text-foreground/85">{peer.node_name}</span>
                  {capabilities.hardware?.gpu_vram_gb && (
                    <span className="mx-1.5 text-muted-foreground">·</span>
                  )}
                  {capabilities.hardware?.gpu_vram_gb && (
                    <span>{capabilities.hardware.gpu_vram_gb} GB VRAM</span>
                  )}
                  <span className="mx-1.5 text-muted-foreground">·</span>
                  <span className="font-mono">{capabilities.rpc_endpoint}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {error !== null && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground leading-snug">
          {isOn
            ? 'Disabling reloads the model locally, dropping the peers.'
            : activeModelId
              ? `Will reload "${activeModelId}" across the peers above.`
              : 'Load a model first in the Models view.'}
        </p>
        <button
          type="button"
          onClick={() => void handleToggle()}
          disabled={working || probing || (!isOn && eligible.length === 0)}
          className={cn(
            'inline-flex shrink-0 h-9 items-center gap-1.5 rounded-lg px-3.5 text-sm font-medium',
            'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            isOn
              ? 'border border-border/60 bg-card text-foreground hover:bg-accent'
              : 'bg-primary text-primary-foreground hover:bg-primary/90',
            'disabled:cursor-not-allowed disabled:opacity-60',
          )}
        >
          {working ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              {isOn ? 'Disabling…' : 'Enabling…'}
            </>
          ) : isOn ? (
            'Disable'
          ) : (
            'Enable'
          )}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pipeline mode card
// ---------------------------------------------------------------------------

/**
 * Pipeline mode - split a model's layers across the local machine and household
 * peers, with only activations crossing the wire. Sibling of Big Model Mode, but
 * WAN-viable (works across the internet, not just the LAN) and torch-based rather
 * than llama.cpp/RPC. The head sidecar orchestrates the chain itself.
 *
 * Honesty rule: like Big Model Mode this is for capacity (running a model
 * bigger than any single machine), not speed.
 */
function PipelineModeCard({ peers }: { peers: MeshPeer[] }) {
  const { activeModelId, loadedPipeline, continuousBatch, sidecarUp } = useHealth();
  const [eligible, setEligible] = useState<
    { peer: MeshPeer; capabilities: PeerCapabilities }[]
  >([]);
  const [probing, setProbing] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Desired shared-batching state for the next enable. Default off. When the
  // pipeline is already on, /health is the source of truth (continuousBatch),
  // so we show that instead of the pending choice.
  const [wantBatch, setWantBatch] = useState(false);

  // Probe peers for available_for_pipeline on mount + whenever the roster
  // changes. Same cheap GET /peer/capabilities the other cards use.
  useEffect(() => {
    let cancelled = false;
    setProbing(true);
    void (async () => {
      try {
        const result = await getPipelineEligiblePeers(peers);
        if (!cancelled) setEligible(result);
      } catch {
        if (!cancelled) setEligible([]);
      } finally {
        if (!cancelled) setProbing(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [peers]);

  const isOn = loadedPipeline !== null;

  // Hide the card when nothing's eligible and not already running.
  if (!probing && eligible.length === 0 && !isOn) {
    return null;
  }

  async function handleToggle() {
    setError(null);
    if (!sidecarUp || !activeModelId) {
      setError(
        'Load a model in the Models view first. Pipeline mode reloads it split across your machines.',
      );
      return;
    }

    setWorking(true);
    try {
      if (isOn) {
        // Disable - reload locally (drops the peers).
        await localApi.loadModel(activeModelId, {});
      } else {
        // Enable - the sidecar auto-orchestrates the household chain. Carry the
        // shared-batching choice through so the chain loads with the batcher when on.
        await localApi.loadModel(activeModelId, { pipeline: true, continuousBatch: wantBatch });
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes('503')) {
        setError(
          'No peer accepted a stage. Each peer needs contributor mode on and POTLUCK_PIPELINE_ENABLED=1.',
        );
      } else if (msg.includes('502')) {
        setError('Could not build a chain across the household. Check the peers are reachable.');
      } else {
        setError(msg);
      }
    } finally {
      setWorking(false);
    }
  }

  return (
    <div
      className={cn(
        'rounded-2xl border p-5 space-y-4 shadow-hearth-sm',
        isOn ? 'border-primary/25 bg-primary/5' : 'border-border/60 bg-card',
      )}
    >
      <div className="flex items-start gap-3">
        <div
          className={cn(
            'flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ring-1',
            isOn ? 'bg-primary/15 ring-primary/30' : 'bg-muted ring-border/60',
          )}
        >
          {isOn ? (
            <CheckCircle2 className="h-4 w-4 text-primary" aria-hidden="true" />
          ) : (
            <Network className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          )}
        </div>
        <div className="min-w-0 flex-1">
          <h2 className="font-serif text-base leading-tight tracking-display text-foreground">
            Pipeline mode
          </h2>
          <p className="mt-0.5 text-sm text-muted-foreground leading-snug">
            Split a model's layers across your machines, including over the
            internet, so you can run one too big for any single box. Slower per
            token, but it reaches machines the LAN-only Big Model Mode cannot.
          </p>
        </div>
      </div>

      {/* Status / peer list */}
      <div className="rounded-lg border border-border/40 bg-background/40 p-3">
        {probing ? (
          <p className="text-xs text-muted-foreground flex items-center gap-1.5">
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
            Probing household peers…
          </p>
        ) : isOn ? (
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-foreground">
              Active: {loadedPipeline?.model_id} split across the household
            </p>
            <p className="font-mono text-xs text-muted-foreground">
              head {loadedPipeline?.head_layers}/{loadedPipeline?.n_layers} layers on{' '}
              {loadedPipeline?.device} · next hop {loadedPipeline?.downstream}
            </p>
          </div>
        ) : eligible.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No eligible peers found. A peer is eligible when it's running
            contributor mode, has the inference backend installed, and has{' '}
            <code className="font-mono">POTLUCK_PIPELINE_ENABLED</code> set.
          </p>
        ) : (
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-foreground">
              {eligible.length} peer{eligible.length === 1 ? '' : 's'} eligible
            </p>
            <ul className="space-y-0.5">
              {eligible.map(({ peer, capabilities }) => (
                <li key={peer.node_id} className="text-xs text-muted-foreground">
                  <span className="font-medium text-foreground/85">{peer.node_name}</span>
                  {capabilities.hardware?.gpu_vram_gb && (
                    <>
                      <span className="mx-1.5 text-muted-foreground">·</span>
                      <span>{capabilities.hardware.gpu_vram_gb} GB VRAM</span>
                    </>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* Advanced: shared batching. Secondary control under the status block.
          When the chain is already up, /health is the source of truth; otherwise
          this is the pending choice for the next enable. */}
      <div className="rounded-lg border border-border/40 bg-background/40 p-3">
        <label className="flex items-start justify-between gap-3 cursor-pointer">
          <span className="min-w-0">
            <span className="block text-xs font-medium text-foreground">Shared batching</span>
            <span className="mt-0.5 block text-xs text-muted-foreground leading-snug">
              Serve several agents at once through one shared pass. Helps when
              multiple tools hit this machine together; no change for a single chat.
            </span>
          </span>
          <input
            type="checkbox"
            role="switch"
            checked={isOn ? continuousBatch : wantBatch}
            disabled={working || isOn}
            onChange={(e) => setWantBatch(e.target.checked)}
            className="mt-0.5 h-4 w-4 shrink-0 accent-primary disabled:cursor-not-allowed disabled:opacity-60"
          />
        </label>
        {isOn && (
          <p className="mt-1.5 text-xs text-muted-foreground leading-snug">
            {continuousBatch
              ? 'On for the loaded chain.'
              : 'Off for the loaded chain. Disable, toggle, then re-enable to change it.'}
          </p>
        )}
      </div>

      {error !== null && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground leading-snug">
          {isOn
            ? 'Disabling reloads the model locally, dropping the peers.'
            : activeModelId
              ? `Will reload "${activeModelId}" split across the household.`
              : 'Load a model first in the Models view.'}
        </p>
        <button
          type="button"
          onClick={() => void handleToggle()}
          disabled={working || probing || !sidecarUp || (!isOn && eligible.length === 0)}
          className={cn(
            'inline-flex shrink-0 h-9 items-center gap-1.5 rounded-lg px-3.5 text-sm font-medium',
            'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            isOn
              ? 'border border-border/60 bg-card text-foreground hover:bg-accent'
              : 'bg-primary text-primary-foreground hover:bg-primary/90',
            'disabled:cursor-not-allowed disabled:opacity-60',
          )}
        >
          {working ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              {isOn ? 'Disabling…' : 'Enabling…'}
            </>
          ) : isOn ? (
            'Disable'
          ) : (
            'Enable'
          )}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Routing strategy control
// ---------------------------------------------------------------------------

function RoutingStrategyControl() {
  const routingMode = useChatStore((s) => s.routingMode);
  const setRoutingMode = useChatStore((s) => s.setRoutingMode);

  const options: {
    id: 'capability' | 'round_robin';
    title: string;
    blurb: string;
  }[] = [
    {
      id: 'capability',
      title: 'Smart routing',
      blurb:
        "Picks the best machine for each prompt. Uses the busy/idle signal, hardware tier, and which model is loaded. Long or complex prompts go to higher-tier machines.",
    },
    {
      id: 'round_robin',
      title: 'Rotate evenly',
      blurb:
        "Alternates across all eligible machines so each one stays warm. Pick this when you'd rather spread load than concentrate it on the fastest peer.",
    },
  ];

  return (
    <div className="rounded-xl border border-border/60 bg-card p-2 space-y-1">
      {options.map((opt) => {
        const active = routingMode === opt.id;
        return (
          <button
            key={opt.id}
            type="button"
            onClick={() => setRoutingMode(opt.id)}
            className={cn(
              'w-full text-left rounded-lg p-3 transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              active
                ? 'bg-primary/8 ring-1 ring-primary/30'
                : 'hover:bg-accent',
            )}
            aria-pressed={active}
          >
            <div className="flex items-center gap-2.5">
              <span
                className={cn(
                  'flex h-4 w-4 shrink-0 items-center justify-center rounded-full border-2',
                  active ? 'border-primary' : 'border-muted-foreground/40',
                )}
                aria-hidden="true"
              >
                {active && <span className="h-1.5 w-1.5 rounded-full bg-primary" />}
              </span>
              <span
                className={cn(
                  'text-sm font-medium',
                  active ? 'text-foreground' : 'text-foreground/85',
                )}
              >
                {opt.title}
              </span>
            </div>
            <p className="mt-1 ml-6 text-xs text-muted-foreground leading-snug">
              {opt.blurb}
            </p>
          </button>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Peer row
// ---------------------------------------------------------------------------

// ---- Per-peer capability truth ---------------------------------------------

function CapChip({ children, tone = 'default' }: { children: ReactNode; tone?: 'default' | 'sage' | 'amber' }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded px-1.5 py-0.5 text-xs',
        tone === 'sage' && 'bg-sage/10 text-sage',
        tone === 'amber' && 'bg-amber-500/10 text-amber-700 dark:text-amber-300',
        tone === 'default' && 'bg-muted text-muted-foreground',
      )}
    >
      {children}
    </span>
  );
}

/** Best-effort inference backend from the advertised hardware. */
function deriveBackend(gpuName: string | null | undefined, platform: string | null | undefined): string {
  const g = (gpuName || '').toLowerCase();
  if (platform === 'darwin' || g.includes('apple')) return 'Metal';
  if (/nvidia|geforce|rtx|gtx|tesla|quadro/.test(g)) return 'CUDA';
  if (/amd|radeon|\brx\b/.test(g)) return 'ROCm';
  return gpuName ? 'GPU' : 'CPU';
}

/** Quantization level pulled from the model slug (e.g. ...-q4_k_m -> Q4). */
function deriveQuant(slug: string | null | undefined): string | null {
  if (!slug) return null;
  const m = slug.match(/q(\d+)(?:[_-]?k[_-]?[ms]|[_-]?\d)?$/i);
  return m ? `Q${m[1]}` : null;
}

function formatCtx(n: number | null | undefined): string | null {
  if (!n || n <= 0) return null;
  return n >= 1000 ? `${Math.round(n / 1000)}K ctx` : `${n} ctx`;
}

function PeerRow({
  peer,
  diag,
  caps,
  onRemoved,
}: {
  peer: MeshPeer;
  diag?: PeerDiagnostic | undefined;
  caps?: PeerCapabilities | undefined;
  onRemoved: () => void;
}) {
  // A specific, actionable reason replaces a bare "Offline" - the warn/error
  // detail is shown inline so the user sees the fix without hovering.
  const showDetail = diag && (diag.severity === 'warn' || diag.severity === 'error');

  const hw = caps?.hardware;
  const backend = hw ? deriveBackend(hw.gpu_name, hw.platform) : null;
  const ctx = formatCtx(caps?.loaded_model_detail?.context_size);
  const quant = deriveQuant(caps?.loaded_model);
  const tps = caps?.loaded_model_detail?.tokens_per_sec;
  const hwLabel = hw
    ? hw.gpu_name
      ? `${hw.gpu_name}${hw.gpu_vram_gb ? ` · ${hw.gpu_vram_gb} GB` : ''}${backend ? ` · ${backend}` : ''}`
      : `CPU${hw.ram_total_gb ? ` · ${Math.round(hw.ram_total_gb)} GB RAM` : ''}`
    : null;

  return (
    <li className="flex items-start gap-3 rounded-xl border border-border/60 bg-card p-3.5">
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-muted ring-1 ring-border/60">
        <Monitor className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
      </div>

      <div className="min-w-0 flex-1">
        <p className="font-medium text-foreground truncate">
          {peer.node_name}
        </p>
        <p className="text-xs text-muted-foreground truncate">
          <span className="font-mono">{peer.mesh_ip}</span>
          {peer.platform && (
            <>
              <span className="mx-1.5 text-muted-foreground">·</span>
              {peer.platform}
            </>
          )}
          {peer.last_seen_at && (
            <>
              <span className="mx-1.5 text-muted-foreground">·</span>
              last seen {relativeTime(peer.last_seen_at)}
            </>
          )}
        </p>

        {/* Capability truth: what this machine can actually do right now. Shown
            when the peer answered a live /peer/capabilities probe; fields it
            doesn't report are simply omitted. */}
        {caps && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {caps.loaded_model ? (
              <CapChip>
                {formatModelName(caps.loaded_model)}
                {quant ? ` · ${quant}` : ''}
                {ctx ? ` · ${ctx}` : ''}
                {tps ? ` · ${tps} tok/s` : ''}
              </CapChip>
            ) : (
              <CapChip>No model loaded</CapChip>
            )}
            {hwLabel && <CapChip>{hwLabel}</CapChip>}
            {hw?.recommended_tier && <CapChip>{hw.recommended_tier} tier</CapChip>}
            <CapChip tone={caps.inference_load?.busy ? 'amber' : 'sage'}>
              {caps.inference_load?.busy ? 'Busy' : 'Idle'}
            </CapChip>
            {caps.available_for_big_model && <CapChip>Big Model ready</CapChip>}
            {caps.link_type === 'wired' && (
              <CapChip>
                <Cable className="mr-1 h-3 w-3" aria-hidden="true" />
                Wired
              </CapChip>
            )}
            {caps.link_type === 'wifi' && (
              <CapChip tone="amber">
                <Wifi className="mr-1 h-3 w-3" aria-hidden="true" />
                WiFi
              </CapChip>
            )}
          </div>
        )}

        {/* Wiring nudge: shown when this peer is on WiFi and eligible for
            distributed inference. Shown per-peer so the user knows exactly
            which machine to wire. Copy is plain + honest - no hype. */}
        {caps?.link_type === 'wifi' &&
          (caps.available_for_pipeline ?? caps.available_for_big_model) && (
            <p className="mt-1.5 flex items-start gap-1.5 text-xs leading-snug text-amber-700 dark:text-amber-300">
              <Wifi className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
              On WiFi. Wiring it (Ethernet) makes distributed inference faster and steadier.
            </p>
          )}

        {showDetail && (
          <p className="mt-1.5 text-xs leading-snug text-muted-foreground">
            {diag.detail}
            {diag.fix && (
              <>
                {' '}
                <span className="text-foreground/80">Fix: {diag.fix}</span>
              </>
            )}
          </p>
        )}
      </div>

      <div className="flex shrink-0 flex-col items-end gap-1.5">
        <PeerStateBadge diag={diag} />
        {!peer.reachable && <PeerRemoveControl peer={peer} onRemoved={onRemoved} />}
      </div>
    </li>
  );
}

/** Remove a stale/ghost machine (an unreachable roster entry left by a wiped
 *  or reinstalled machine). Two-step confirm so a live-but-sleeping machine
 *  isn't dropped by accident. */
function PeerRemoveControl({ peer, onRemoved }: { peer: MeshPeer; onRemoved: () => void }) {
  const [phase, setPhase] = useState<'idle' | 'confirm' | 'working'>('idle');
  const [error, setError] = useState<string | null>(null);

  async function doRemove() {
    setError(null);
    setPhase('working');
    try {
      await removeMachine(peer.node_id);
      onRemoved();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove.');
      setPhase('idle');
    }
  }

  if (phase === 'working') {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <Loader2 className="h-2.5 w-2.5 animate-spin" aria-hidden="true" />
        Removing…
      </span>
    );
  }

  if (phase === 'confirm') {
    return (
      <span className="inline-flex items-center gap-2 text-xs">
        <button
          type="button"
          onClick={() => void doRemove()}
          className="font-medium text-destructive hover:underline"
        >
          Remove
        </button>
        <button
          type="button"
          onClick={() => setPhase('idle')}
          className="text-muted-foreground hover:underline"
        >
          Cancel
        </button>
      </span>
    );
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        type="button"
        onClick={() => setPhase('confirm')}
        title="Remove this machine from your household"
        className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-destructive transition-colors"
      >
        <Trash2 className="h-2.5 w-2.5" aria-hidden="true" />
        Remove
      </button>
      {error !== null && (
        <span className="text-xs text-destructive" role="alert">
          {error}
        </span>
      )}
    </span>
  );
}

/** Status pill driven by the resolved per-peer diagnostic - never just "offline". */
function PeerStateBadge({ diag }: { diag?: PeerDiagnostic | undefined }) {
  if (!diag) return null;

  const VISUAL: Record<
    PeerDiagnostic['state'],
    { icon: typeof Link2 | null; spin?: boolean; cls: string }
  > = {
    direct: { icon: Link2, cls: 'border-sage/30 bg-sage/10 text-sage' },
    relayed: {
      icon: RadioTower,
      cls: 'border-sage/30 bg-sage/10 text-sage',
    },
    connecting: {
      icon: Loader2,
      spin: true,
      cls: 'border-border/60 bg-muted text-muted-foreground',
    },
    no_data_plane: {
      icon: ShieldOff,
      cls: 'border-destructive/30 bg-destructive/10 text-destructive',
    },
    key_drift: {
      icon: KeyRound,
      cls: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300',
    },
    status_unknown: { icon: null, cls: 'border-border/60 bg-muted text-muted-foreground' },
  };

  const v = VISUAL[diag.state];
  const Icon = v.icon;
  const title = diag.fix ? `${diag.detail} · ${diag.fix}` : diag.detail;

  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5',
        'text-xs font-medium',
        v.cls,
      )}
      title={title}
    >
      {Icon ? (
        <Icon className={cn('h-2.5 w-2.5', v.spin && 'animate-spin')} aria-hidden="true" />
      ) : (
        <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/50" aria-hidden="true" />
      )}
      {diag.label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Unpair - destructive, inline confirmation
// ---------------------------------------------------------------------------

function UnpairControl({ onUnpaired }: { onUnpaired: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleConfirm() {
    setError(null);
    setWorking(true);
    try {
      await meshClient.unpair();
      setConfirming(false);
      onUnpaired();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unpair failed.');
    } finally {
      setWorking(false);
    }
  }

  if (!confirming) {
    return (
      <div className="rounded-xl border border-border/60 bg-card p-4 flex items-center justify-between gap-4">
        <p className="text-sm text-muted-foreground">
          Wipe this machine's WireGuard keys and remove it from the
          household. The other machines stay paired with each other.
        </p>
        <button
          type="button"
          onClick={() => setConfirming(true)}
          className={cn(
            'inline-flex shrink-0 h-9 items-center gap-1.5 rounded-lg px-3',
            'border border-destructive/30 bg-destructive/8 text-sm font-medium text-destructive',
            'hover:bg-destructive/12 transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive',
          )}
        >
          <LogOut className="h-3.5 w-3.5" />
          Leave household
        </button>
      </div>
    );
  }

  return (
    <div
      className="rounded-xl border border-destructive/30 bg-destructive/8 p-4 space-y-3"
      role="alertdialog"
      aria-labelledby="unpair-confirm-title"
    >
      <div className="flex items-start gap-2.5">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
        <div>
          <p
            id="unpair-confirm-title"
            className="font-medium text-foreground"
          >
            Leave this household?
          </p>
          <p className="text-sm text-muted-foreground mt-0.5">
            This machine's mesh key will be wiped. You'll need a fresh
            pairing token from another machine to re-join.
          </p>
        </div>
      </div>

      {error !== null && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={() => {
            setConfirming(false);
            setError(null);
          }}
          disabled={working}
          className={cn(
            'rounded-lg border border-border/60 bg-card px-3 py-1.5 text-sm font-medium text-foreground',
            'hover:bg-accent transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            'disabled:cursor-not-allowed disabled:opacity-60',
          )}
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={() => void handleConfirm()}
          disabled={working}
          className={cn(
            'inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5',
            'bg-destructive text-destructive-foreground text-sm font-medium',
            'hover:bg-destructive/90 transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive',
            'disabled:cursor-not-allowed disabled:opacity-60',
          )}
        >
          {working ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Leaving…
            </>
          ) : (
            <>
              <LogOut className="h-3.5 w-3.5" />
              Leave
            </>
          )}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="mb-3 font-sans text-xs font-medium text-muted-foreground tracking-hearth-label">
      {children}
    </h2>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div
      className="flex items-start gap-2.5 rounded-xl border border-destructive/30 bg-destructive/8 p-3"
      role="status"
    >
      <AlertCircle
        className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
        aria-hidden="true"
      />
      <p className="text-sm text-foreground">
        <span className="font-medium">Mesh sidecar reported an error:</span>{' '}
        <span className="text-muted-foreground">{message}</span>
      </p>
    </div>
  );
}

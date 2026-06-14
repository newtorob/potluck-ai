/**
 * HouseholdView — /household route.
 *
 * The visible payoff for the Phase G mesh stack. Surfaces:
 *   1. **Status header** — mesh up/down + node identity (truncated mesh IP).
 *   2. **Roster** — every peer in the household with reachability badges.
 *   3. **Add another machine** CTA — opens the AddMachineWizard.
 *   4. **Onboarding for unpaired machines** — "be first" or "join existing".
 *   5. **Unpair** — destructive action, confirms inline.
 *
 * Privacy invariant (memory 42a7e671 / b3cbfba9):
 *   THIS view is allowed to surface per-peer identity (node_name, mesh_ip,
 *   reachability mode) — that's exactly what a household roster is for.
 *   What this view must NOT do is leak peer identity into the /network
 *   aggregate view or into error toasts for unrelated peers.
 *
 * The view subscribes to the meshStore on mount and unsubscribes on
 * unmount; the store handles polling cadence + visibility-pause.
 */

import { useEffect, useMemo, useState } from 'react';
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
} from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';
import { useMeshStore } from '@/stores/meshStore';
import { useChatStore } from '@/stores/chatStore';
import { useHealth } from '@/hooks/useHealth';
import { localApi } from '@/lib/localApi';
import { getBigModelEligiblePeers, type PeerCapabilities } from '@/lib/householdInference';
import {
  computeCapacity,
  speedLabel,
  speedHint,
  speedColorClasses,
  type CapacitySnapshot,
  type CapacityResult,
} from '@/lib/capacity';
import { meshClient, issuePairingToken, MeshApiError, CoordinatorError } from '@/lib/meshClient';
import { relativeTime } from '@/lib/timeFormat';
import type { MeshPeer, MeshStatus } from '@/types/mesh';
import { AddMachineWizard } from '@/components/household/AddMachineWizard';
import { deriveDiagnostics, type PeerDiagnostic } from '@/lib/meshDiagnostics';
import { PreflightBanner } from '@/components/household/PreflightBanner';

// ---------------------------------------------------------------------------
// Root
// ---------------------------------------------------------------------------

export function HouseholdView() {
  const status = useMeshStore((s) => s.status);
  const peers = useMeshStore((s) => s.peers);
  const bridge = useMeshStore((s) => s.bridge);
  const lastError = useMeshStore((s) => s.lastError);
  const start = useMeshStore((s) => s.start);
  const stop = useMeshStore((s) => s.stop);
  const refresh = useMeshStore((s) => s.refresh);

  const [wizardOpen, setWizardOpen] = useState(false);

  useEffect(() => {
    start();
    return () => stop();
  }, [start, stop]);

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
              household, open Potluck through the desktop app — the mesh
              sidecar runs only there.
            </>
          }
        />
      </div>
    );
  }

  // --- sidecar offline (bridge present, socket not reachable) ---
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
              . In packaged builds, the system daemon should be running —
              if it isn't, reinstall.
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

          {/* Privacy footer — same treatment as /network */}
          <p
            className="pt-2 text-[11px] text-muted-foreground/70 max-w-2xl"
            aria-label="Privacy note"
          >
            Your machines stay private. Peer names, mesh IPs, and reachability
            details on this page never leave your machines. The coordinator
            knows this group exists but the wider network has no
            view into who's in it.
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
// Header — title + status pill
// ---------------------------------------------------------------------------

function HouseholdHeader({ status }: { status: MeshStatus | null }) {
  const onlineCount = status?.peers_online_count ?? 0;
  const meshUp = status?.mesh_status === 'running';

  return (
    <ViewHeader
      eyebrow="Private mesh"
      title="My Machines"
      description={
        <span className="flex items-center gap-3">
          <span className="flex items-center gap-1.5">
            <span
              className={cn(
                'inline-block h-2 w-2 rounded-full',
                meshUp
                  ? 'bg-sage animate-hearth-pulse'
                  : status?.mesh_status === 'error'
                    ? 'bg-destructive'
                    : 'bg-muted-foreground/40',
              )}
              aria-hidden="true"
            />
            {meshUp
              ? `Mesh up — ${onlineCount} peer${onlineCount === 1 ? '' : 's'} online`
              : status?.mesh_status === 'error'
                ? 'Mesh error'
                : status?.mesh_status === 'starting'
                  ? 'Starting…'
                  : 'Mesh not set up'}
          </span>
          {status?.mesh_ip && (
            <span
              className="font-mono text-[11px] text-muted-foreground/70"
              title="Your mesh IP within this household"
            >
              {status.mesh_ip}
            </span>
          )}
        </span>
      }
    />
  );
}

// ---------------------------------------------------------------------------
// Unpaired state — two onboarding paths
// ---------------------------------------------------------------------------

function UnpairedPanels({ onPaired }: { onPaired: () => void }) {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <BeFirstPanel onPaired={onPaired} />
      <JoinExistingPanel onPaired={onPaired} />
    </div>
  );
}

function BeFirstPanel({ onPaired }: { onPaired: () => void }) {
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function pairOnce(token: string): Promise<void> {
    await meshClient.pair({ pairing_token: token });
  }

  async function handleSetup() {
    setError(null);
    setWorking(true);
    try {
      const token = await issuePairingToken();
      try {
        await pairOnce(token.token);
      } catch (err) {
        // Stale-key recovery: the sidecar persists wg.key on disk and
        // marks itself enrolled at startup whenever the file exists.
        // If the coordinator's DB was wiped (or this is a fresh dev
        // setup), the local "enrolled" flag is out of sync with the
        // coordinator's view of the world. Auto-unpair wipes the key,
        // then we re-issue a token (the first one was single-use so
        // it's now consumed even though the pair errored) and retry.
        if (err instanceof MeshApiError && err.code === 'already_enrolled') {
          await meshClient.unpair();
          const fresh = await issuePairingToken();
          await pairOnce(fresh.token);
        } else {
          throw err;
        }
      }
      onPaired();
    } catch (err) {
      if (err instanceof MeshApiError) {
        setError(`${err.code}: ${err.message}`);
      } else if (err instanceof CoordinatorError) {
        setError(
          err.httpStatus === 0 || err.httpStatus >= 500
            ? "Couldn't reach the coordinator. Check the connection URL in Settings → Advanced."
            : err.message,
        );
      } else {
        setError(err instanceof Error ? err.message : 'Could not set up household.');
      }
    } finally {
      setWorking(false);
    }
  }

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
        later — any machine with Potluck installed can join.
      </p>

      {error !== null && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <button
        type="button"
        onClick={() => void handleSetup()}
        disabled={working}
        className={cn(
          'inline-flex h-9 items-center justify-center gap-1.5 rounded-lg px-3.5',
          'bg-primary text-primary-foreground text-sm font-medium',
          'hover:bg-primary/90 transition-colors',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          'disabled:cursor-not-allowed disabled:opacity-60',
        )}
      >
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

function JoinExistingPanel({ onPaired }: { onPaired: () => void }) {
  const [tokenInput, setTokenInput] = useState('');
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleJoin(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = tokenInput.trim();
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
          setError('A pairing attempt is already running — please wait a moment.');
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

  return (
    <form
      onSubmit={(e) => void handleJoin(e)}
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

      <div className="space-y-2">
        <label
          htmlFor="join-token"
          className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label"
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
            'font-mono text-sm text-foreground placeholder:text-muted-foreground/50',
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
// Paired state — this machine + roster
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

  return (
    <div className="space-y-8">
      {/* Connectivity health — named, root-cause-suppressed (VPN-resilience Pillar #1). */}
      <PreflightBanner report={report} />

      {/* Hero payoff — leads with the differentiator, not the status plumbing. */}
      <MeshPayoffCard peers={peers} onAddMachine={onAddMachine} />

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
                    local node from /status — it doesn't include the
                    node_name (that lives on the server side). */}
                {status.mesh_ip}
              </p>
              <p className="text-[11px] text-muted-foreground/80">
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
              to invite one more — installations on the same Wi-Fi, on a
              friend's house with Tailscale, or anywhere with internet all
              work.
            </p>
          </div>
        ) : (
          <ul className="space-y-2">
            {peers.map((peer) => (
              <PeerRow key={peer.node_id} peer={peer} diag={report.perPeer[peer.node_id]} />
            ))}
          </ul>
        )}
      </section>

      {/* Capacity — "your network can run X fast / Y good / Z slow".
          Renders only when at least one peer is reachable; computes from
          live /peer/capabilities probes. */}
      {peers.some((p) => p.reachable) && (
        <section aria-label="Network capacity">
          <SectionHeader>Your network can run</SectionHeader>
          <CapacityCard peers={peers} />
        </section>
      )}

      {/* Big Model Mode — gated on at least one peer offering rpc compute.
          The component itself probes on mount + shows nothing if none. */}
      <section aria-label="Big Model Mode">
        <BigModelModeCard peers={peers} />
      </section>

      {/* Routing strategy — only matters when there's more than one peer */}
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
// Mesh payoff hero — the "type small, run big" differentiator, up top
// ---------------------------------------------------------------------------

/**
 * The hero card the My Machines view leads with. It states the one thing
 * single-machine local-AI tools (LM Studio, Ollama) can't do — run a model
 * too big for the machine you're typing on by routing to another machine you
 * own — and shows the live route as named chips. Qualitative on purpose; the
 * CapacityCard below carries the quantitative "what can it run" numbers.
 */
function MeshPayoffCard({
  peers,
  onAddMachine,
}: {
  peers: MeshPeer[];
  onAddMachine: () => void;
}) {
  const reachable = peers.filter((p) => p.reachable);
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
            Ask from this machine and Potluck can run a model too big for it on{' '}
            <span className="font-medium text-foreground">
              {reachable.length === 1
                ? shownNames[0]
                : `${reachable.length} of your other machines`}
            </span>
            , privately over your own WireGuard mesh. No cloud, no one else's hardware.
          </>
        ) : peers.length > 0 ? (
          <>
            Your other machines are offline right now. Bring one online and you can run models
            bigger than this machine can hold, privately over your own mesh.
          </>
        ) : (
          <>
            Right now it's just this machine. Pair a second one and your laptop can borrow your
            desktop's GPU — run models bigger than any single box can hold, privately over your own
            mesh.
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
// Capacity card — "your network can run X fast / Y good / Z slow"
// ---------------------------------------------------------------------------

/**
 * CapacityCard — surface what the household's live peer set can actually
 * run, bucketed by model size. Probes each reachable peer on mount and
 * whenever the roster changes. Honesty rule: conservative buckets only;
 * "good" never lies, "unavailable" gives an actionable upgrade hint.
 */
function CapacityCard({ peers }: { peers: MeshPeer[] }) {
  const [snapshot, setSnapshot] = useState<CapacitySnapshot | null>(null);
  const [probing, setProbing] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setProbing(true);
    void (async () => {
      try {
        const result = await computeCapacity(peers);
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
  }, [peers]);

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
        No reachable peers — capacity unavailable.
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
      <p className="mt-3 text-[11px] text-muted-foreground/70">
        Buckets reflect the best single peer for each model size. fast = &gt;20 tok/s, good = 8-20, slow = 1-8.
        {snapshot.bigModelPoolSize >= 2 && (
          <> {snapshot.bigModelPoolSize} peers can pool for Big Model Mode.</>
        )}
      </p>
    </div>
  );
}

function CapacityPill({ result }: { result: CapacityResult }) {
  const titleParts: string[] = [`${result.label} model — ${speedLabel(result.speed)} (${speedHint(result.speed)})`];
  if (result.bestPeerHost && result.speed !== 'unavailable') {
    titleParts.push(`best peer: ${result.bestPeerHost}`);
  }
  if (result.bigModelEligible) {
    titleParts.push('unlocked via Big Model Mode pooling — capacity, not speed');
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
      {result.bigModelEligible && (
        <span className="text-[10px] opacity-70" aria-label="via Big Model pooling">·pool</span>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Big Model Mode card
// ---------------------------------------------------------------------------

/**
 * Big Model Mode — load the LOCAL model with one or more household peers
 * acting as remote ggml-backend RPC devices. Weights + KV + compute get
 * spread across the local machine and the chosen peers.
 *
 * Honesty rule (memory e369308e): this is for CAPACITY (running models
 * bigger than any single machine holds), NOT SPEED. UX must reflect that.
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
  // roster changes. Cheap — single GET /peer/capabilities per peer.
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

  // Don't render the card at all when nothing's eligible AND not already
  // running — keeps the household view uncluttered for users who haven't
  // set up rpc-server on any peer.
  if (!probing && eligible.length === 0 && !isOn) {
    return null;
  }

  async function handleToggle() {
    setError(null);
    if (!sidecarUp || !activeModelId) {
      setError(
        'Load a model in the Models view first — Big Model Mode reloads whatever is currently loaded with RPC peers participating.',
      );
      return;
    }

    setWorking(true);
    try {
      if (isOn) {
        // Disable — reload model with no rpc_servers.
        await localApi.loadModel(activeModelId, {});
      } else {
        // Enable — reload model distributed across eligible peers.
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
      // 501 from the sidecar means stock (unpatched) llama-cpp-python.
      if (msg.includes('501') || msg.toLowerCase().includes('patched')) {
        setError(
          'This sidecar is running stock llama-cpp-python. Install the Potluck-patched wheel — see docs/phase-g/household-contributor-headless.md.',
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
              Active — model split across {loadedRpcServers?.length ?? 0} peer
              {(loadedRpcServers?.length ?? 0) === 1 ? '' : 's'} + this machine
            </p>
            <ul className="space-y-0.5">
              {(loadedRpcServers ?? []).map((ep) => (
                <li
                  key={ep}
                  className="font-mono text-[11px] text-muted-foreground"
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
                <li key={peer.node_id} className="text-[11px] text-muted-foreground">
                  <span className="font-medium text-foreground/85">{peer.node_name}</span>
                  {capabilities.hardware?.gpu_vram_gb && (
                    <span className="mx-1.5 text-muted-foreground/40">·</span>
                  )}
                  {capabilities.hardware?.gpu_vram_gb && (
                    <span>{capabilities.hardware.gpu_vram_gb} GB VRAM</span>
                  )}
                  <span className="mx-1.5 text-muted-foreground/40">·</span>
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
        <p className="text-[11px] text-muted-foreground/80 leading-snug">
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
        "Picks the best machine for each prompt — uses the busy/idle signal, hardware tier, and which model is loaded. Long or complex prompts go to higher-tier machines.",
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

function PeerRow({ peer, diag }: { peer: MeshPeer; diag?: PeerDiagnostic }) {
  // A specific, actionable reason replaces a bare "Offline" — the warn/error
  // detail is shown inline so the user sees the fix without hovering.
  const showDetail = diag && (diag.severity === 'warn' || diag.severity === 'error');

  return (
    <li className="flex items-start gap-3 rounded-xl border border-border/60 bg-card p-3.5">
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-muted ring-1 ring-border/60">
        <Monitor className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
      </div>

      <div className="min-w-0 flex-1">
        <p className="font-medium text-foreground truncate">
          {peer.node_name}
        </p>
        <p className="text-[11px] text-muted-foreground/80 truncate">
          <span className="font-mono">{peer.mesh_ip}</span>
          {peer.platform && (
            <>
              <span className="mx-1.5 text-muted-foreground/40">·</span>
              {peer.platform}
            </>
          )}
          {peer.last_seen_at && (
            <>
              <span className="mx-1.5 text-muted-foreground/40">·</span>
              last seen {relativeTime(peer.last_seen_at)}
            </>
          )}
        </p>
        {showDetail && (
          <p className="mt-1.5 text-[11px] leading-snug text-muted-foreground">
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

      <PeerStateBadge diag={diag} />
    </li>
  );
}

/** Status pill driven by the resolved per-peer diagnostic — never just "offline". */
function PeerStateBadge({ diag }: { diag?: PeerDiagnostic }) {
  if (!diag) return null;

  const VISUAL: Record<
    PeerDiagnostic['state'],
    { icon: typeof Link2 | null; spin?: boolean; cls: string }
  > = {
    direct: { icon: Link2, cls: 'border-sage/30 bg-sage/10 text-sage' },
    relayed: {
      icon: RadioTower,
      cls: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300',
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
    status_unknown: { icon: null, cls: 'border-border/60 bg-muted text-muted-foreground' },
  };

  const v = VISUAL[diag.state];
  const Icon = v.icon;
  const title = diag.fix ? `${diag.detail} — ${diag.fix}` : diag.detail;

  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5',
        'text-[10px] font-medium',
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
// Unpair — destructive, inline confirmation
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
    <h2 className="mb-3 font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
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

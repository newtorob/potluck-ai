/**
 * StatusBar — persistent bottom strip with "alive" indicators.
 *
 * Sits below the main content, above the OS chrome. Communicates the
 * three pieces of state every Potluck user wants to glance-check:
 *
 *   1. Model loaded (sage dot pulsing when active)
 *   2. MCP server up (sage dot when external agents can connect)
 *   3. Network mode (Off / Per-session / Standby) — agent-neutral framing
 *
 * Why this exists: the app previously had no persistent "alive" surface.
 * The whole --sage token was defined for "this is alive" affordances and
 * was used in exactly one placeholder. Now every view inherits a calm,
 * always-visible heartbeat without needing per-view chrome.
 *
 * Design notes:
 *   - h-7 (28px) — small enough to not steal real estate, large enough
 *     for 3:1 contrast text
 *   - text-[11px] — matches the editorial small-caps used elsewhere
 *   - border-t hairline only; no shadow stack
 *   - All indicators are status-only; settings toggles live on /settings
 *   - Network mode pill is clickable and opens the per-session network
 *     modal (placeholder until task #18 lands /network)
 *
 * Privacy note: never shows per-peer identity. Aggregate state only.
 */

import { useState, useEffect, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { cn } from '@/components/ui/cn';
import { useHealth } from '@/hooks/useHealth';
import { useChatStore } from '@/stores/chatStore';
import { useMeshStore } from '@/stores/meshStore';
import { useAuth } from '@/hooks/useAuth';
import { ipc } from '@/lib/ipc';
import { formatModelName } from '@/lib/modelName';
import { deriveDiagnostics } from '@/lib/meshDiagnostics';

export function StatusBar() {
  const { modelLoaded, activeModelId, sidecarUp } = useHealth();
  const { isSignedIn, hasCachedAccount } = useAuth();

  // App version — fetched from Electron preload on mount
  const [appVersion, setAppVersion] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    ipc.getAppVersion()
      .then((v) => { if (!cancelled) setAppVersion(v); })
      .catch(() => { /* non-fatal — running outside Electron */ });
    return () => { cancelled = true; };
  }, []);

  // Network mode reflects chatStore.networkMode — the same flag the
  // chat-composer toggle flips. Off = nothing leaves the device;
  // Per-session = the chat composer is currently set to route via the
  // contributor network on next send.
  //
  // The pill stays a STATUS indicator (not a toggle) — flipping network
  // mode happens at the place the user can also see what they're about
  // to send (the composer), per the brief's "agent-neutral, opt-in,
  // never global" rule (memory e555655d).
  const networkOn = useChatStore((s) => s.networkMode);
  const networkMode: 'off' | 'per-session' | 'standby' = networkOn ? 'per-session' : 'off';

  return (
    <footer
      className={cn(
        'flex h-7 shrink-0 items-center gap-4 border-t border-border bg-card px-4',
        'text-[11px] text-muted-foreground select-none',
      )}
      aria-label="Application status"
    >
      <StatusItem
        active={modelLoaded}
        label={modelLoaded ? (activeModelId ? formatModelName(activeModelId) : 'Model loaded') : 'No model loaded'}
        tooltip={modelLoaded ? 'A local model is loaded and ready.' : 'No model loaded. Pick one in Models.'}
        href="/models"
      />

      <Divider />

      <StatusItem
        active={sidecarUp}
        label={sidecarUp ? 'Sidecar up' : 'Sidecar offline'}
        tooltip={sidecarUp
          ? 'Local Python backend is responding on 127.0.0.1:8321.'
          : 'Local Python backend is unreachable.'}
      />

      <MeshPill />

      <Divider />

      <NetworkPill mode={networkMode} />

      {/* Shown only in the offline-account case: signed in on a cached
          token but the cloud API couldn't confirm it (see AuthGate /
          useAuth.hasCachedAccount). Not shown once actually signed in. */}
      {!isSignedIn && hasCachedAccount && (
        <>
          <Divider />
          <StatusItem
            active={false}
            label="Account offline"
            tooltip="Signed in, but Potluck can't reach the cloud API right now. Your account will resync once you're back online."
            href="/membership"
          />
        </>
      )}

      {/* Right-aligned: app version */}
      <div className="ml-auto flex items-center gap-3">
        {appVersion !== null && (
          <span className="font-mono text-[10px] text-muted-foreground/60">
            v{appVersion}
          </span>
        )}
      </div>
    </footer>
  );
}

// ---------------------------------------------------------------------------
// StatusItem — sage dot + label, optionally a router link
// ---------------------------------------------------------------------------

interface StatusItemProps {
  active: boolean;
  label: string;
  tooltip: string;
  href?: string;
}

function StatusItem({ active, label, tooltip, href }: StatusItemProps) {
  const inner = (
    <>
      <span
        className={cn(
          'inline-block h-1.5 w-1.5 shrink-0 rounded-full',
          active ? 'bg-sage' : 'bg-muted-foreground/40',
          active && 'animate-hearth-pulse',
        )}
        aria-hidden="true"
      />
      <span className="truncate">{label}</span>
    </>
  );

  const sharedClass = cn(
    'flex items-center gap-1.5 truncate max-w-[14rem]',
    'transition-colors',
    href && 'hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded',
  );

  if (href !== undefined) {
    return (
      <Link to={href} className={sharedClass} title={tooltip} aria-label={tooltip}>
        {inner}
      </Link>
    );
  }
  return (
    <span className={sharedClass} title={tooltip} aria-label={tooltip}>
      {inner}
    </span>
  );
}

// ---------------------------------------------------------------------------
// NetworkPill — agent-neutral framing per memory e555655d
// ---------------------------------------------------------------------------

interface NetworkPillProps {
  mode: 'off' | 'per-session' | 'standby';
}

function NetworkPill({ mode }: NetworkPillProps) {
  const label =
    mode === 'off'      ? 'Network: Off'
    : mode === 'standby' ? 'Network: Standby'
    : 'Network: On';

  const tooltip =
    mode === 'off'      ? 'Nothing leaves your machine. Toggle on in the chat composer.'
    : mode === 'standby' ? 'Network ready but inactive. Opt in per session.'
    : 'Your next chat send routes through the contributor network. Aggregate stats in /network.';

  // Click-through goes to /network (the observability view), not
  // /settings — the user wants to SEE the network state, not change a
  // setting. Toggling happens in the chat composer.
  return (
    <Link
      to="/network"
      className={cn(
        'flex items-center gap-1.5 rounded-full border px-2 py-0.5',
        'text-[10px] font-medium transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        mode === 'off'
          ? 'border-border/60 bg-muted/40 text-muted-foreground hover:text-foreground hover:border-border'
          // Active state borrows the chat composer's ember treatment so
          // the routing-decision signal looks the same wherever the user
          // sees it.
          : 'border-primary/30 bg-primary/10 text-primary hover:bg-primary/15',
      )}
      title={tooltip}
      aria-label={tooltip}
    >
      <span
        className={cn(
          'inline-block h-1.5 w-1.5 rounded-full',
          mode === 'off' ? 'bg-muted-foreground/50' : 'bg-sage animate-hearth-pulse',
        )}
        aria-hidden="true"
      />
      {label}
    </Link>
  );
}

// ---------------------------------------------------------------------------
// MeshPill — aggregate mesh-health heartbeat, app-wide (VPN-resilience Pillar #1)
// ---------------------------------------------------------------------------

/**
 * Surfaces the overall mesh diagnostics (from deriveDiagnostics) as a single
 * status entry — calm sage when healthy, ember/amber/red when something needs
 * attention. Links to My Machines for the full per-peer breakdown.
 *
 * Privacy: aggregate-only. The headline + global diagnostics are count-based
 * and carry no per-peer identity (per-peer reasons live behind the click on
 * /household, never here). Hidden entirely until this machine is enrolled —
 * an unpaired node has no mesh to report on.
 */
function MeshPill() {
  const status = useMeshStore((s) => s.status);
  const peers = useMeshStore((s) => s.peers);
  const bridge = useMeshStore((s) => s.bridge);
  const report = useMemo(
    () => deriveDiagnostics({ bridge, status, peers }),
    [bridge, status, peers],
  );

  // Only when this machine is actually part of a mesh. No node_id = unpaired,
  // and "mesh stopped" there would be a false alarm.
  if (!status?.node_id) return null;

  const sev = report.overall;
  const dotCls =
    sev === 'ok'
      ? 'bg-sage animate-hearth-pulse'
      : sev === 'info'
        ? 'bg-muted-foreground/50'
        : sev === 'warn'
          ? 'bg-amber-500'
          : 'bg-destructive';
  const textCls =
    sev === 'warn'
      ? 'text-amber-700 dark:text-amber-300'
      : sev === 'error'
        ? 'text-destructive'
        : '';

  // Tooltip carries the worst global (local/coordinator) cause + fix when there
  // is one. report.global never contains per-peer entries, so this stays
  // identity-free.
  const worst = report.global.find((d) => d.severity === 'warn' || d.severity === 'error');
  const tooltip = worst
    ? `${report.headline}. ${worst.detail}${worst.fix ? ` Fix: ${worst.fix}` : ''}`
    : report.headline;

  return (
    <>
      <Divider />
      <Link
        to="/household"
        className={cn(
          'flex items-center gap-1.5 truncate max-w-[16rem] rounded transition-colors',
          'hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          textCls,
        )}
        title={tooltip}
        aria-label={tooltip}
      >
        <span
          className={cn('inline-block h-1.5 w-1.5 shrink-0 rounded-full', dotCls)}
          aria-hidden="true"
        />
        <span className="truncate">{report.headline}</span>
      </Link>
    </>
  );
}

function Divider() {
  return <span className="h-3 w-px bg-border" aria-hidden="true" />;
}

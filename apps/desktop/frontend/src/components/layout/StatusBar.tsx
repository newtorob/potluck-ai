import { useUiPrefsStore } from '@/stores/uiPrefsStore';
/**
 * StatusBar - persistent bottom strip with "alive" indicators.
 *
 * Sits below the main content, above the OS chrome. Communicates the
 * three pieces of state every Potluck user wants to glance-check:
 *
 *   1. Model loaded (sage dot pulsing when active)
 *   2. MCP server up (sage dot when external agents can connect)
 *   3. Network mode (Off / Per-session / Standby) - agent-neutral framing
 *
 * Why this exists: the --sage token marks "this is alive" affordances, and
 * the status bar gives every view a calm, always-visible heartbeat without
 * needing per-view chrome.
 *
 * Design notes:
 *   - h-7 (28px) - small enough to not steal real estate, large enough
 *     for 3:1 contrast text
 *   - text-xs - matches the editorial small-caps used elsewhere
 *   - border-t hairline only; no shadow stack
 *   - All indicators are status-only; settings toggles live on /settings
 *   - Network mode pill links to /chat, where the routing scope is changed
 *
 * Privacy note: never shows per-peer identity. Aggregate state only.
 */

import { useState, useEffect, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { cn } from '@/components/ui/cn';
import { useHealth } from '@/hooks/useHealth';
import { useChatStore, type RoutingScope } from '@/stores/chatStore';
import { useMeshStore } from '@/stores/meshStore';
import { useConnectionStore } from '@/stores/connectionStore';
import { useAuth } from '@/hooks/useAuth';
import { ipc } from '@/lib/ipc';
import { formatModelName } from '@/lib/modelName';
import { deriveDiagnostics } from '@/lib/meshDiagnostics';

export function StatusBar() {
  const { modelLoaded, activeModelId, sidecarUp } = useHealth();
  const { isSignedIn, hasCachedAccount } = useAuth();

  // App version - fetched from Electron preload on mount
  const [appVersion, setAppVersion] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    ipc.getAppVersion()
      .then((v) => { if (!cancelled) setAppVersion(v); })
      .catch(() => { /* non-fatal - running outside Electron */ });
    return () => { cancelled = true; };
  }, []);

  // Show the selected chat destination, including household and circle scope.
  // Receiving an answer and offering compute are independent controls.
  const savedRoutingScope = useChatStore((s) => s.routingScope);
  const localUseEnabled = useUiPrefsStore((s) => s.localUseEnabled);
  const routingScope = (isSignedIn || hasCachedAccount) && !localUseEnabled ? savedRoutingScope : 'local';

  return (
    <footer
      className={cn(
        'flex h-8 shrink-0 items-center gap-3 overflow-x-auto border-t border-border bg-card px-4',
        'text-xs text-muted-foreground select-none',
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
        label={sidecarUp ? 'Local engine ready' : 'Local engine offline'}
        tooltip={sidecarUp
          ? 'Local Python backend is responding on 127.0.0.1:8321.'
          : 'Local Python backend is unreachable.'}
      />

      <MeshPill />

      <Divider />

      <NetworkPill scope={routingScope} />

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

      {/* Right-aligned: dev/env indicator (dev builds only) + app version */}
      <div className="ml-auto flex items-center gap-3">
        <DevBadge />
        {appVersion !== null && (
          <span className="font-mono text-xs text-muted-foreground">
            v{appVersion}
          </span>
        )}
      </div>
    </footer>
  );
}

// ---------------------------------------------------------------------------
// StatusItem - sage dot + label, optionally a router link
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
// NetworkPill - agent-neutral framing
// ---------------------------------------------------------------------------

function NetworkPill({ scope }: { scope: RoutingScope }) {
  const labels: Record<RoutingScope, string> = {
    local: 'Chat: This machine', household: 'Chat: My machines',
    circle: 'Chat: Trusted circle', cooperative: 'Chat: Open pool',
  };
  const tips: Record<RoutingScope, string> = {
    local: 'Your next chat message runs on this machine. Change scope in Chat.',
    household: 'Your next chat message routes to one of your own machines. Change scope in Chat.',
    circle: 'Your next chat message routes through your selected trusted circle. Change scope in Chat.',
    cooperative: 'Your next chat message routes through the open pool. Change scope in Chat.',
  };
  const label = labels[scope];
  const tooltip = tips[scope];
  const local = scope === 'local';

  // Scope changes belong beside the message the member is about to send.
  return (
    <Link
      to="/chat"
      className={cn(
        'flex items-center gap-1.5 rounded-full border px-2 py-0.5',
        'text-xs font-medium transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        local
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
          local ? 'bg-muted-foreground/50' : 'bg-sage animate-hearth-pulse',
        )}
        aria-hidden="true"
      />
      {label}
    </Link>
  );
}

// ---------------------------------------------------------------------------
// MeshPill - aggregate mesh-health heartbeat, app-wide
// ---------------------------------------------------------------------------

/**
 * Surfaces the overall mesh diagnostics (from deriveDiagnostics) as a single
 * status entry - calm sage when healthy, ember/amber/red when something needs
 * attention. Links to My Machines for the full per-peer breakdown.
 *
 * Privacy: aggregate-only. The headline + global diagnostics are count-based
 * and carry no per-peer identity (per-peer reasons live behind the click on
 * /household, never here). Hidden entirely until this machine is enrolled -
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

// ---------------------------------------------------------------------------
// DevBadge - non-prod build indicator (so a dev window is never mistaken for
// packaged prod, and it's always clear which cloud API / coordinator a dev
// session is actually talking to). Renders nothing in a packaged prod build.
// ---------------------------------------------------------------------------

/** Shape written by the Electron main process at build time, if present.
 *  Read defensively everywhere below - this may not exist yet. */
interface BuildInfo {
  sha?: string;
  builtAt?: string;
  packaged?: boolean;
  dirty?: boolean;
}

/** Best-effort host-only display of a URL; falls back to the raw string
 *  (e.g. an unparsable localStorage override) rather than throwing. */
function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function DevBadge() {
  const coordinatorUrl = useConnectionStore((s) => s.coordinatorUrl);
  const buildInfo = (window as unknown as { potluck?: { app?: { buildInfo?: BuildInfo } } })
    .potluck?.app?.buildInfo;

  // Vite's compile-time DEV flag is the primary signal (always false in a
  // packaged prod build); buildInfo.packaged === false backs it up once the
  // main process starts stamping it.
  const isDev = import.meta.env.DEV || buildInfo?.packaged === false;
  if (!isDev) return null;

  const cloudBase =
    (import.meta.env['VITE_CLOUD_API_URL'] as string | undefined) ?? 'https://api.trypotluck.ai';

  const stamp = buildInfo?.sha
    ? `${buildInfo.sha.slice(0, 7)}${buildInfo.dirty ? '+dirty' : ''}` +
      (buildInfo.builtAt ? ` built ${new Date(buildInfo.builtAt).toLocaleString()}` : '')
    : null;

  const tooltip = [
    'Mode: dev',
    `Cloud API: ${cloudBase}`,
    `Coordinator: ${coordinatorUrl}`,
    stamp ? `Build: ${stamp}` : null,
  ]
    .filter((line): line is string => line !== null)
    .join('\n');

  return (
    <span
      className={cn(
        'rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5',
        'font-mono text-xs font-medium text-amber-700 dark:text-amber-300',
      )}
      title={tooltip}
      aria-label={tooltip}
    >
      DEV · {hostOf(cloudBase)}
    </span>
  );
}

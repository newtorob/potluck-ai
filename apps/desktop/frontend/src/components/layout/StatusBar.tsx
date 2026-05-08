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

import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { cn } from '@/components/ui/cn';
import { useHealth } from '@/hooks/useHealth';
import { ipc } from '@/lib/ipc';

export function StatusBar() {
  const { modelLoaded, activeModelId } = useHealth();

  // App version — fetched from Electron preload on mount
  const [appVersion, setAppVersion] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    ipc.getAppVersion()
      .then((v) => { if (!cancelled) setAppVersion(v); })
      .catch(() => { /* non-fatal — running outside Electron */ });
    return () => { cancelled = true; };
  }, []);

  // MCP server status placeholder — wired to real /health.mcp_running once
  // the sidecar exposes it. For now we infer "up" from sidecar reachability
  // via useHealth's activeModelId !== undefined behaviour: any successful
  // response means the local API is responding.
  const sidecarUp = activeModelId !== null || modelLoaded;

  // Network mode is agent-neutral and v0 is always Off until /network ships.
  // Memory: per-session network opt-in, never global. So this is a status
  // pill, not a toggle — the toggle lives in the chat composer per-prompt.
  const networkMode: 'off' | 'per-session' | 'standby' = 'off';

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
        label={modelLoaded ? (activeModelId ?? 'Model loaded') : 'No model loaded'}
        tooltip={modelLoaded ? 'A local model is loaded and ready.' : 'No model loaded — pick one in Models.'}
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

      <Divider />

      <NetworkPill mode={networkMode} />

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
    : 'Network: Per-session';

  const tooltip =
    mode === 'off'      ? 'Nothing leaves your machine. Enable per-prompt in chat.'
    : mode === 'standby' ? 'Network ready but inactive — opt in per session.'
    : 'A request in this session is using the network anonymously, by your choice.';

  return (
    <Link
      to="/settings#privacy"
      className={cn(
        'flex items-center gap-1.5 rounded-full border border-border/60 bg-muted/40 px-2 py-0.5',
        'text-[10px] font-medium text-muted-foreground',
        'hover:text-foreground hover:border-border transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
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

function Divider() {
  return <span className="h-3 w-px bg-border" aria-hidden="true" />;
}

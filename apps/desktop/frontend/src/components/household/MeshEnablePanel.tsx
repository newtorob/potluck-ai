/**
 * MeshEnablePanel — the "app brings up the mesh" onboarding flow for the
 * Household view. Replaces the old dead-end "sidecar isn't running,
 * reinstall" message with the real enable → approve → running sequence,
 * backed by window.potluck.mesh.{daemonStatus,enable,openLoginItems}
 * (main.js's SMAppService bridge on macOS today; a plain "not available
 * yet" message on other platforms, which aren't wired up).
 *
 * This component doesn't poll on its own — meshStore already refreshes
 * daemon status on its normal 2s cadence whenever the mesh socket isn't
 * reachable (see refineBridgeOnUnreachable in stores/meshStore.ts). Button
 * handlers here just trigger one immediate refresh via `onChanged` so the
 * UI doesn't sit and wait out the rest of the current tick.
 *
 * Once the daemon reaches 'running', the socket answers and HouseholdView's
 * bridge flips straight to 'reachable' — this panel stops rendering and the
 * normal unpaired/paired views (BeFirstPanel, JoinExistingPanel,
 * AddMachineWizard) take over with no extra wiring needed here.
 */

import { useState } from 'react';
import { Power, ShieldCheck, ExternalLink, Loader2, WifiOff, RotateCw, AlertTriangle } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { EmptyState } from '@/components/layout/EmptyState';
import { meshDaemon } from '@/lib/meshClient';
import type { MeshDaemonStatus } from '@/types/mesh';
import type { BridgeReachability } from '@/stores/meshStore';

type DaemonBridgeState = Extract<
  BridgeReachability,
  | 'daemon_not_installed'
  | 'daemon_requires_approval'
  | 'daemon_starting'
  | 'daemon_failed'
  | 'daemon_unsupported'
>;

interface MeshEnablePanelProps {
  bridge: DaemonBridgeState;
  daemonStatus: MeshDaemonStatus | null;
  /** Called after a button action so the caller can trigger one immediate
   *  store refresh instead of waiting for the next 2s poll tick. */
  onChanged: () => void;
}

const buttonClass = cn(
  'inline-flex h-9 items-center justify-center gap-1.5 rounded-lg px-4',
  'bg-primary text-primary-foreground text-sm font-medium',
  'hover:bg-primary/90 transition-colors',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
  'disabled:cursor-not-allowed disabled:opacity-60',
);

export function MeshEnablePanel({ bridge, daemonStatus, onChanged }: MeshEnablePanelProps) {
  if (bridge === 'daemon_starting') {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 py-12 text-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-hidden="true" />
        <p className="text-sm text-muted-foreground">Starting mesh networking…</p>
      </div>
    );
  }

  if (bridge === 'daemon_unsupported') {
    const platformLabel =
      daemonStatus?.platform === 'win32'
        ? 'Windows'
        : daemonStatus?.platform === 'linux'
          ? 'Linux'
          : 'this platform';
    return (
      <EmptyState
        icon={WifiOff}
        title="Mesh networking isn't available on this platform yet"
        description={`Mesh networking works on macOS today. ${platformLabel} support is on the way.`}
      />
    );
  }

  if (bridge === 'daemon_requires_approval') {
    return <RequiresApprovalPanel onChanged={onChanged} />;
  }

  if (bridge === 'daemon_failed') {
    return <FailedPanel detail={daemonStatus?.detail} onChanged={onChanged} />;
  }

  return <NotInstalledPanel platform={daemonStatus?.platform} onChanged={onChanged} />;
}

// ---------------------------------------------------------------------------
// failed — the service is installed but crashed / is crash-looping. Give the
// user a Retry (re-runs the same enable/install path) instead of leaving them
// on an indefinite "Starting…" spinner with no way out.
// ---------------------------------------------------------------------------

function FailedPanel({ detail, onChanged }: { detail?: string; onChanged: () => void }) {
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleRetry() {
    setError(null);
    setWorking(true);
    try {
      const result = await meshDaemon.enable();
      if (result.state === 'not_installed' || result.state === 'failed') {
        setError(result.detail ?? 'Mesh networking could not start. Please try again.');
      }
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Mesh networking could not start.');
    } finally {
      setWorking(false);
    }
  }

  return (
    <EmptyState
      icon={AlertTriangle}
      title="Mesh networking didn't start"
      description="The background mesh service is installed but couldn't start. Retrying reinstalls and restarts it, so it usually clears on the next try."
      primaryAction={
        <button type="button" onClick={() => void handleRetry()} disabled={working} className={buttonClass}>
          {working ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Retrying…
            </>
          ) : (
            <>
              <RotateCw className="h-3.5 w-3.5" />
              Retry
            </>
          )}
        </button>
      }
      secondaryAction={
        error !== null ? (
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
        ) : detail ? (
          <p className="text-xs text-muted-foreground">{detail}</p>
        ) : undefined
      }
    />
  );
}

// ---------------------------------------------------------------------------
// not_installed
// ---------------------------------------------------------------------------

/** Platform-specific wording for the one-time approval prompt the user will
 *  see when they click "Enable." macOS asks via System Settings > Login
 *  Items (a separate approval step — see RequiresApprovalPanel below);
 *  Linux asks via a single polkit/pkexec password prompt with no separate
 *  approval step. Falls back to generic wording if platform is unknown
 *  (daemonStatus hasn't loaded yet). */
function approvalPromptCopy(platform: string | undefined): string {
  if (platform === 'linux') {
    return "This installs a small background service so your machines can reach each other. You'll get one authentication prompt the first time.";
  }
  if (platform === 'darwin') {
    return "This installs a small background service so your machines can reach each other. You'll get one approval prompt from macOS the first time.";
  }
  return 'This installs a small background service so your machines can reach each other. You’ll get one approval prompt the first time.';
}

function NotInstalledPanel({ platform, onChanged }: { platform?: string; onChanged: () => void }) {
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleEnable() {
    setError(null);
    setWorking(true);
    try {
      const result = await meshDaemon.enable();
      if (result.state === 'not_installed') {
        setError(result.detail ?? 'Could not enable mesh networking.');
      }
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not enable mesh networking.');
    } finally {
      setWorking(false);
    }
  }

  return (
    <EmptyState
      icon={Power}
      title="Turn on mesh networking"
      description={approvalPromptCopy(platform)}
      primaryAction={
        <button type="button" onClick={() => void handleEnable()} disabled={working} className={buttonClass}>
          {working ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Enabling…
            </>
          ) : (
            <>
              <Power className="h-3.5 w-3.5" />
              Enable mesh networking
            </>
          )}
        </button>
      }
      secondaryAction={
        error !== null ? (
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
        ) : undefined
      }
    />
  );
}

// ---------------------------------------------------------------------------
// requires_approval
// ---------------------------------------------------------------------------

function RequiresApprovalPanel({ onChanged }: { onChanged: () => void }) {
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleOpenSettings() {
    setError(null);
    setWorking(true);
    try {
      await meshDaemon.openLoginItems();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not open System Settings.');
    } finally {
      setWorking(false);
    }
  }

  return (
    <EmptyState
      icon={ShieldCheck}
      title="Approve the mesh service"
      description={
        <>
          macOS needs your OK before the mesh service can run. Open{' '}
          <span className="font-medium text-foreground">
            System Settings → General → Login Items &amp; Extensions
          </span>{' '}
          and turn on "Potluck Mesh".
        </>
      }
      primaryAction={
        <button
          type="button"
          onClick={() => void handleOpenSettings()}
          disabled={working}
          className={buttonClass}
        >
          {working ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Opening…
            </>
          ) : (
            <>
              <ExternalLink className="h-3.5 w-3.5" />
              Open System Settings
            </>
          )}
        </button>
      }
      secondaryAction={
        error !== null ? (
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
        ) : (
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
            Checking automatically once approved…
          </p>
        )
      }
    />
  );
}

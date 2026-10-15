import { hasAccountAccess, useAuthStore } from '@/stores/authStore';
/**
 * ConnectStep - step 5 of the First Run wizard ("Your machines").
 *
 * Three sections, top to bottom:
 *   1. Coordinator - confirm what the sidecar is connected to (read-only;
 *      editing lives in Settings → Advanced).
 *   2. Your machines - mesh status, this machine's mesh IP, peer count, and
 *      a link to /household to add more machines.
 *   3. Contribute - opt-in toggle for contributor_mode (share spare compute
 *      with the open pool). Wired to the real NetworkSettings boolean via the
 *      settingsStore. Default off, matching the sidecar default.
 *
 * Graceful degradation: when the mesh bridge is missing or the sidecar is
 * offline, each section degrades to a calm "set this up later" state. No
 * error walls.
 */

import { useEffect } from 'react';
import { Monitor, Radio, Cpu } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { cn } from '@/components/ui/cn';
import { useMeshStore } from '@/stores/meshStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { registerContributorDeviceKey } from '@/lib/deviceKey';

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ConnectStep() {
  const accountAvailable = useAuthStore(hasAccountAccess);
  const status = useMeshStore((s) => s.status);
  const peers = useMeshStore((s) => s.peers);
  const bridge = useMeshStore((s) => s.bridge);
  const start = useMeshStore((s) => s.start);

  const settings = useSettingsStore((s) => s.settings);
  const isSaving = useSettingsStore((s) => s.isSaving);
  const updateSettings = useSettingsStore((s) => s.update);

  const navigate = useNavigate();

  // Kick the mesh store if it hasn't been started yet (app-wide start in
  // App.tsx is idempotent, so this is a safe no-op in production).
  useEffect(() => {
    if (accountAvailable) start();
  }, [start, accountAvailable]);

  const isPaired = status !== null && status.node_id !== null && status.mesh_ip !== null;
  const meshUp = status?.mesh_status === 'running';
  const peerCount = peers.length;

  const contributorMode = settings?.network.contributor_mode ?? false;

  async function handleContributorToggle(next: boolean) {
    if (next && !hasAccountAccess(useAuthStore.getState())) return;
    try {
      await updateSettings({ network: { contributor_mode: next } });
    } catch {
      // Rollback is handled by the store; nothing extra needed here.
      return;
    }
    if (next) {
      // Pre-register this machine's contributor device key with the
      // coordinator so attestation isn't rejected by REQUIRE_REGISTERED_KEYS
      // (see lib/deviceKey.ts). Best-effort: a coordinator that's
      // unreachable here just means the machine will fail attestation
      // later, same as before this wiring existed - don't block the toggle
      // or roll it back over this.
      try {
        await registerContributorDeviceKey();
      } catch (err) {
        console.warn('[ConnectStep] device key registration failed:', err);
      }
    }
  }

  if (!accountAvailable) return (
    <div className="space-y-3 animate-fade-in">
      <h2 className="font-serif text-2xl tracking-display text-foreground">Start on this machine</h2>
      <p className="text-sm leading-relaxed text-muted-foreground">No account is needed for local chat or model management. Continue on this machine now, and sign in later from Settings → Account to connect other machines or use the network.</p>
      <p className="text-sm leading-relaxed text-muted-foreground">Signing in later keeps your local chats and settings.</p>
    </div>
  );

  return (
    <div className="space-y-5 animate-fade-in">
      <div className="space-y-1.5">
        <h2 className="font-serif text-2xl tracking-display leading-tight text-foreground">
          Your machines
        </h2>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Potluck works locally by default. Connecting your other machines
          lets you run larger models and share load across them.
        </p>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* 1. Coordinator                                                       */}
      {/* ------------------------------------------------------------------ */}
      <Section
        icon={<Radio className="h-4 w-4" aria-hidden="true" />}
        label="Coordinator"
      >
        {status === null || bridge === 'bridge_missing' || bridge === 'sidecar_offline' ? (
          <p className="text-sm text-muted-foreground">
            {bridge === 'bridge_missing'
              ? 'Mesh is only available in the desktop app.'
              : 'Mesh sidecar is not running. You can set this up later in Settings.'}
          </p>
        ) : (
          <div className="flex items-start gap-2.5">
            <span
              className={cn(
                'mt-1.5 inline-block h-2 w-2 shrink-0 rounded-full',
                status.coordinator_connection === 'connected'
                  ? 'bg-sage animate-hearth-pulse'
                  : status.coordinator_connection === 'connecting'
                    ? 'bg-amber-400 animate-hearth-pulse'
                    : 'bg-muted-foreground/40',
              )}
              aria-hidden="true"
            />
            <div className="min-w-0">
              <p className="text-sm text-foreground">
                {status.coordinator_connection === 'connected'
                  ? 'Connected to your coordinator'
                  : status.coordinator_connection === 'connecting'
                    ? 'Connecting to your coordinator…'
                    : 'Coordinator unreachable'}
              </p>
              <p className="mt-0.5 font-mono text-[11px] text-muted-foreground/80 truncate">
                {status.coordinator_url}
              </p>
              <p className="mt-1 text-[11px] text-muted-foreground/60">
                To change this URL, go to{' '}
                <span className="text-muted-foreground">Settings &rarr; Advanced</span>.
              </p>
            </div>
          </div>
        )}
      </Section>

      {/* ------------------------------------------------------------------ */}
      {/* 2. Your machines                                                     */}
      {/* ------------------------------------------------------------------ */}
      <Section
        icon={<Monitor className="h-4 w-4" aria-hidden="true" />}
        label="Your machines"
      >
        {bridge === 'bridge_missing' || bridge === 'sidecar_offline' || status === null ? (
          <p className="text-sm text-muted-foreground">
            Set up your machine network later from{' '}
            <button
              type="button"
              onClick={() => void navigate('/household')}
              className="underline underline-offset-2 hover:text-foreground transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm"
            >
              My Machines
            </button>
            .
          </p>
        ) : (
          <div className="space-y-2.5">
            <div className="flex items-center gap-2">
              <span
                className={cn(
                  'inline-block h-2 w-2 shrink-0 rounded-full',
                  meshUp
                    ? 'bg-sage animate-hearth-pulse'
                    : 'bg-muted-foreground/40',
                )}
                aria-hidden="true"
              />
              <span className="text-sm text-foreground">
                {meshUp ? 'Mesh running' : isPaired ? 'Mesh not running' : 'Not yet connected to mesh'}
              </span>
              {status.mesh_ip !== null && (
                <span className="font-mono text-[11px] text-muted-foreground/70">
                  {status.mesh_ip}
                </span>
              )}
            </div>

            {isPaired && (
              <p className="text-sm text-muted-foreground">
                {peerCount === 0
                  ? 'Just this machine right now.'
                  : `${peerCount + 1} machine${peerCount + 1 === 1 ? '' : 's'} in your household.`}
              </p>
            )}

            <button
              type="button"
              onClick={() => void navigate('/household')}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-lg border border-border/70 bg-card',
                'px-3 py-1.5 text-xs font-medium text-foreground',
                'hover:bg-accent hover:border-primary/30 transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              )}
            >
              {isPaired && peerCount === 0 ? 'Add another machine' : isPaired ? 'Manage machines' : 'Set up My Machines'}
            </button>
          </div>
        )}
      </Section>

      {/* ------------------------------------------------------------------ */}
      {/* 3. Contribute                                                        */}
      {/* ------------------------------------------------------------------ */}
      <Section
        icon={<Cpu className="h-4 w-4" aria-hidden="true" />}
        label="Contribute compute"
        optional
      >
        {settings === null ? (
          <p className="text-sm text-muted-foreground">
            You can turn this on later in Settings.
          </p>
        ) : (
          <label className="flex items-start gap-3 cursor-pointer select-none">
            <div className="flex-1 space-y-0.5">
              <p className="text-sm text-foreground">
                Share spare compute with the network
              </p>
              <p className="text-xs leading-relaxed text-muted-foreground">
                While on, this machine can answer requests from your other
                machines using its loaded model. Sharing it with a trusted
                circle is a separate choice under Network. Off by default.
              </p>
            </div>
            <input
              type="checkbox"
              role="switch"
              aria-label="Share spare compute with the network"
              checked={contributorMode}
              disabled={isSaving}
              onChange={(e) => void handleContributorToggle(e.target.checked)}
              className="mt-0.5 h-4 w-4 shrink-0 accent-primary disabled:cursor-not-allowed disabled:opacity-60"
            />
          </label>
        )}
      </Section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section wrapper
// ---------------------------------------------------------------------------

function Section({
  icon,
  label,
  optional = false,
  children,
}: {
  icon: React.ReactNode;
  label: string;
  optional?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-border/70 bg-card px-4 py-3.5 space-y-2.5">
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground">{icon}</span>
        <span className="font-serif text-[13px] tracking-display text-foreground font-medium">
          {label}
        </span>
        {optional && (
          <span className="ml-auto text-[11px] italic text-muted-foreground/60">
            optional
          </span>
        )}
      </div>
      <div>{children}</div>
    </div>
  );
}

/**
 * ReadinessView - "/readiness" - the netcheck.
 *
 * One glance answers two questions the two-machine bring-up made us answer by
 * hand over SSH: (1) can this machine run inference locally, and (2) is it
 * reachable on the mesh - each gap named with a fix.
 *
 * Composes signals from useHealth, useHardware, meshStore + deriveDiagnostics.
 * Two passive network probes (VPN conflict, WG port) are now fed from the mesh
 * status poll. The GPU verify button runs a real device op on demand via
 * GET /readiness/gpu-test - never auto-runs on mount.
 */

import { useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import {
  CheckCircle2,
  Info,
  AlertTriangle,
  XCircle,
  RefreshCw,
  Cpu,
  Network,
  Loader2,
  type LucideIcon,
} from 'lucide-react';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { cn } from '@/components/ui/cn';
import { useHealth } from '@/hooks/useHealth';
import { useHardware } from '@/hooks/useHardware';
import { useMeshStore } from '@/stores/meshStore';
import { deriveDiagnostics, worstSeverity, type DiagSeverity } from '@/lib/meshDiagnostics';
import {
  deriveReadiness,
  type ReadinessCheck,
  type ReadinessReport,
} from '@/lib/readiness';
import { localApi, type GpuTestResult } from '@/lib/localApi';

// ---------------------------------------------------------------------------
// Severity presentation
// ---------------------------------------------------------------------------

const SEV: Record<
  DiagSeverity,
  { icon: LucideIcon; tone: string; chip: string; ring: string }
> = {
  ok: {
    icon: CheckCircle2,
    tone: 'text-sage',
    chip: 'bg-sage/10 text-sage',
    ring: 'ring-sage/20',
  },
  info: {
    icon: Info,
    tone: 'text-muted-foreground',
    chip: 'bg-muted text-muted-foreground',
    ring: 'ring-border',
  },
  warn: {
    icon: AlertTriangle,
    tone: 'text-amber-600 dark:text-amber-400',
    chip: 'bg-amber-500/10 text-amber-700 dark:text-amber-300',
    ring: 'ring-amber-500/20',
  },
  error: {
    icon: XCircle,
    tone: 'text-red-600 dark:text-red-400',
    chip: 'bg-red-500/10 text-red-700 dark:text-red-300',
    ring: 'ring-red-500/20',
  },
};

// ---------------------------------------------------------------------------
// Rows
// ---------------------------------------------------------------------------

function CheckRow({ check, footer }: { check: ReadinessCheck; footer?: ReactNode }) {
  const sev = SEV[check.severity];
  const Icon = sev.icon;
  return (
    <li>
      <div className="flex items-start gap-3 px-4 py-3">
        <Icon className={cn('mt-0.5 size-4 shrink-0', sev.tone)} aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-foreground">{check.title}</p>
          <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">{check.detail}</p>
          {check.fix && (
            <p className="mt-1 text-xs text-foreground/80">
              <span className="text-muted-foreground">Fix: </span>
              {check.fix}
            </p>
          )}
        </div>
      </div>
      {footer}
    </li>
  );
}

function Group({
  title,
  icon: Icon,
  checks,
}: {
  title: string;
  icon: LucideIcon;
  checks: ReadinessCheck[];
}) {
  if (checks.length === 0) return null;
  return (
    <section className="overflow-hidden rounded-xl border border-border bg-card">
      <header className="flex items-center gap-2 border-b border-border px-4 py-2.5">
        <Icon className="size-3.5 text-muted-foreground" aria-hidden />
        <h2 className="font-sans text-xs font-medium text-muted-foreground">
          {title}
        </h2>
      </header>
      <ul className="divide-y divide-border">
        {checks.map((c) => (
          <CheckRow key={c.code} check={c} />
        ))}
      </ul>
    </section>
  );
}

// ---------------------------------------------------------------------------
// GPU verify state
// ---------------------------------------------------------------------------

type GpuVerifyState =
  | { phase: 'idle' }
  | { phase: 'running' }
  | { phase: 'done'; result: GpuTestResult }
  | { phase: 'error'; message: string };

// ---------------------------------------------------------------------------
// View
// ---------------------------------------------------------------------------

export function ReadinessView() {
  const { sidecarUp, modelLoaded, activeModelId, nCtxLoaded } = useHealth();
  const { hardware, isLoading: hwLoading, refresh: refreshHardware } = useHardware();
  const status = useMeshStore((s) => s.status);
  const peers = useMeshStore((s) => s.peers);
  const bridge = useMeshStore((s) => s.bridge);
  const refreshMesh = useMeshStore((s) => s.refresh);

  const [gpuVerify, setGpuVerify] = useState<GpuVerifyState>({ phase: 'idle' });

  const report: ReadinessReport = useMemo(() => {
    const mesh = deriveDiagnostics({ bridge, status, peers });
    // exactOptionalPropertyTypes: spread optional fields only when the signal
    // is present - passing `prop: undefined` would fail the strict type check.
    // VPN/port are derived inline here so useMemo's dep array stays stable
    // (no new-object intermediary consts that reconstruct on every render).
    const vpnConflict =
      status !== null
        ? { conflict: status.vpn_conflict, detail: status.vpn_detail }
        : undefined;
    const wgPortListening = status !== null ? status.wg_port_listening : undefined;
    return deriveReadiness({
      sidecarUp,
      modelLoaded,
      activeModelId,
      nCtxLoaded,
      hardware,
      enrolled: !!status?.node_id,
      mesh,
      peers,
      ...(vpnConflict !== undefined ? { vpnConflict } : {}),
      ...(wgPortListening !== undefined ? { wgPortListening } : {}),
    });
  }, [bridge, status, peers, sidecarUp, modelLoaded, activeModelId, nCtxLoaded, hardware]);

  const machineChecks = report.checks.filter((c) => c.group === 'machine');
  const networkChecks = report.checks.filter((c) => c.group === 'network');
  // The banner answers "is this machine ready", so it tracks machine severity
  // only, never the optional network. A relayed or unreachable peer (amber/red
  // below) must not tint the headline when local inference is fine.
  const machineSeverity = machineChecks.reduce<DiagSeverity>(
    (worst, c) => worstSeverity(worst, c.severity),
    'ok',
  );
  const banner = SEV[report.machineReady ? 'ok' : machineSeverity];
  const BannerIcon = banner.icon;

  const onRecheck = () => {
    refreshHardware();
    void refreshMesh();
  };

  const onVerifyGpu = () => {
    if (gpuVerify.phase === 'running') return;
    setGpuVerify({ phase: 'running' });
    localApi.gpuTest().then(
      (result) => { setGpuVerify({ phase: 'done', result }); },
      (err: unknown) => {
        setGpuVerify({
          phase: 'error',
          message: err instanceof Error ? err.message : 'GPU test failed.',
        });
      },
    );
  };

  return (
    <div className="flex h-full flex-col">
      <ViewHeader
        eyebrow="Netcheck"
        title="Machine readiness"
        description="Whether this machine can run locally, and whether it's reachable on your mesh."
        actions={
          <button
            type="button"
            onClick={onRecheck}
            disabled={hwLoading}
            aria-busy={hwLoading}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs text-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <RefreshCw className={cn('size-3.5', hwLoading && 'animate-spin')} aria-hidden />
            Re-check
          </button>
        }
      />

      <div className="flex-1 overflow-y-auto px-6 py-5">
        <div className="mx-auto flex max-w-2xl flex-col gap-5">
          {/* Overall banner */}
          <div
            className={cn(
              'flex items-center gap-3 rounded-xl px-4 py-3.5 ring-1',
              banner.chip,
              banner.ring,
            )}
          >
            <BannerIcon className={cn('size-5 shrink-0', banner.tone)} aria-hidden />
            <div className="min-w-0">
              <p className="font-serif text-lg leading-tight tracking-display text-foreground">
                {report.headline}
              </p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {report.machineReady
                  ? 'Local inference is good to go. Network is optional.'
                  : 'Resolve the machine checks below to run inference here.'}
              </p>
            </div>
          </div>

          {/* Machine group - GPU verify button is injected after the HARDWARE row */}
          <MachineGroup checks={machineChecks} gpuVerify={gpuVerify} onVerifyGpu={onVerifyGpu} />
          <Group title="Network" icon={Network} checks={networkChecks} />

          <p className="px-1 text-xs leading-relaxed text-muted-foreground">
            Readiness is computed from local signals only: model name, hardware, and
            peer reachability. No prompt or inference content is read. Manage machines
            in{' '}
            <Link to="/household" className="underline underline-offset-2 hover:text-foreground">
              My Machines
            </Link>
            .
          </p>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Machine group - wraps the standard Group but inserts the GPU verify widget
// after the HARDWARE check row.
// ---------------------------------------------------------------------------

function MachineGroup({
  checks,
  gpuVerify,
  onVerifyGpu,
}: {
  checks: ReadinessCheck[];
  gpuVerify: GpuVerifyState;
  onVerifyGpu: () => void;
}) {
  if (checks.length === 0) return null;
  return (
    <section className="overflow-hidden rounded-xl border border-border bg-card">
      <header className="flex items-center gap-2 border-b border-border px-4 py-2.5">
        <Cpu className="size-3.5 text-muted-foreground" aria-hidden />
        <h2 className="font-sans text-xs font-medium text-muted-foreground">
          This machine
        </h2>
      </header>
      <ul className="divide-y divide-border">
        {checks.map((c) => (
          <CheckRow
            key={c.code}
            check={c}
            footer={
              c.code === 'HARDWARE' ? (
                <GpuVerifyWidget state={gpuVerify} onVerify={onVerifyGpu} />
              ) : undefined
            }
          />
        ))}
      </ul>
    </section>
  );
}

// ---------------------------------------------------------------------------
// GPU verify widget - rendered inside the HARDWARE list item, below the row.
// ---------------------------------------------------------------------------

function GpuVerifyWidget({
  state,
  onVerify,
}: {
  state: GpuVerifyState;
  onVerify: () => void;
}) {
  return (
    <div className="border-t border-border/50 bg-muted/30 px-4 py-2.5">
      {state.phase === 'idle' && (
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onVerify}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1 text-xs text-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Verify GPU
          </button>
          <span className="text-xs text-muted-foreground">
            Runs a real compute op to confirm the GPU or runtime works.
          </span>
        </div>
      )}

      {state.phase === 'running' && (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="size-3.5 animate-spin" aria-hidden />
          Running GPU test...
        </div>
      )}

      {state.phase === 'done' && (
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1">
            {state.result.ok ? (
              <p className="text-xs text-emerald-700 dark:text-emerald-300">
                <span className="font-medium">GPU verified:</span>{' '}
                {state.result.device}, ran a real {state.result.ran} in{' '}
                {state.result.latency_ms.toLocaleString()}ms.
              </p>
            ) : (
              <>
                <p className="text-xs font-medium text-amber-700 dark:text-amber-300">
                  GPU test did not succeed
                </p>
                <p className="mt-0.5 text-xs text-muted-foreground">{state.result.detail}</p>
              </>
            )}
          </div>
          <button
            type="button"
            onClick={onVerify}
            className="shrink-0 text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground focus-visible:outline-none"
          >
            Re-run
          </button>
        </div>
      )}

      {state.phase === 'error' && (
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <p className="text-xs font-medium text-red-700 dark:text-red-300">
              GPU test could not run
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">{state.message}</p>
            <p className="mt-1 text-xs text-foreground/80">
              <span className="text-muted-foreground">Fix: </span>
              The sidecar may not support this probe yet. Make sure Potluck is up to date.
            </p>
          </div>
          <button
            type="button"
            onClick={onVerify}
            className="shrink-0 text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground focus-visible:outline-none"
          >
            Re-run
          </button>
        </div>
      )}
    </div>
  );
}

export default ReadinessView;

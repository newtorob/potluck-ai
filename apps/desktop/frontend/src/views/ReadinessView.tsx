/**
 * ReadinessView — "/readiness" — the netcheck.
 *
 * One glance answers two questions the two-machine bring-up made us answer by
 * hand over SSH: (1) can this machine run inference locally, and (2) is it
 * reachable on the mesh — each gap named with a fix.
 *
 * Pure composition of signals already polled elsewhere (useHealth, useHardware,
 * meshStore + deriveDiagnostics). No new backend. Deeper probes (real GPU test,
 * port scan, VPN-conflict probe, token validity) are a tracked follow-on.
 */

import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import {
  CheckCircle2,
  Info,
  AlertTriangle,
  XCircle,
  RefreshCw,
  Cpu,
  Network,
  type LucideIcon,
} from 'lucide-react';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { cn } from '@/components/ui/cn';
import { useHealth } from '@/hooks/useHealth';
import { useHardware } from '@/hooks/useHardware';
import { useMeshStore } from '@/stores/meshStore';
import { deriveDiagnostics, type DiagSeverity } from '@/lib/meshDiagnostics';
import {
  deriveReadiness,
  type ReadinessCheck,
  type ReadinessReport,
} from '@/lib/readiness';

// ---------------------------------------------------------------------------
// Severity presentation
// ---------------------------------------------------------------------------

const SEV: Record<
  DiagSeverity,
  { icon: LucideIcon; tone: string; chip: string; ring: string }
> = {
  ok: {
    icon: CheckCircle2,
    tone: 'text-emerald-600 dark:text-emerald-400',
    chip: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
    ring: 'ring-emerald-500/20',
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

function CheckRow({ check }: { check: ReadinessCheck }) {
  const sev = SEV[check.severity];
  const Icon = sev.icon;
  return (
    <li className="flex items-start gap-3 px-4 py-3">
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
        <h2 className="text-[11px] font-medium uppercase tracking-hearth-label text-muted-foreground">
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
// View
// ---------------------------------------------------------------------------

export function ReadinessView() {
  const { sidecarUp, modelLoaded, activeModelId, nCtxLoaded } = useHealth();
  const { hardware, isLoading: hwLoading, refresh: refreshHardware } = useHardware();
  const status = useMeshStore((s) => s.status);
  const peers = useMeshStore((s) => s.peers);
  const bridge = useMeshStore((s) => s.bridge);
  const refreshMesh = useMeshStore((s) => s.refresh);

  const report: ReadinessReport = useMemo(() => {
    const mesh = deriveDiagnostics({ bridge, status, peers });
    return deriveReadiness({
      sidecarUp,
      modelLoaded,
      activeModelId,
      nCtxLoaded,
      hardware,
      enrolled: !!status?.node_id,
      mesh,
      peers,
    });
  }, [bridge, status, peers, sidecarUp, modelLoaded, activeModelId, nCtxLoaded, hardware]);

  const machineChecks = report.checks.filter((c) => c.group === 'machine');
  const networkChecks = report.checks.filter((c) => c.group === 'network');
  // The banner answers "is this machine ready", so it tracks machine readiness,
  // not the optional network. A relayed peer (amber, below) shouldn't tint the
  // headline when local inference is good to go.
  const banner = SEV[report.machineReady ? 'ok' : report.overall];
  const BannerIcon = banner.icon;

  const onRecheck = () => {
    refreshHardware();
    void refreshMesh();
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
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs text-foreground transition-colors hover:bg-muted"
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

          <Group title="This machine" icon={Cpu} checks={machineChecks} />
          <Group title="Network" icon={Network} checks={networkChecks} />

          <p className="px-1 text-[11px] leading-relaxed text-muted-foreground">
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

export default ReadinessView;

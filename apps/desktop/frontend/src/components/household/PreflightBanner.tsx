/**
 * PreflightBanner - the global connectivity health strip for My Machines.
 *
 * Instead of a single "offline", show a named, root-cause-suppressed read on
 * the mesh - local → coordinator → peers.
 * When everything's healthy it's a slim, calm one-liner; when something's
 * wrong it surfaces the specific cause and the fix, worst-first.
 *
 * Flicker control: connectivity flaps on a single poll. We debounce
 * *escalations* (apply a worse state only if it persists ~4s) but apply
 * *improvements* immediately - never nag on a blip, always clear good news fast.
 */

import { useEffect, useRef, useState } from 'react';
import { CheckCircle2, AlertTriangle, XCircle, Loader2 } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import type { DiagnosticsReport, DiagSeverity, Diagnostic } from '@/lib/meshDiagnostics';

const SEVERITY_RANK: Record<DiagSeverity, number> = { ok: 0, info: 1, warn: 2, error: 3 };
const ESCALATION_DEBOUNCE_MS = 4_000;

/**
 * Hold a stable report: apply same-or-better severity immediately, defer a
 * worse severity until it has persisted for the debounce window. A single timer
 * (not reset each poll) guarantees a sustained problem eventually shows.
 */
function useStableReport(report: DiagnosticsReport): DiagnosticsReport {
  const [shown, setShown] = useState<DiagnosticsReport>(report);
  const latest = useRef(report);
  latest.current = report;
  const escTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const worse = SEVERITY_RANK[report.overall] > SEVERITY_RANK[shown.overall];
    if (!worse) {
      if (escTimer.current) {
        clearTimeout(escTimer.current);
        escTimer.current = null;
      }
      setShown(report); // de-escalation or content refresh at same severity
      return;
    }
    // Worse: schedule once; don't reset on subsequent worse polls.
    if (escTimer.current == null) {
      escTimer.current = setTimeout(() => {
        escTimer.current = null;
        setShown(latest.current);
      }, ESCALATION_DEBOUNCE_MS);
    }
  }, [report, shown]);

  useEffect(
    () => () => {
      if (escTimer.current) clearTimeout(escTimer.current);
    },
    [],
  );

  return shown;
}

interface SevVisual {
  icon: typeof CheckCircle2;
  spin?: boolean;
  row: string; // container classes
  iconColor: string;
}

const SEV_VISUAL: Record<DiagSeverity, SevVisual> = {
  ok: {
    icon: CheckCircle2,
    row: 'border-sage/25 bg-sage/[0.06] text-foreground',
    iconColor: 'text-sage',
  },
  info: {
    icon: Loader2,
    spin: true,
    row: 'border-border/60 bg-card text-foreground',
    iconColor: 'text-muted-foreground',
  },
  warn: {
    icon: AlertTriangle,
    row: 'border-amber-500/30 bg-amber-500/[0.07] text-foreground',
    iconColor: 'text-amber-600 dark:text-amber-400',
  },
  error: {
    icon: XCircle,
    row: 'border-destructive/30 bg-destructive/[0.07] text-foreground',
    iconColor: 'text-destructive',
  },
};

export function PreflightBanner({ report }: { report: DiagnosticsReport }) {
  const stable = useStableReport(report);
  const visual = SEV_VISUAL[stable.overall];
  const Icon = visual.icon;

  // Healthy / info → slim single line. Warn/error → expanded with causes+fixes.
  const expanded = stable.overall === 'warn' || stable.overall === 'error';

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'rounded-xl border px-3.5 py-2.5 transition-colors duration-base',
        visual.row,
      )}
    >
      <div className="flex items-center gap-2.5">
        <Icon
          className={cn('h-4 w-4 shrink-0', visual.iconColor, visual.spin && 'animate-spin')}
          aria-hidden="true"
        />
        <span className="text-sm font-medium">{stable.headline}</span>
      </div>

      {expanded && stable.global.length > 0 && (
        <ul className="mt-2 space-y-1.5 pl-6">
          {stable.global
            .filter((d) => d.severity === 'warn' || d.severity === 'error')
            .map((d) => (
              <DiagnosticRow key={d.code} diag={d} />
            ))}
        </ul>
      )}
    </div>
  );
}

function DiagnosticRow({ diag }: { diag: Diagnostic }) {
  return (
    <li className="text-xs leading-snug">
      <span className="font-medium text-foreground">{diag.title}.</span>{' '}
      <span className="text-muted-foreground">{diag.detail}</span>
      {diag.fix && (
        <>
          {' '}
          <span className="text-foreground/80">Fix: {diag.fix}</span>
        </>
      )}
    </li>
  );
}

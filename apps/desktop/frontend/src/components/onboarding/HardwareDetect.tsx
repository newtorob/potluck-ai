/**
 * HardwareDetect — step 2 of the First Run wizard.
 *
 * Fetches hardware from the sidecar and visualises detected specs with
 * a highlighted recommended tier card.
 */

import { Cpu, MemoryStick, MonitorCheck, Loader2, AlertCircle } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import type { HardwareInfo, HardwareTier } from '@/hooks/useHardware';

// ---------------------------------------------------------------------------
// Tier copy
// ---------------------------------------------------------------------------

const TIER_INFO: Record<
  HardwareTier,
  { label: string; description: string; colorClass: string }
> = {
  small: {
    label: 'Small tier',
    description:
      'Lightweight 3B–4B parameter models. Fast on any machine. Great for summarisation, drafting, and Q&A.',
    colorClass: 'border-emerald-500/50 bg-emerald-500/5 text-emerald-600 dark:text-emerald-400',
  },
  medium: {
    label: 'Medium tier',
    description:
      '7B–8B parameter models. Strong reasoning, code completion, and nuanced conversation. The recommended sweet spot.',
    colorClass: 'border-blue-500/50 bg-blue-500/5 text-blue-600 dark:text-blue-400',
  },
  large: {
    label: 'Large tier',
    description:
      '13B+ parameter models. Best quality. Requires a capable GPU or significant RAM. Worth it if your hardware supports it.',
    colorClass: 'border-violet-500/50 bg-violet-500/5 text-violet-600 dark:text-violet-400',
  },
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface HardwareDetectProps {
  hardware: HardwareInfo | null;
  isLoading: boolean;
  error: string | null;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function HardwareDetect({ hardware, isLoading, error }: HardwareDetectProps) {
  if (isLoading) {
    return (
      <div className="flex flex-col items-center gap-3 py-8">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
        <p className="text-sm text-muted-foreground">Detecting your hardware…</p>
      </div>
    );
  }

  if (error || !hardware) {
    return (
      <div className="flex flex-col items-center gap-3 rounded-xl border border-destructive/20 bg-destructive/5 px-6 py-6 text-center">
        <AlertCircle className="h-8 w-8 text-destructive" />
        <p className="text-sm font-medium text-foreground">Hardware detection failed</p>
        <p className="text-xs text-muted-foreground">
          {error ?? 'Could not reach the local sidecar. Make sure the Python backend is running.'}
        </p>
      </div>
    );
  }

  const tier = hardware.recommended_tier;
  const tierInfo = TIER_INFO[tier];

  return (
    <div className="space-y-5">
      {/* Spec cards */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <SpecCard
          icon={<Cpu className="h-4 w-4" />}
          label="CPU"
          value={hardware.cpu_name ?? 'Unknown'}
          sub={
            hardware.cpu_cores_physical !== null
              ? `${hardware.cpu_cores_physical}C / ${hardware.cpu_cores_logical ?? '?'}T`
              : undefined
          }
        />
        <SpecCard
          icon={<MemoryStick className="h-4 w-4" />}
          label="System RAM"
          value={`${hardware.ram_total_gb.toFixed(1)} GB`}
          sub={`${hardware.ram_available_gb.toFixed(1)} GB free`}
        />
        <SpecCard
          icon={<MonitorCheck className="h-4 w-4" />}
          label="GPU"
          value={hardware.gpu_name ?? 'None detected'}
          sub={hardware.gpu_vram_gb !== null ? `${hardware.gpu_vram_gb.toFixed(1)} GB VRAM` : undefined}
        />
        <SpecCard
          icon={<Cpu className="h-4 w-4" />}
          label="Platform"
          value={hardware.os}
          sub={hardware.arch}
        />
      </div>

      {/* Recommended tier callout */}
      <div
        className={cn(
          'rounded-xl border px-5 py-4 space-y-1',
          tierInfo.colorClass,
        )}
      >
        <p className="text-sm font-semibold">{tierInfo.label} recommended</p>
        <p className="text-xs leading-relaxed opacity-90">{tierInfo.description}</p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SpecCard
// ---------------------------------------------------------------------------

function SpecCard({
  icon,
  label,
  value,
  sub,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-border bg-card px-3 py-3">
      <div className="flex items-center gap-1.5 text-muted-foreground">
        {icon}
        <span className="text-xs font-medium">{label}</span>
      </div>
      <p className="text-sm font-semibold text-foreground truncate" title={value}>
        {value}
      </p>
      {sub && <p className="text-xs text-muted-foreground">{sub}</p>}
    </div>
  );
}

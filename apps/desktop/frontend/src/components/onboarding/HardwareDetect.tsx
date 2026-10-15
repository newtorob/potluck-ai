/**
 * HardwareDetect - step 2 of the First Run wizard.
 *
 * Fetches hardware from the sidecar and visualises detected specs with
 * available memory for model selection.
 */

import { Cpu, MemoryStick, MonitorCheck, Loader2, AlertCircle } from 'lucide-react';
import type { HardwareInfo } from '@/hooks/useHardware';

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
          sub={
            hardware.gpu_vram_gb !== null ? `${hardware.gpu_vram_gb.toFixed(1)} GB VRAM` : undefined
          }
        />
        <SpecCard
          icon={<Cpu className="h-4 w-4" />}
          label="Platform"
          value={hardware.os}
          sub={hardware.arch}
        />
      </div>

      {/* GPU-missing warning - surfaces when detection found nothing.
          Keeps onboarding moving (CPU inference works for the small tier)
          but tells the user why their tier might not be what they expected. */}
      {hardware.gpu_name === null && (
        <div className="flex gap-3 rounded-xl border border-amber-500/40 bg-amber-500/5 px-4 py-3">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
          <div className="space-y-1">
            <p className="text-sm font-medium text-foreground">
              No GPU detected. Inference will run on CPU
            </p>
            <p className="text-xs leading-relaxed text-muted-foreground">
              Smaller models are a conservative starting point on CPU. Speed still needs a local
              check. If you have a discrete GPU on Windows or Linux, make sure its driver is
              installed and Potluck is up to date. Detection covers NVIDIA, AMD, Intel, and Apple
              Silicon.
            </p>
          </div>
        </div>
      )}
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
  sub?: string | undefined;
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

/**
 * useHardware — fetches hardware detection results from the Python sidecar.
 *
 * Used by HardwareDetect (onboarding) and SettingsView (hardware panel)
 * to show GPU, VRAM, RAM info and the recommended model tier.
 */

import { useState, useEffect } from 'react';
import { localApi } from '@/lib/localApi';

export type HardwareTier = 'small' | 'medium' | 'large';

export interface HardwareInfo {
  os: string;
  arch: string;
  cpu_name: string;
  cpu_cores_physical: number | null;
  cpu_cores_logical: number | null;
  ram_total_gb: number;
  ram_available_gb: number;
  gpu_name: string | null;
  gpu_vram_gb: number | null;
  /** Engine-probed capacity, separate from a detected physical adapter. */
  inference_gpu_available?: boolean | null;
  inference_gpu_vram_gb?: number | null;
  recommended_tier: HardwareTier;
}

interface UseHardwareReturn {
  hardware: HardwareInfo | null;
  isLoading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useHardware(): UseHardwareReturn {
  const [hardware, setHardware] = useState<HardwareInfo | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);

    localApi
      .detectHardware()
      .then((data) => {
        if (cancelled) return;
        // Cast via unknown — the raw API response shape matches HardwareInfo
        setHardware(data as unknown as HardwareInfo);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Hardware detection failed');
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [tick]);

  return {
    hardware,
    isLoading,
    error,
    refresh: () => setTick((t) => t + 1),
  };
}

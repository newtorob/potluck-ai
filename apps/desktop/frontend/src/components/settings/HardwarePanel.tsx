/**
 * HardwarePanel — runtime inference settings.
 *
 * Sliders/selects for context_size, gpu_layers, threads, temperature, max_tokens.
 * Saves on blur (or debounced for sliders). Changes apply on next model load.
 */

import { useState, useRef, useCallback } from 'react';
import { AlertTriangle } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import type { PotluckSettings, PotluckSettingsPatch } from '@/lib/localApi';

const CONTEXT_SIZE_OPTIONS = [2048, 4096, 8192, 16384, 32768] as const;
type ContextSize = (typeof CONTEXT_SIZE_OPTIONS)[number];

interface HardwarePanelProps {
  settings: PotluckSettings;
  isSaving: boolean;
  saveError: string | null;
  onUpdate: (patch: PotluckSettingsPatch) => Promise<void>;
}

// ---------------------------------------------------------------------------
// Shared label component
// ---------------------------------------------------------------------------

function FieldLabel({
  htmlFor,
  label,
  hint,
}: {
  htmlFor: string;
  label: string;
  hint?: string;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <label htmlFor={htmlFor} className="text-sm font-medium text-foreground">
        {label}
      </label>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function HardwarePanel({ settings, isSaving, saveError, onUpdate }: HardwarePanelProps) {
  const { runtime } = settings;

  // Local ephemeral state for slider display (not committed until debounce fires)
  const [localGpuLayers, setLocalGpuLayers] = useState(runtime.gpu_layers);
  const [localThreads, setLocalThreads] = useState(runtime.threads);
  const [localTemperature, setLocalTemperature] = useState(runtime.temperature);
  const [localMaxTokens, setLocalMaxTokens] = useState(runtime.max_tokens);

  // Debounce timers
  const gpuLayersTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const threadsTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const temperatureTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const maxTokensTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const debounce = useCallback(
    (
      timerRef: React.MutableRefObject<ReturnType<typeof setTimeout> | null>,
      patch: Partial<PotluckSettings['runtime']>,
      delay = 400,
    ) => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        void onUpdate({ runtime: patch });
      }, delay);
    },
    [onUpdate],
  );

  return (
    <div className="space-y-6">
      {/* Warning banner */}
      <div className="flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-500/5 px-4 py-3">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
        <p className="text-xs text-amber-600 dark:text-amber-400">
          Changes apply on the next model load. Reload your current model to use new settings.
        </p>
      </div>

      {saveError && (
        <p className="text-sm text-destructive" role="alert">
          {saveError}
        </p>
      )}

      {/* Context size */}
      <div className="space-y-2">
        <FieldLabel
          htmlFor="context-size"
          label="Context window"
          hint="Maximum number of tokens the model can consider in one inference."
        />
        <select
          id="context-size"
          value={runtime.context_size}
          disabled={isSaving}
          onChange={(e) => {
            void onUpdate({ runtime: { context_size: Number(e.target.value) as ContextSize } });
          }}
          className={cn(
            'w-full rounded-lg border border-input bg-background px-3 py-2 text-sm',
            'focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2',
            'disabled:opacity-50',
          )}
        >
          {CONTEXT_SIZE_OPTIONS.map((v) => (
            <option key={v} value={v}>
              {v.toLocaleString()} tokens
            </option>
          ))}
        </select>
      </div>

      {/* GPU layers */}
      <div className="space-y-2">
        <FieldLabel
          htmlFor="gpu-layers"
          label="GPU layers"
          hint="-1 = auto (offload as many layers as VRAM allows). 0 = CPU only."
        />
        <div className="flex items-center gap-3">
          <input
            id="gpu-layers"
            type="range"
            min={-1}
            max={100}
            value={localGpuLayers}
            disabled={isSaving}
            onChange={(e) => {
              const v = Number(e.target.value);
              setLocalGpuLayers(v);
              debounce(gpuLayersTimer, { gpu_layers: v });
            }}
            className="flex-1 accent-primary disabled:opacity-50"
            aria-valuetext={localGpuLayers === -1 ? 'Auto' : String(localGpuLayers)}
          />
          <output
            htmlFor="gpu-layers"
            className="w-12 text-right text-sm font-mono text-foreground"
          >
            {localGpuLayers === -1 ? 'Auto' : localGpuLayers}
          </output>
        </div>
      </div>

      {/* Threads */}
      <div className="space-y-2">
        <FieldLabel
          htmlFor="threads"
          label="CPU threads"
          hint="0 = auto-detect. Set manually for consistent performance."
        />
        <div className="flex items-center gap-3">
          <input
            id="threads"
            type="range"
            min={0}
            max={32}
            value={localThreads}
            disabled={isSaving}
            onChange={(e) => {
              const v = Number(e.target.value);
              setLocalThreads(v);
              debounce(threadsTimer, { threads: v });
            }}
            className="flex-1 accent-primary disabled:opacity-50"
            aria-valuetext={localThreads === 0 ? 'Auto' : String(localThreads)}
          />
          <output
            htmlFor="threads"
            className="w-12 text-right text-sm font-mono text-foreground"
          >
            {localThreads === 0 ? 'Auto' : localThreads}
          </output>
        </div>
      </div>

      {/* Temperature */}
      <div className="space-y-2">
        <FieldLabel
          htmlFor="temperature"
          label="Temperature"
          hint="Controls randomness. 0.0 = deterministic, 1.5 = highly creative."
        />
        <div className="flex items-center gap-3">
          <input
            id="temperature"
            type="range"
            min={0}
            max={1.5}
            step={0.05}
            value={localTemperature}
            disabled={isSaving}
            onChange={(e) => {
              const v = Number(e.target.value);
              setLocalTemperature(v);
              debounce(temperatureTimer, { temperature: v });
            }}
            className="flex-1 accent-primary disabled:opacity-50"
          />
          <output
            htmlFor="temperature"
            className="w-12 text-right text-sm font-mono text-foreground"
          >
            {localTemperature.toFixed(2)}
          </output>
        </div>
      </div>

      {/* Max tokens */}
      <div className="space-y-2">
        <FieldLabel
          htmlFor="max-tokens"
          label="Max output tokens"
          hint="Maximum tokens the model will generate per response."
        />
        <div className="flex items-center gap-3">
          <input
            id="max-tokens"
            type="range"
            min={128}
            max={8192}
            step={128}
            value={localMaxTokens}
            disabled={isSaving}
            onChange={(e) => {
              const v = Number(e.target.value);
              setLocalMaxTokens(v);
              debounce(maxTokensTimer, { max_tokens: v });
            }}
            className="flex-1 accent-primary disabled:opacity-50"
          />
          <output
            htmlFor="max-tokens"
            className="w-12 text-right text-sm font-mono text-foreground"
          >
            {localMaxTokens}
          </output>
        </div>
      </div>

      {isSaving && (
        <p className="text-xs text-muted-foreground">Saving…</p>
      )}
    </div>
  );
}

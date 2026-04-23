/**
 * AdvancedPanel — Advanced settings tab.
 *
 * Contains:
 *   - Model storage path (existing ModelPathPanel)
 *   - Memory tuning (sliders and toggles for memory.* settings)
 *
 * Each memory control is debounced ~300ms and persists via useSettings().update.
 */

import { useState, useRef, useCallback } from 'react';
import { Brain, FolderOpen } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { ModelPathPanel } from '@/components/settings/ModelPathPanel';
import { McpIntegrationPanel } from '@/components/settings/McpIntegrationPanel';
import { useProject } from '@/hooks/useProject';
import { normaliseProjectName } from '@/lib/projectUtils';
import type { PotluckSettings, MemorySettings } from '@/lib/localApi';
import type { MemoryCategory } from '@/types/memory';

// ---------------------------------------------------------------------------
// All valid categories in display order
// ---------------------------------------------------------------------------

const ALL_CATEGORIES: MemoryCategory[] = [
  'preference',
  'fact',
  'decision',
  'project',
  'person',
  'instruction',
];

// ---------------------------------------------------------------------------
// Shared sub-components
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

interface ToggleRowProps {
  id: string;
  label: string;
  description: string;
  checked: boolean;
  disabled: boolean;
  onChange: (checked: boolean) => void;
}

function ToggleRow({ id, label, description, checked, disabled, onChange }: ToggleRowProps) {
  return (
    <div className="flex items-start justify-between gap-4 py-3 border-b border-border last:border-0">
      <div className="flex flex-col gap-0.5 min-w-0">
        <label htmlFor={id} className="text-sm font-medium text-foreground cursor-pointer">
          {label}
        </label>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
      <button
        id={id}
        role="switch"
        type="button"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cn(
          'relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full',
          'transition-colors duration-200',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
          'disabled:cursor-not-allowed disabled:opacity-50',
          checked ? 'bg-primary' : 'bg-muted',
        )}
        aria-label={label}
      >
        <span
          className={cn(
            'pointer-events-none inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm',
            'transition-transform duration-200',
            checked ? 'translate-x-[18px]' : 'translate-x-[3px]',
          )}
        />
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Memory tuning section
// ---------------------------------------------------------------------------

interface MemoryTuningProps {
  memory: MemorySettings;
  isSaving: boolean;
  onUpdate: (patch: Partial<MemorySettings>) => void;
}

function MemoryTuningSection({ memory, isSaving, onUpdate }: MemoryTuningProps) {
  // Local slider values for responsive display before debounce commits
  const [localMaxRetrieval, setLocalMaxRetrieval] = useState(memory.max_retrieval);
  const [localMinConfidence, setLocalMinConfidence] = useState(memory.min_confidence_for_retrieval);
  const [localRetentionDays, setLocalRetentionDays] = useState(memory.retention_days);

  const maxRetrievalTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const minConfidenceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retentionDaysTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const debounce = useCallback(
    (
      timerRef: React.MutableRefObject<ReturnType<typeof setTimeout> | null>,
      patch: Partial<MemorySettings>,
      delay = 300,
    ) => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        onUpdate(patch);
      }, delay);
    },
    [onUpdate],
  );

  const toggleCategory = (cat: MemoryCategory) => {
    const enabled = memory.categories_enabled;
    const next = enabled.includes(cat)
      ? enabled.filter((c) => c !== cat)
      : [...enabled, cat];
    onUpdate({ categories_enabled: next });
  };

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-5">
      <div className="flex items-center gap-2">
        <Brain className="h-4 w-4 text-primary" aria-hidden="true" />
        <h2 className="text-sm font-semibold text-foreground">Memory tuning</h2>
      </div>

      {/* Toggles */}
      <div>
        <ToggleRow
          id="memory-auto-extract"
          label="Auto-extract from conversations"
          description="Automatically extract facts, preferences, and decisions from each conversation."
          checked={memory.auto_extract}
          disabled={isSaving}
          onChange={(v) => onUpdate({ auto_extract: v })}
        />
        <ToggleRow
          id="memory-auto-retrieve"
          label="Inject relevant memories into chat"
          description="Surface relevant memories as context at the start of each chat turn."
          checked={memory.auto_retrieve}
          disabled={isSaving}
          onChange={(v) => onUpdate({ auto_retrieve: v })}
        />
      </div>

      {/* Memories per turn slider */}
      <div className="space-y-2">
        <FieldLabel
          htmlFor="memory-max-retrieval"
          label="Memories per turn"
          hint="How many memories are injected into each chat context (5 – 50)."
        />
        <div className="flex items-center gap-3">
          <input
            id="memory-max-retrieval"
            type="range"
            min={5}
            max={50}
            step={1}
            value={localMaxRetrieval}
            disabled={isSaving}
            onChange={(e) => {
              const v = Number(e.target.value);
              setLocalMaxRetrieval(v);
              debounce(maxRetrievalTimer, { max_retrieval: v });
            }}
            className="flex-1 accent-primary disabled:opacity-50"
            aria-valuetext={String(localMaxRetrieval)}
          />
          <output
            htmlFor="memory-max-retrieval"
            className="w-8 text-right text-sm font-mono text-foreground"
          >
            {localMaxRetrieval}
          </output>
        </div>
      </div>

      {/* Min confidence slider */}
      <div className="space-y-2">
        <FieldLabel
          htmlFor="memory-min-confidence"
          label="Min confidence for retrieval"
          hint="Only memories above this confidence score are surfaced (0.10 – 0.95)."
        />
        <div className="flex items-center gap-3">
          <input
            id="memory-min-confidence"
            type="range"
            min={0.1}
            max={0.95}
            step={0.05}
            value={localMinConfidence}
            disabled={isSaving}
            onChange={(e) => {
              const v = Number(e.target.value);
              setLocalMinConfidence(v);
              debounce(minConfidenceTimer, { min_confidence_for_retrieval: v });
            }}
            className="flex-1 accent-primary disabled:opacity-50"
          />
          <output
            htmlFor="memory-min-confidence"
            className="w-10 text-right text-sm font-mono text-foreground"
          >
            {localMinConfidence.toFixed(2)}
          </output>
        </div>
      </div>

      {/* Retention days */}
      <div className="space-y-2">
        <FieldLabel
          htmlFor="memory-retention-days"
          label="Retention (days)"
          hint="Memories older than this are eligible for pruning. 0 = keep forever."
        />
        <input
          id="memory-retention-days"
          type="number"
          min={0}
          max={3650}
          value={localRetentionDays}
          disabled={isSaving}
          onChange={(e) => {
            const v = Math.max(0, Number(e.target.value));
            setLocalRetentionDays(v);
            debounce(retentionDaysTimer, { retention_days: v });
          }}
          className={cn(
            'w-28 rounded-lg border border-input bg-background px-3 py-2 text-sm',
            'focus:outline-none focus:ring-2 focus:ring-ring',
            'disabled:opacity-50',
          )}
        />
      </div>

      {/* Active categories */}
      <div className="space-y-2">
        <FieldLabel
          htmlFor="memory-categories"
          label="Active categories"
          hint="Only memories in enabled categories are extracted and retrieved."
        />
        <div
          id="memory-categories"
          role="group"
          aria-label="Active memory categories"
          className="flex flex-wrap gap-2"
        >
          {ALL_CATEGORIES.map((cat) => {
            const active = memory.categories_enabled.includes(cat);
            return (
              <button
                key={cat}
                type="button"
                aria-pressed={active}
                disabled={isSaving}
                onClick={() => toggleCategory(cat)}
                className={cn(
                  'rounded-full px-3 py-1 text-xs font-medium capitalize transition-colors',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  'disabled:opacity-50',
                  active
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-muted text-muted-foreground hover:bg-accent hover:text-foreground',
                )}
              >
                {cat}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

interface AdvancedPanelProps {
  settings: PotluckSettings;
  isSaving: boolean;
  saveError: string | null;
  onUpdate: (patch: Partial<PotluckSettings>) => Promise<void>;
}

// ---------------------------------------------------------------------------
// Active project section
// ---------------------------------------------------------------------------

function ActiveProjectSection() {
  const { projects, activeProject, setActive } = useProject();

  const handleChange = (val: string) => {
    if (val === '__new__') {
      const raw = window.prompt('New project name:');
      if (!raw) return;
      const name = normaliseProjectName(raw);
      if (!name) return;
      void setActive(name);
      return;
    }
    void setActive(val === '__global__' ? null : val);
  };

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-4">
      <div className="flex items-center gap-2">
        <FolderOpen className="h-4 w-4 text-primary" aria-hidden="true" />
        <h2 className="text-sm font-semibold text-foreground">Active project</h2>
      </div>
      <p className="text-xs text-muted-foreground">
        The active project scopes memory and task views by default. Memories tagged with this
        project (or no project) are surfaced in chat.
      </p>
      <div>
        <label htmlFor="settings-active-project" className="mb-1 block text-xs font-medium text-muted-foreground">
          Current project
        </label>
        <select
          id="settings-active-project"
          value={activeProject ?? '__global__'}
          onChange={(e) => handleChange(e.target.value)}
          className={cn(
            'w-full rounded-lg border border-input bg-background px-3 py-2',
            'text-sm text-foreground',
            'focus:outline-none focus:ring-2 focus:ring-ring',
          )}
        >
          <option value="__global__">Global (no project)</option>
          {projects.map((p) => (
            <option key={p.project} value={p.project}>
              {p.project}
            </option>
          ))}
          <option value="__new__">+ New project…</option>
        </select>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

export function AdvancedPanel({ settings, isSaving, saveError, onUpdate }: AdvancedPanelProps) {
  const handleMemoryUpdate = (patch: Partial<MemorySettings>) => {
    void onUpdate({ memory: { ...settings.memory, ...patch } });
  };

  return (
    <div className="space-y-6">
      {saveError && (
        <p className="text-sm text-destructive" role="alert">
          {saveError}
        </p>
      )}

      {/* Active project */}
      <ActiveProjectSection />

      {/* Model storage path */}
      <ModelPathPanel settings={settings} />

      {/* Memory tuning */}
      <MemoryTuningSection
        memory={settings.memory}
        isSaving={isSaving}
        onUpdate={handleMemoryUpdate}
      />

      {/* External agents (MCP) */}
      <McpIntegrationPanel />
    </div>
  );
}

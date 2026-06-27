/**
 * PrivacyPanel — privacy and telemetry toggles.
 *
 * Each toggle has a label, description, and immediate save on change.
 * Re-asserts the core Potluck AI privacy guarantee.
 */

import { useState } from 'react';
import { Shield } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import type { PotluckSettings, PotluckSettingsPatch } from '@/lib/localApi';
import { MemorySettings } from '@/components/memory/MemorySettings';
import {
  contributionReceiptsEnabled,
  setContributionReceiptsEnabled,
} from '@/lib/appPrefs';

interface PrivacyPanelProps {
  settings: PotluckSettings;
  isSaving: boolean;
  saveError: string | null;
  onUpdate: (patch: PotluckSettingsPatch) => Promise<void>;
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
    <div className="flex items-start justify-between gap-4 py-4 border-b border-border last:border-0">
      <div className="flex flex-col gap-0.5 min-w-0">
        <label htmlFor={id} className="text-sm font-medium text-foreground cursor-pointer">
          {label}
        </label>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>

      {/* Toggle switch */}
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

function ContributionReceiptsSection() {
  const [enabled, setEnabled] = useState<boolean>(() => contributionReceiptsEnabled());
  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-3">
      <h2 className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
        Contribution receipts
      </h2>
      <ToggleRow
        id="contribution-receipts"
        label="Contribution receipts"
        description="Show what your machines have served and earned. On by default."
        checked={enabled}
        disabled={false}
        onChange={(v) => {
          setContributionReceiptsEnabled(v);
          setEnabled(v);
        }}
      />
    </div>
  );
}

export function PrivacyPanel({ settings, isSaving, saveError, onUpdate }: PrivacyPanelProps) {
  const { privacy } = settings;

  const update = (patch: Partial<typeof privacy>) => {
    void onUpdate({ privacy: patch });
  };

  return (
    <div className="space-y-6">
      {/* Privacy guarantee banner */}
      <div className="flex items-start gap-3 rounded-xl border border-primary/20 bg-primary/5 px-4 py-3">
        <Shield className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
        <div>
          <p className="text-sm font-medium text-foreground">Local by default. You choose when a prompt leaves this device.</p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Local mode runs entirely on this device via llama.cpp. My Machines routes to your own
            hardware over your private WireGuard mesh. Open-pool routing passes the prompt through
            the coordinator (in memory, never stored, with your identity stripped) to a volunteer
            peer who sees the prompt but not who you are. Potluck's servers never store your
            conversation content.
          </p>
        </div>
      </div>

      {saveError && (
        <p className="text-sm text-destructive" role="alert">
          {saveError}
        </p>
      )}

      <div>
        <ToggleRow
          id="chat-history"
          label="Save chat history"
          description="Store your conversations locally in a SQLite database on this device. You can clear history at any time."
          checked={privacy.chat_history_enabled}
          disabled={isSaving}
          onChange={(v) => update({ chat_history_enabled: v })}
        />
        <ToggleRow
          id="telemetry"
          label="Share anonymous usage data"
          description="Send aggregate, non-identifiable usage statistics (feature usage counts, session length). No message content is ever included."
          checked={privacy.telemetry_enabled}
          disabled={isSaving}
          onChange={(v) => update({ telemetry_enabled: v })}
        />
        <ToggleRow
          id="crash-reports"
          label="Send crash reports"
          description="Automatically send stack traces when the app crashes. Helps us fix bugs faster. Reports never include conversation data."
          checked={privacy.crash_reports_enabled}
          disabled={isSaving}
          onChange={(v) => update({ crash_reports_enabled: v })}
        />
      </div>

      <ContributionReceiptsSection />

      <MemorySettings settings={settings} isSaving={isSaving} onUpdate={onUpdate} />
    </div>
  );
}

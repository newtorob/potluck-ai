/**
 * SettingsView - /settings route.
 *
 * Tabbed interface with query-string persistence (?tab=account, ?tab=hardware, etc.).
 * Refreshing the page restores the selected tab.
 *
 * Tabs:
 *   account - sign-in state, version and update status
 *   hardware - runtime sliders (context, gpu_layers, threads, temperature, max_tokens)
 *   privacy - telemetry and history toggles
 *   advanced - model storage path
 */

import { useSearchParams } from 'react-router-dom';
import { AlertCircle, RefreshCw } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { HardwarePanel } from '@/components/settings/HardwarePanel';
import { PrivacyPanel } from '@/components/settings/PrivacyPanel';
import { AccountPanel } from '@/components/settings/AccountPanel';
import { AdvancedPanel } from '@/components/settings/AdvancedPanel';
import { VaultPanel } from '@/components/settings/VaultPanel';
import { GatewayPanel } from '@/components/settings/GatewayPanel';
import { useSettings } from '@/hooks/useSettings';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';

// ---------------------------------------------------------------------------
// Tab definitions
// ---------------------------------------------------------------------------

type TabId = 'account' | 'hardware' | 'privacy' | 'vault' | 'connect' | 'advanced';

const TABS: { id: TabId; label: string }[] = [
  { id: 'account', label: 'Account' },
  { id: 'hardware', label: 'Hardware' },
  { id: 'privacy', label: 'Privacy' },
  { id: 'vault', label: 'Vault' },
  { id: 'connect', label: 'Connect tools' },
  { id: 'advanced', label: 'Advanced' },
];

function isValidTab(v: string): v is TabId {
  return TABS.some((t) => t.id === v);
}

function tabFromParam(value: string | null): TabId {
  return value !== null && isValidTab(value) ? value : 'account';
}

// ---------------------------------------------------------------------------
// SettingsView
// ---------------------------------------------------------------------------

export function SettingsView() {
  const { settings, isLoading, loadError, isSaving, saveError, update, retry } = useSettings();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = tabFromParam(searchParams.get('tab'));
  const setActiveTab = (tab: TabId) => {
    const next = new URLSearchParams(searchParams);
    next.set('tab', tab);
    setSearchParams(next, { replace: true });
  };

  const handleTabKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>, idx: number) => {
    const count = TABS.length;
    if (e.key === 'ArrowRight') {
      e.preventDefault();
      const next = TABS[(idx + 1) % count];
      if (next) {
        setActiveTab(next.id);
        document.getElementById(`tab-${next.id}`)?.focus();
      }
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      const prev = TABS[(idx - 1 + count) % count];
      if (prev) {
        setActiveTab(prev.id);
        document.getElementById(`tab-${prev.id}`)?.focus();
      }
    } else if (e.key === 'Home') {
      e.preventDefault();
      const first = TABS[0];
      if (first) {
        setActiveTab(first.id);
        document.getElementById(`tab-${first.id}`)?.focus();
      }
    } else if (e.key === 'End') {
      e.preventDefault();
      const last = TABS[TABS.length - 1];
      if (last) {
        setActiveTab(last.id);
        document.getElementById(`tab-${last.id}`)?.focus();
      }
    }
  };

  return (
    <div className="flex h-full min-w-0 flex-col">
      <ViewHeader title="Settings" />

      {/* Tab bar */}
      <div
        role="tablist"
        aria-label="Settings tabs"
        className="flex shrink-0 gap-0 overflow-x-auto border-b border-border px-6"
      >
        {TABS.map(({ id, label }, idx) => (
          <button
            key={id}
            id={`tab-${id}`}
            role="tab"
            type="button"
            aria-selected={activeTab === id}
            aria-controls={`tabpanel-${id}`}
            tabIndex={activeTab === id ? 0 : -1}
            onClick={() => setActiveTab(id)}
            onKeyDown={(e) => handleTabKeyDown(e, idx)}
            className={cn(
              'shrink-0 cursor-pointer whitespace-nowrap px-4 py-2.5 text-sm font-medium transition-colors duration-fast border-b-2 -mb-px',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring',
              activeTab === id
                ? 'border-primary text-foreground'
                : 'border-transparent text-muted-foreground hover:text-foreground hover:border-border',
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-xl">
          {/* Account */}
          <section
            id="tabpanel-account"
            role="tabpanel"
            aria-labelledby="tab-account"
            hidden={activeTab !== 'account'}
          >
            <AccountPanel />
          </section>

          {/* Settings-backed panels share the same loading/error pattern */}
          {(
            [
              { id: 'hardware', Panel: HardwarePanel },
              { id: 'privacy', Panel: PrivacyPanel },
              { id: 'vault', Panel: VaultPanel },
              { id: 'connect', Panel: GatewayPanel },
              { id: 'advanced', Panel: AdvancedPanel },
            ] as const
          ).map(({ id, Panel }) => (
            <section
              key={id}
              id={`tabpanel-${id}`}
              role="tabpanel"
              aria-labelledby={`tab-${id}`}
              hidden={activeTab !== id}
            >
              {settings ? (
                <>
                  {loadError && <SettingsLoadError cached isLoading={isLoading} onRetry={retry} />}
                  <Panel
                    settings={settings}
                    isSaving={isSaving}
                    saveError={saveError}
                    onUpdate={update}
                  />
                </>
              ) : loadError ? (
                <SettingsLoadError isLoading={isLoading} onRetry={retry} />
              ) : (
                <SettingsSkeleton />
              )}
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function SettingsSkeleton() {
  return (
    <div
      className="space-y-5 animate-fade-in"
      role="status"
      aria-label="Loading settings"
      aria-busy="true"
    >
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="space-y-2" style={{ animationDelay: `${i * 60}ms` }}>
          <div className="h-3 w-1/4 animate-hearth-pulse rounded-full bg-muted" />
          <div className="h-10 w-full animate-hearth-pulse rounded-lg bg-muted/70" />
        </div>
      ))}
    </div>
  );
}

function SettingsLoadError({
  cached = false,
  isLoading,
  onRetry,
}: {
  cached?: boolean;
  isLoading: boolean;
  onRetry: () => Promise<void>;
}) {
  const retryButton = (
    <button
      type="button"
      onClick={() => {
        void onRetry();
      }}
      disabled={isLoading}
      aria-busy={isLoading}
      className={cn(
        'inline-flex h-10 w-36 shrink-0 cursor-pointer items-center justify-center gap-2 rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground',
        'transition-colors hover:bg-primary/90 active:bg-primary/80',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
        'disabled:cursor-not-allowed disabled:opacity-50',
      )}
    >
      <RefreshCw
        className={cn('h-4 w-4', isLoading && 'motion-safe:animate-spin')}
        aria-hidden="true"
      />
      <span aria-live="polite">{isLoading ? 'Trying again…' : 'Retry'}</span>
    </button>
  );

  if (cached) {
    return (
      <div className="mb-6 flex flex-wrap items-start gap-3 rounded-xl border border-border bg-card p-4">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <div className="min-w-0 flex-1" role="status">
          <p className="text-sm font-medium text-foreground">Couldn’t refresh settings</p>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            Showing your last loaded settings. Try again to check for changes.
          </p>
        </div>
        {retryButton}
      </div>
    );
  }

  return (
    <div role="alert">
      <EmptyState
        icon={AlertCircle}
        tone="warning"
        title="Settings couldn’t load"
        description="Potluck couldn’t read your settings. Try again. If it keeps happening, close and reopen Potluck."
        primaryAction={retryButton}
        align="top"
        grain={false}
      />
    </div>
  );
}

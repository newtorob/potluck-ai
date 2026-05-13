/**
 * SettingsView — /settings route.
 *
 * Tabbed interface with URL hash persistence (#account, #hardware, #privacy, #advanced).
 * Refreshing the page restores the selected tab.
 *
 * Tabs:
 *   account   — placeholder / coming soon
 *   hardware  — runtime sliders (context, gpu_layers, threads, temperature, max_tokens)
 *   privacy   — telemetry and history toggles
 *   advanced  — model storage path
 */

import { useEffect, useState } from 'react';
import { AlertCircle } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { HardwarePanel } from '@/components/settings/HardwarePanel';
import { PrivacyPanel } from '@/components/settings/PrivacyPanel';
import { AccountPanel } from '@/components/settings/AccountPanel';
import { AdvancedPanel } from '@/components/settings/AdvancedPanel';
import { VaultPanel } from '@/components/settings/VaultPanel';
import { useSettings } from '@/hooks/useSettings';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';
import type { PotluckSettings } from '@/lib/localApi';

// ---------------------------------------------------------------------------
// Tab definitions
// ---------------------------------------------------------------------------

type TabId = 'account' | 'hardware' | 'privacy' | 'vault' | 'advanced';

const TABS: { id: TabId; label: string }[] = [
  { id: 'account', label: 'Account' },
  { id: 'hardware', label: 'Hardware' },
  { id: 'privacy', label: 'Privacy' },
  { id: 'vault', label: 'Vault' },
  { id: 'advanced', label: 'Advanced' },
];

function isValidTab(v: string): v is TabId {
  return TABS.some((t) => t.id === v);
}

function getTabFromHash(): TabId {
  const hash = window.location.hash.replace('#', '');
  return isValidTab(hash) ? hash : 'account';
}

// ---------------------------------------------------------------------------
// SettingsView
// ---------------------------------------------------------------------------

export function SettingsView() {
  const { settings, isLoaded, isSaving, saveError, update } = useSettings();
  const [activeTab, setActiveTab] = useState<TabId>(getTabFromHash);

  // Sync URL hash when tab changes
  useEffect(() => {
    history.replaceState(null, '', `#${activeTab}`);
  }, [activeTab]);

  // Handle back/forward navigation changing the hash
  useEffect(() => {
    const onHashChange = () => setActiveTab(getTabFromHash());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

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
    <div className="flex h-full flex-col">
      <ViewHeader title="Settings" />

      {/* Tab bar */}
      <div
        role="tablist"
        aria-label="Settings tabs"
        className="flex gap-0 border-b border-border px-6"
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
              'px-4 py-2.5 text-sm font-medium transition-colors duration-fast border-b-2 -mb-px',
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
              {!isLoaded ? (
                <SettingsSkeleton />
              ) : settings ? (
                <Panel
                  settings={settings}
                  isSaving={isSaving}
                  saveError={saveError}
                  onUpdate={update}
                />
              ) : (
                <SidecarError />
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
    <div className="space-y-5 animate-fade-in" aria-label="Loading settings" aria-live="polite">
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="space-y-2" style={{ animationDelay: `${i * 60}ms` }}>
          <div className="h-3 w-1/4 animate-hearth-pulse rounded-full bg-muted" />
          <div className="h-10 w-full animate-hearth-pulse rounded-lg bg-muted/70" />
        </div>
      ))}
    </div>
  );
}

function SidecarError() {
  return (
    <EmptyState
      icon={AlertCircle}
      tone="warning"
      title="Local sidecar unreachable"
      description="The Python backend isn't responding on 127.0.0.1:8321. Settings will load once it's running."
      align="top"
      grain={false}
    />
  );
}

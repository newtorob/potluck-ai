import { Settings } from 'lucide-react';
import { useState } from 'react';
import { cn } from '@/components/ui/cn';

type Tab = 'account' | 'hardware' | 'privacy' | 'advanced';

const TABS: { id: Tab; label: string }[] = [
  { id: 'account', label: 'Account' },
  { id: 'hardware', label: 'Hardware' },
  { id: 'privacy', label: 'Privacy' },
  { id: 'advanced', label: 'Advanced' },
];

/**
 * Settings view — top-level route for /settings.
 *
 * Placeholder scaffold with inert tab navigation. Real panels:
 *   - Account: profile info, membership status, billing portal link
 *   - Hardware: GPU layers slider, thread count, context size
 *   - Privacy: telemetry toggle, chat history toggle
 *   - Advanced: model storage path, debug logging
 *
 * State driven by useSettings hook -> localApi.getSettings / updateSettings
 */
export function SettingsView() {
  const [activeTab, setActiveTab] = useState<Tab>('account');

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-border px-6 py-4">
        <Settings className="h-5 w-5 text-muted-foreground" />
        <h1 className="text-sm font-semibold">Settings</h1>
      </div>

      {/* Tab bar */}
      <div className="flex gap-1 border-b border-border px-6 pt-2">
        {TABS.map(({ id, label }) => (
          <button
            key={id}
            onClick={() => setActiveTab(id)}
            className={cn(
              'px-4 py-2 text-sm font-medium transition-colors rounded-t-md',
              activeTab === id
                ? 'text-foreground border-b-2 border-primary'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Tab content placeholder */}
      <div className="flex-1 overflow-y-auto px-6 py-8">
        <div className="mx-auto max-w-xl space-y-4">
          {/* Skeleton rows */}
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="space-y-2">
              <div className="h-3 w-1/4 animate-pulse rounded bg-muted" />
              <div className="h-10 w-full animate-pulse rounded-lg bg-muted" />
            </div>
          ))}
        </div>

        {/* Empty state */}
        <div className="mt-10 flex flex-col items-center justify-center gap-2 text-center">
          <p className="text-sm font-medium text-muted-foreground capitalize">
            {activeTab} settings coming soon
          </p>
          <p className="text-xs text-muted-foreground/60">
            Wired to Python sidecar via localApi.getSettings — guide §4.4
          </p>
        </div>
      </div>
    </div>
  );
}

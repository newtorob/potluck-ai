import { useEffect, useState } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { TitleBar } from '@/components/layout/TitleBar';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { Sidebar } from '@/components/layout/Sidebar';
import { StatusBar } from '@/components/layout/StatusBar';
import { AuthGate } from '@/components/auth/AuthGate';
import { ChatView } from '@/views/ChatView';
import { ModelLibrary } from '@/views/ModelLibrary';
import { MemoryView } from '@/views/MemoryView';
import { MemoryMapView } from '@/views/MemoryMapView';
import { TaskStateView } from '@/views/TaskStateView';
import { AgentsView } from '@/views/AgentsView';
import { NetworkView } from '@/views/NetworkView';
import { HouseholdView } from '@/views/HouseholdView';
import { SettingsView } from '@/views/SettingsView';
import { MembershipView } from '@/views/MembershipView';
import { FirstRun } from '@/views/FirstRun';
import { useModelStore } from '@/stores/modelStore';
import { useChatStore } from '@/stores/chatStore';
import { useTheme } from '@/hooks/useTheme';

/**
 * Root application shell.
 *
 * Layout: TitleBar (Mac drag region) + Sidebar (fixed left) + main content area.
 * AuthGate is a pass-through for now; real auth enforcement comes later.
 *
 * Routes:
 *   /chat        — streaming chat interface
 *   /models      — model library: browse, install, delete
 *   /settings    — hardware, privacy, account, advanced tabs
 *   /membership  — founding member status + billing
 *
 * --- First-Run gating ---
 * On mount we fetch the model catalog from the sidecar. We show the FirstRun
 * wizard only when ALL of the following are true after the fetch settles:
 *   1. No installed models (none with installed === true)
 *   2. localStorage key `potluck.onboarding_completed` is not 'true'
 *
 * We wait for the initial fetch to complete before deciding so that users
 * who already have models installed never see the wizard on startup.
 *
 * The wizard sets localStorage on completion or skip — the gate is never
 * re-entered in the same session, and never re-appears on subsequent launches.
 */

const ONBOARDING_KEY = 'potluck.onboarding_completed';

export default function App() {
  // Initialise theme from localStorage on mount so <html class> matches
  // user preference before paint (avoids cream-flash on dark startup).
  useTheme();

  // Route path drives the per-view error boundary's reset: navigating to a
  // different view clears a crashed boundary so the user is never stuck.
  const location = useLocation();

  // Read model state directly from the store at the shell level.
  // useModels() is not used here to avoid triggering its auto-fetch inside the
  // conditional FirstRun branch, which could cause duplicated requests.
  const models = useModelStore((s) => s.models);
  const isFetching = useModelStore((s) => s.isFetching);
  const fetchModels = useModelStore((s) => s.fetchModels);

  // fetchSettled: true once the very first fetch has resolved (success or error).
  const [fetchSettled, setFetchSettled] = useState(false);

  useEffect(() => {
    void fetchModels().finally(() => setFetchSettled(true));
    // Hydrate chat history from the sidecar's SQLite. Runs once on mount;
    // failures are non-fatal (chat still works in memory). See chatStore.hydrate().
    void useChatStore.getState().hydrate();
    // fetchModels is stable (Zustand action ref never changes)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onboardingCompleted = localStorage.getItem(ONBOARDING_KEY) === 'true';
  const hasInstalledModels = models.some((m) => m.installed);

  // Only gate once the fetch has settled to avoid a false-positive flash
  const showFirstRun = fetchSettled && !onboardingCompleted && !hasInstalledModels;

  // Loading spinner while the very first fetch is in-flight
  if (!fetchSettled && isFetching) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-background">
        <div
          className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent"
          role="status"
          aria-label="Loading"
        />
      </div>
    );
  }

  // First-run wizard (no sidebar, no routes — wizard owns the full viewport)
  if (showFirstRun) {
    return (
      <div className="flex h-screen w-screen flex-col overflow-hidden bg-background text-foreground">
        <TitleBar />
        <FirstRun models={models} />
      </div>
    );
  }

  // Normal app shell:
  //   TitleBar (mac drag region)
  //   ┌──────────────────────────────────┐
  //   │ Sidebar │ main (Routes)          │
  //   └──────────────────────────────────┘
  //   StatusBar (persistent "alive" strip)
  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-background text-foreground">
      {/* Mac-only drag region — zero height on other platforms */}
      <TitleBar />

      <div className="flex flex-1 overflow-hidden">
        <AuthGate>
          <Sidebar />

          <main className="flex-1 overflow-hidden min-w-0">
            <ErrorBoundary resetKey={location.pathname} scopeLabel="this view">
            <Routes>
              <Route path="/" element={<Navigate to="/chat" replace />} />
              <Route path="/chat" element={<ChatView />} />
              <Route path="/models" element={<ModelLibrary />} />
              <Route path="/memory" element={<MemoryView />} />
              <Route path="/memory/map" element={<MemoryMapView />} />
              <Route path="/tasks" element={<TaskStateView />} />
              <Route path="/agents" element={<AgentsView />} />
              <Route path="/household" element={<HouseholdView />} />
              <Route path="/network" element={<NetworkView />} />
              <Route path="/settings" element={<SettingsView />} />
              <Route path="/membership" element={<MembershipView />} />
              {/* Catch-all: redirect unknown paths to chat */}
              <Route path="*" element={<Navigate to="/chat" replace />} />
            </Routes>
            </ErrorBoundary>
          </main>
        </AuthGate>
      </div>

      {/* Persistent bottom status strip — model loaded, sidecar, network mode */}
      <StatusBar />
    </div>
  );
}

import { useEffect, useRef, useState } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { TitleBar } from '@/components/layout/TitleBar';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { Sidebar } from '@/components/layout/Sidebar';
import { StatusBar } from '@/components/layout/StatusBar';
import { SidecarReconnectingBanner } from '@/components/layout/SidecarReconnectingBanner';
import { UpdateBanner } from '@/components/layout/UpdateBanner';
import { AnswerCheckBanner } from '@/components/layout/AnswerCheckBanner';
import { UpdateRequiredGate } from '@/components/layout/UpdateRequiredGate';
import { AccountRequired } from '@/components/auth/AccountRequired';
import { AuthGate } from '@/components/auth/AuthGate';
import { ChatView } from '@/views/ChatView';
import { ModelLibrary } from '@/views/ModelLibrary';
import { MemoryView } from '@/views/MemoryView';
import { MemoryMapView } from '@/views/MemoryMapView';
import { TaskStateView } from '@/views/TaskStateView';
import { AgentsView } from '@/views/AgentsView';
import { UsageView } from '@/views/UsageView';
import { NetworkView } from '@/views/NetworkView';
import { HouseholdView } from '@/views/HouseholdView';
import { CirclesView } from '@/views/CirclesView';
import { ReadinessView } from '@/views/ReadinessView';
import { SettingsView } from '@/views/SettingsView';
import { MembershipView } from '@/views/MembershipView';
import { AdminView } from '@/views/AdminView';
import { AuthVerify } from '@/views/AuthVerify';
import { FirstRun } from '@/views/FirstRun';
import { useModelStore } from '@/stores/modelStore';
import { useChatStore } from '@/stores/chatStore';
import { useMeshStore } from '@/stores/meshStore';
import { useUpdateStore } from '@/stores/updateStore';
import { initCoordinatorBaseSync } from '@/stores/connectionStore';
import { useUiPrefsStore } from '@/stores/uiPrefsStore';
import { localApi } from '@/lib/localApi';
import { autoLoadOnStartup } from '@/lib/appPrefs';
import { hydrateDurablePrefs } from '@/lib/durablePrefs';
import { useWeeklyReceiptToast } from '@/hooks/useWeeklyReceiptToast';
import { WeeklyReceiptToast } from '@/components/household/WeeklyReceiptToast';

/**
 * Root application shell.
 *
 * Layout: TitleBar (Mac drag region) + Sidebar (fixed left) + main content area.
 * AuthGate offers local use or account sign-in before Sidebar/routes render - see
 * apps/desktop/frontend/src/components/auth/AuthGate.tsx for the loading /
 * sign-in-wall / offline-cached-account decision.
 *
 * Above all of that sits the update version gate: a client below the
 * server-reported minimum version renders UpdateRequiredGate instead of
 * anything else (see updateStore.checkVersionHandshake, called on mount
 * below). See also UpdateBanner for the non-blocking "update available"
 * strip driven by the local auto-updater.
 *
 * Routes:
 *   /chat - streaming chat interface
 *   /models - model library: browse, install, delete
 *   /settings - hardware, privacy, account, advanced tabs
 *   /membership - founding member status + billing
 *
 * --- First-Run gating ---
 * On mount we fetch the model catalog from the sidecar. We show the FirstRun
 * wizard only when all of the following are true after the fetch settles:
 *   1. No installed models (none with installed === true)
 *   2. potluck.onboarding_completed is not true in uiPrefsStore (backed by
 *      localStorage + the sidecar durable config)
 *
 * That decision is latched: once the wizard opens, finishing a model install
 * must not unmount it mid-flow. The wizard exits only when onboarding is
 * marked complete (skip / finish).
 *
 * We wait for the initial fetch to complete before deciding so that users
 * who already have models installed never see the wizard on startup.
 *
 * The wizard marks completion via uiPrefsStore.setOnboardingCompleted(true),
 * which writes localStorage and persists to the sidecar config so the flag
 * survives reinstalls and dev-vs-packaged launches.
 */

export default function App() {

  // Weekly receipt toast - once per completed week, opt-out via Settings → Privacy.
  const { toast: weeklyToast, dismiss: dismissWeeklyToast } = useWeeklyReceiptToast();

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
    // Hydrate all durable preferences from the Python sidecar config
    // (~/.potluck/config.json). Covers the coordinator URL, theme, routing
    // scope, sidebar state, onboarding flag, and more. One GET /settings call;
    // values are applied to the live stores so they survive localStorage wipes,
    // reinstalls, and dev-vs-packaged launches. Degrades gracefully when the
    // sidecar is not yet ready (localStorage defaults stay in effect).
    void hydrateDurablePrefs();
    // fetchModels is stable (Zustand action ref never changes)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Start the mesh status poller app-wide so the StatusBar mesh pill reflects
  // connectivity from any view, not just My Machines. start() is idempotent and
  // the store pauses polling on document.hidden; we leave it running for the
  // app's lifetime (no stop on unmount).
  useEffect(() => {
    useMeshStore.getState().start();
  }, []);

  // Push the coordinator origin to the Electron main process (packaged app
  // only - no-op in dev/browser) so its onBeforeSendHeaders injector knows
  // which host to attach the member session bearer to for coordinator
  // requests that bypass the buffering IPC bridge (streamNetworkJob's SSE
  // stream - see localApi.ts). Pushes once now, then again on every
  // subsequent coordinatorUrl change (user edit, sidecar adoption, durable
  // hydration) via a store subscription - see connectionStore.ts.
  useEffect(() => {
    initCoordinatorBaseSync();
  }, []);

  // Version handshake: GET /client/version and compare against the running
  // build. Below `minimum` sets updateStore.blocked, which the early return
  // below turns into the full-screen UpdateRequiredGate - the backstop that
  // stands in for whatever cryptic failure an old client would otherwise hit
  // against a changed backend. Below `latest` (but not blocked) nudges the
  // same "available" banner state UpdateBanner drives from the local
  // auto-updater. Never blocks on a failed/absent response - see
  // updateStore.checkVersionHandshake.
  useEffect(() => {
    void useUpdateStore.getState().checkVersionHandshake();
  }, []);

  // Legacy sidecars only. Current runtimes own startup restoration, including
  // the choice to stay unloaded; opening another UI must never override it.
  // Make the default launch state "alive": once the model list has settled,
  // auto-load the last-active model (fallback: smallest installed) so the user
  // lands ready-to-chat instead of on "No model loaded". Runs once, only when
  // nothing is already loaded and at least one model is installed (a fresh
  // machine with no models falls through to the FirstRun wizard). Opt out via
  // Settings → Advanced → Startup.
  const autoLoadTried = useRef(false);
  useEffect(() => {
    if (autoLoadTried.current || !fetchSettled) return;
    autoLoadTried.current = true;
    if (!autoLoadOnStartup()) return;
    void (async () => {
      try {
        const health = await localApi.health();
        if (health.model_restore) return;
        if (health.model_loaded) return; // already live - leave it
        const installed = useModelStore.getState().models.filter((m) => m.installed);
        if (installed.length === 0) return; // nothing to load - FirstRun handles this
        const lastActive = health.active_model_id
          ? installed.find((m) => m.slug === health.active_model_id)
          : undefined;
        const pick =
          lastActive ?? [...installed].sort((a, b) => a.size_bytes - b.size_bytes)[0];
        if (pick) void useModelStore.getState().load(pick.slug);
      } catch {
        // Sidecar unreachable - nothing to auto-load.
      }
    })();
  }, [fetchSettled]);

  const onboardingCompleted = useUiPrefsStore((s) => s.onboardingCompleted);
  const hasInstalledModels = models.some((m) => m.installed);
  const updateBlocked = useUpdateStore((s) => s.blocked);

  // Latch the "should show wizard" decision after the first settled fetch.
  // Without this, completing the onboarding install flips hasInstalledModels
  // and unmounts FirstRun mid-flow (before the user finishes the wizard).
  const wizardLatched = useRef(false);
  if (fetchSettled && !onboardingCompleted && !hasInstalledModels) {
    wizardLatched.current = true;
  }
  if (onboardingCompleted) {
    wizardLatched.current = false;
  }

  // Only gate once the fetch has settled to avoid a false-positive flash
  const showFirstRun = fetchSettled && !onboardingCompleted && wizardLatched.current;

  // Universal update backstop: a client below the server-reported `minimum`
  // version blocks here, before anything else renders - including the
  // magic-link bounce page below. Whatever changed on the backend, an old
  // client shows this one clear screen instead of a cryptic downstream
  // failure. See the version-handshake effect above / updateStore.ts.
  if (updateBlocked) {
    return <UpdateRequiredGate />;
  }

  // Standalone magic-link verify/bounce page - rendered outside the app shell
  // (no sidebar/status bar). In a browser it bounces to the potluck:// scheme;
  // inside Electron it verifies in place. See AuthVerify + useDeepLinkAuth.
  if (location.pathname === '/auth/verify') {
    return <AuthVerify />;
  }

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

  // First-run wizard (no sidebar, no routes - wizard owns the full viewport)
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
      {/* Mac-only drag region - zero height on other platforms */}
      <TitleBar />

      {/* Thin strip shown only if the local engine drops after being up */}
      <SidecarReconnectingBanner />

      {/* Thin strip driving the auto-update download/restart flow */}
      <UpdateBanner />

      {/* Thin strip shown when the answer check moved the model to the CPU or the model failed it */}
      <AnswerCheckBanner />

      <div className="flex min-h-0 flex-1 overflow-hidden">
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
              <Route path="/usage" element={<UsageView />} />
              <Route path="/household" element={<AccountRequired feature="connect your machines"><HouseholdView /></AccountRequired>} />
              <Route path="/circles" element={<AccountRequired feature="use trusted circles"><CirclesView /></AccountRequired>} />
              <Route path="/readiness" element={<ReadinessView />} />
              <Route path="/network" element={<AccountRequired feature="use the network"><NetworkView /></AccountRequired>} />
              <Route path="/settings" element={<SettingsView />} />
              <Route path="/membership" element={<MembershipView />} />
              <Route path="/admin" element={<AccountRequired feature="open administration"><AdminView /></AccountRequired>} />
              {/* Catch-all: redirect unknown paths to chat */}
              <Route path="*" element={<Navigate to="/chat" replace />} />
            </Routes>
            </ErrorBoundary>
          </main>
        </AuthGate>
      </div>

      {/* Persistent bottom status strip - model loaded, sidecar, network mode */}
      <StatusBar />

      {/* Weekly receipt toast - floats above the status bar */}
      {weeklyToast.visible && weeklyToast.data !== null && (
        <WeeklyReceiptToast data={weeklyToast.data} onDismiss={dismissWeeklyToast} />
      )}
    </div>
  );
}

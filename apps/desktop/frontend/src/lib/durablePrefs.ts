/**
 * Durable UI preferences — hydration from the Python sidecar config.
 *
 * hydrateDurablePrefs() runs once at App mount. It fetches GET /settings from
 * the local sidecar and applies any stored values to the live stores, so
 * preferences survive localStorage wipes, reinstalls, and dev-vs-packaged
 * launches.
 *
 * Execution model:
 *   1. On startup, each store/hook reads localStorage synchronously (fast-path
 *      cache). This is always instant and never causes a startup flash.
 *   2. hydrateDurablePrefs() resolves ~1 RTT later. For any key that differs
 *      between localStorage (possibly empty) and config.json, the store's
 *      setter is called to update both localStorage and live React state.
 *   3. Sidecar unreachable → try/catch returns early; localStorage defaults
 *      remain in effect. The session is always correct.
 *
 * Also consolidates the coordinator-URL hydration that previously lived
 * directly in App.tsx, so there is one GET /settings per startup instead
 * of two.
 *
 * Import graph: this module imports from stores and localApi; none of those
 * import from durablePrefs, so there is no circular dependency.
 */

import { localApi } from '@/lib/localApi';
import { useConnectionStore } from '@/stores/connectionStore';
import { useUiPrefsStore } from '@/stores/uiPrefsStore';
import { useChatStore } from '@/stores/chatStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { setAutoLoadOnStartup, setContributionReceiptsEnabled } from '@/lib/appPrefs';

/**
 * Fetch durable settings from the sidecar and apply them to the live stores.
 * Safe to call from a fire-and-forget useEffect — degrades gracefully when
 * the sidecar is not yet ready.
 */
export async function hydrateDurablePrefs(): Promise<void> {
  let settings;
  try {
    settings = await localApi.getSettings();
  } catch {
    // Sidecar unreachable — localStorage defaults stay in effect for this launch.
    return;
  }

  // Seed the settings store from this same response. chatStore's chat-history
  // privacy gate reads it and fails closed while settings are null, so this
  // must land before the first send. Costs no extra round-trip.
  useSettingsStore.getState().hydrateFromSidecar(settings);

  // Coordinator URL (network section, not the ui dict).
  const coordUrl = settings.network.coordinator_url.trim();
  if (coordUrl.length > 0) {
    useConnectionStore.getState().hydrateFromSidecar(coordUrl);
  }

  // UI preferences (settings.ui is a generic string→string dict the frontend owns).
  const ui = settings.ui;

  // theme
  const theme = ui[PREF.theme];
  if (theme === 'dark' || theme === 'light') {
    useUiPrefsStore.getState().setTheme(theme);
  }

  // autoload_on_startup — localStorage-only live state; corrects the value for
  // the current session even though the auto-load effect already ran earlier.
  const autoload = ui[PREF.autoload];
  if (autoload !== undefined) {
    setAutoLoadOnStartup(autoload !== 'false');
  }

  // contribution_receipts_enabled
  const receipts = ui[PREF.receipts];
  if (receipts !== undefined) {
    setContributionReceiptsEnabled(receipts !== 'false');
  }

  // routing_scope — Zustand chatStore handles live state + localStorage.
  const scope = ui[PREF.routingScope];
  if (
    scope === 'local' ||
    scope === 'household' ||
    scope === 'circle' ||
    scope === 'cooperative'
  ) {
    useChatStore.getState().setRoutingScope(scope);
  }

  // household_routing_mode
  const mode = ui[PREF.routingMode];
  if (mode === 'capability' || mode === 'round_robin') {
    useChatStore.getState().setRoutingMode(mode);
  }

  // sidebar_collapsed
  const collapsed = ui[PREF.sidebarCollapsed];
  if (collapsed !== undefined) {
    useUiPrefsStore.getState().setSidebarCollapsed(collapsed === 'true');
  }

  // onboarding_completed
  const onboarding = ui[PREF.onboarding];
  if (onboarding !== undefined) {
    useUiPrefsStore.getState().setOnboardingCompleted(onboarding === 'true');
  }
}

// ---------------------------------------------------------------------------
// Preference key constants — single source of truth shared with each setter.
// Must match the localStorage keys the stores already use.
// ---------------------------------------------------------------------------

export const PREF = {
  theme: 'potluck.theme',
  autoload: 'potluck.autoload_on_startup',
  receipts: 'potluck.contribution_receipts_enabled',
  routingScope: 'potluck.routing_scope',
  routingMode: 'potluck.household_routing_mode',
  sidebarCollapsed: 'potluck.sidebar_collapsed',
  onboarding: 'potluck.onboarding_completed',
} as const;

/**
 * UI preferences store - durable, cross-surface state for the three prefs
 * that require live React updates when hydrated from the sidecar config.
 *
 * Why Zustand here instead of useState:
 *   theme, sidebarCollapsed, and onboardingCompleted all need to be updated
 *   imperatively by hydrateDurablePrefs() after app mount. React's useState
 *   is local to a component instance and cannot be called from outside React.
 *   A Zustand store provides the same synchronous localStorage read on init
 *   with the imperative getState().set*() path that hydration requires.
 *
 * Write-through contract:
 *   Each setter writes localStorage first (so the session is correct even
 *   if the sidecar is unreachable), then fires a fire-and-forget PATCH to
 *   the Python sidecar so the value survives localStorage wipes, reinstalls,
 *   and dev-vs-packaged launches. See lib/sidecarPersist.ts.
 *
 * Theme application:
 *   applyTheme() is called synchronously at module load time (before the
 *   first React render) so there is no flash of the wrong theme. Subsequent
 *   calls in setTheme() keep the DOM class in sync on every change.
 */

import { create } from 'zustand';
import { persistUiPref } from '@/lib/sidecarPersist';

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

export type Theme = 'dark' | 'light';

const THEME_KEY = 'potluck.theme';

function readStoredTheme(): Theme {
  if (typeof window === 'undefined') return 'dark';
  return window.localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark';
}

function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') return;
  const html = document.documentElement;
  html.classList.toggle('dark', theme === 'dark');
  html.classList.toggle('light', theme === 'light');
  html.style.colorScheme = theme;
}

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

const COLLAPSE_KEY = 'potluck.sidebar_collapsed';

function readStoredSidebarCollapsed(): boolean {
  if (typeof window === 'undefined') return false;
  return window.localStorage.getItem(COLLAPSE_KEY) === 'true';
}

// ---------------------------------------------------------------------------
// Sidebar nav groups (per-group collapse of the "Navigate" / "Workspace"
// clusters, so a chat-heavy user can reclaim vertical space for the Chats list)
// ---------------------------------------------------------------------------

const NAV_GROUPS_KEY = 'potluck.nav_groups_collapsed';

function readStoredNavGroupsCollapsed(): Record<string, boolean> {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(NAV_GROUPS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, boolean>) : {};
  } catch {
    return {};
  }
}

// ---------------------------------------------------------------------------
// Onboarding
// ---------------------------------------------------------------------------

const ONBOARDING_KEY = 'potluck.onboarding_completed';

function readStoredOnboardingCompleted(): boolean {
  if (typeof window === 'undefined') return false;
  return window.localStorage.getItem(ONBOARDING_KEY) === 'true';
}

// ---------------------------------------------------------------------------
// Store shape
// ---------------------------------------------------------------------------

interface UiPrefsState {
  theme: Theme;
  sidebarCollapsed: boolean;
  /** Which sidebar nav groups are collapsed, keyed by group id. Absent/false = expanded. */
  navGroupsCollapsed: Record<string, boolean>;
  onboardingCompleted: boolean;
  /** Explicit choice to enter the app without an account. */
  localUseEnabled: boolean;
  /** In-session revision prevents slow hydration from overwriting a new choice. */
  localUseRevision: number;
  setLocalUseEnabled: (enabled: boolean) => void;

  /** Switch theme, update DOM class, persist to localStorage + sidecar. */
  setTheme: (t: Theme) => void;
  /** Set sidebar collapsed state, persist to localStorage + sidecar. */
  setSidebarCollapsed: (v: boolean) => void;
  /** Collapse/expand a single sidebar nav group, persist to localStorage + sidecar. */
  setNavGroupCollapsed: (id: string, v: boolean) => void;
  /** Mark onboarding done (or clear it), persist to localStorage + sidecar. */
  setOnboardingCompleted: (v: boolean) => void;
}

// Apply the stored theme immediately at module load - before any React render -
// so there is no flash of the wrong theme on startup.
const _initialTheme = readStoredTheme();
applyTheme(_initialTheme);

export const useUiPrefsStore = create<UiPrefsState>()((set) => ({
  theme: _initialTheme,
  sidebarCollapsed: readStoredSidebarCollapsed(),
  navGroupsCollapsed: readStoredNavGroupsCollapsed(),
  onboardingCompleted: readStoredOnboardingCompleted(),
  localUseRevision: 0,
  localUseEnabled: typeof window !== 'undefined' && window.localStorage.getItem('potluck.local_use_enabled') === 'true',

  setLocalUseEnabled(enabled) {
    if (typeof window !== 'undefined') window.localStorage.setItem('potluck.local_use_enabled', String(enabled));
    set((state) => ({ localUseEnabled: enabled, localUseRevision: state.localUseRevision + 1 }));
    persistUiPref('potluck.local_use_enabled', String(enabled));
  },

  setTheme(t) {
    if (typeof window !== 'undefined') window.localStorage.setItem(THEME_KEY, t);
    applyTheme(t);
    set({ theme: t });
    persistUiPref(THEME_KEY, t);
  },

  setSidebarCollapsed(v) {
    if (typeof window !== 'undefined') window.localStorage.setItem(COLLAPSE_KEY, String(v));
    set({ sidebarCollapsed: v });
    persistUiPref(COLLAPSE_KEY, String(v));
  },

  setNavGroupCollapsed(id, v) {
    set((s) => {
      const next = { ...s.navGroupsCollapsed, [id]: v };
      const serialized = JSON.stringify(next);
      if (typeof window !== 'undefined') window.localStorage.setItem(NAV_GROUPS_KEY, serialized);
      persistUiPref(NAV_GROUPS_KEY, serialized);
      return { navGroupsCollapsed: next };
    });
  },

  setOnboardingCompleted(v) {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(ONBOARDING_KEY, v ? 'true' : 'false');
    }
    set({ onboardingCompleted: v });
    persistUiPref(ONBOARDING_KEY, v ? 'true' : 'false');
  },
}));

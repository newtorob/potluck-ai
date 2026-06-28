/**
 * useTheme — Hearth/Common-Room theme toggle.
 *
 * The Hearth design system supports two themes:
 *   - dark  → "Hearth"        — warm-charcoal product chrome (default)
 *   - light → "Common Room"   — cream paper for manifesto/membership
 *
 * State lives in uiPrefsStore (Zustand) so it is imperatively updatable from
 * hydrateDurablePrefs() on startup. The store reads localStorage synchronously
 * at module load time (no startup flash) and applies applyTheme() immediately —
 * before the first React render — so the HTML class matches user preference
 * before paint.
 *
 * Write-through: setTheme writes localStorage, updates the DOM class, and
 * fires a fire-and-forget PATCH to the Python sidecar so the choice survives
 * localStorage wipes, reinstalls, and dev-vs-packaged launches.
 */

import { useUiPrefsStore, type Theme } from '@/stores/uiPrefsStore';

export type { Theme };

export interface UseThemeReturn {
  theme: Theme;
  setTheme: (t: Theme) => void;
  toggle: () => void;
}

export function useTheme(): UseThemeReturn {
  const theme = useUiPrefsStore((s) => s.theme);
  const setTheme = useUiPrefsStore((s) => s.setTheme);

  function toggle() {
    setTheme(theme === 'dark' ? 'light' : 'dark');
  }

  return { theme, setTheme, toggle };
}

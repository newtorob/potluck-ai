/**
 * useTheme — Hearth/Common-Room theme toggle.
 *
 * The Hearth design system supports two themes:
 *   - dark  → "Hearth"        — warm-charcoal product chrome (default)
 *   - light → "Common Room"   — cream paper for manifesto/membership
 *
 * Persists user choice to localStorage under `potluck.theme`. On initial
 * mount we honour the stored choice; absent a stored value we default to
 * 'dark' (matches the bootstrapped <html class="dark"> in index.html).
 *
 * Synchronously applies the class to <html> before paint to prevent any
 * cream-flash on dark startup or vice versa.
 *
 * No reliance on prefers-color-scheme: the manifesto explicitly positions
 * dark Hearth as the product chrome and light Common Room as the editorial
 * surface; we don't want OS preference to override that choice.
 */

import { useCallback, useEffect, useState } from 'react';

const STORAGE_KEY = 'potluck.theme';

export type Theme = 'dark' | 'light';

function readStoredTheme(): Theme {
  if (typeof window === 'undefined') return 'dark';
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return stored === 'light' ? 'light' : 'dark';
}

function applyTheme(theme: Theme) {
  if (typeof document === 'undefined') return;
  const html = document.documentElement;
  html.classList.toggle('dark', theme === 'dark');
  html.classList.toggle('light', theme === 'light');
  // Hint browser form controls / scrollbars to match
  html.style.colorScheme = theme;
}

export interface UseThemeReturn {
  theme: Theme;
  setTheme: (t: Theme) => void;
  toggle: () => void;
}

export function useTheme(): UseThemeReturn {
  const [theme, setThemeState] = useState<Theme>(readStoredTheme);

  // Apply on mount and whenever theme changes
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const setTheme = useCallback((t: Theme) => {
    window.localStorage.setItem(STORAGE_KEY, t);
    setThemeState(t);
  }, []);

  const toggle = useCallback(() => {
    setTheme(theme === 'dark' ? 'light' : 'dark');
  }, [theme, setTheme]);

  return { theme, setTheme, toggle };
}

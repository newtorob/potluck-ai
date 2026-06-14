/**
 * App-level UI preferences persisted in localStorage (frontend-only, like the
 * theme + onboarding flags). Distinct from backend settings (privacy, memory
 * tuning) which round-trip through the sidecar.
 */

const AUTOLOAD_KEY = 'potluck.autoload_on_startup';

/** Whether to auto-load the last-active model when the app launches.
 *  Defaults to ON (only an explicit 'false' disables it). */
export function autoLoadOnStartup(): boolean {
  return localStorage.getItem(AUTOLOAD_KEY) !== 'false';
}

export function setAutoLoadOnStartup(on: boolean): void {
  localStorage.setItem(AUTOLOAD_KEY, on ? 'true' : 'false');
}

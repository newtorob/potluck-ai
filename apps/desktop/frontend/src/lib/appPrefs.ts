/**
 * App-level UI preferences persisted in localStorage (frontend-only, like the
 * theme + onboarding flags). Distinct from backend settings (privacy, memory
 * tuning) which round-trip through the sidecar.
 *
 * The durable setters (setAutoLoadOnStartup, setContributionReceiptsEnabled)
 * also fire a write-through to the Python sidecar config so the values survive
 * localStorage wipes, reinstalls, and dev-vs-packaged launches. See
 * lib/sidecarPersist.ts for the merge semantics.
 *
 * The two ephemeral keys (weeklyReceiptToastShownFor, active_conversation_id)
 * are intentionally NOT persisted to the sidecar — they are per-session/per-week
 * tracking state, not cross-surface preferences.
 */

import { persistUiPref } from '@/lib/sidecarPersist';

const AUTOLOAD_KEY = 'potluck.autoload_on_startup';

/** Whether to auto-load the last-active model when the app launches.
 *  Defaults to ON (only an explicit 'false' disables it). */
export function autoLoadOnStartup(): boolean {
  return localStorage.getItem(AUTOLOAD_KEY) !== 'false';
}

export function setAutoLoadOnStartup(on: boolean): void {
  const v = on ? 'true' : 'false';
  localStorage.setItem(AUTOLOAD_KEY, v);
  persistUiPref(AUTOLOAD_KEY, v);
}

// ---------------------------------------------------------------------------
// Contribution receipts — opt-out UI preference
// ---------------------------------------------------------------------------

const RECEIPTS_KEY = 'potluck.contribution_receipts_enabled';

/**
 * Whether to show contribution receipt features (ContributionReceiptsCard +
 * weekly toast). Defaults to ON (only an explicit 'false' turns it off).
 */
export function contributionReceiptsEnabled(): boolean {
  return localStorage.getItem(RECEIPTS_KEY) !== 'false';
}

export function setContributionReceiptsEnabled(on: boolean): void {
  const v = on ? 'true' : 'false';
  localStorage.setItem(RECEIPTS_KEY, v);
  persistUiPref(RECEIPTS_KEY, v);
}

// ---------------------------------------------------------------------------
// Weekly receipt toast — once-per-week shown tracking (ephemeral, not durable)
// ---------------------------------------------------------------------------

const WEEKLY_TOAST_KEY = 'potluck.weekly_receipt_toast_shown_for';

/**
 * The ISO week_start ("YYYY-MM-DD") for which the weekly toast was last
 * shown (or dismissed). Returns null if never shown.
 */
export function weeklyReceiptToastShownFor(): string | null {
  return localStorage.getItem(WEEKLY_TOAST_KEY);
}

export function setWeeklyReceiptToastShownFor(weekStart: string): void {
  localStorage.setItem(WEEKLY_TOAST_KEY, weekStart);
}

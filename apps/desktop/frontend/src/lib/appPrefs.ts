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
  localStorage.setItem(RECEIPTS_KEY, on ? 'true' : 'false');
}

// ---------------------------------------------------------------------------
// Weekly receipt toast — once-per-week shown tracking
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

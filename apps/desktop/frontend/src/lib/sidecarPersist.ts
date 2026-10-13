/**
 * Fire-and-forget write of a single UI preference to the Python sidecar's
 * durable config (~/.potluck/config.json → ui[key]).
 *
 * Why this is a standalone module: the stores that need write-through
 * (uiPrefsStore, chatStore, appPrefs) would create a circular import if they
 * used localApi.updateSettings - localApi imports connectionStore which
 * imports nothing from app code, but adding a cross-store dependency in the
 * other direction would chain unpredictably. Using bare fetch here, matching
 * the pattern connectionStore uses for its own persistToSidecar helper,
 * keeps the import graph acyclic.
 *
 * Callers always write to localStorage first so the session is correct even
 * when the sidecar is unreachable. This call is best-effort.
 */

const LOCAL_SIDECAR_BASE =
  (import.meta.env['VITE_LOCAL_API_URL'] as string | undefined) ?? 'http://127.0.0.1:8321';

/**
 * Persist a single UI preference key to the sidecar's durable config.
 * PATCH is deep-merged on the server so writing one key never drops another.
 */
export function persistUiPref(key: string, value: string): void {
  void fetch(`${LOCAL_SIDECAR_BASE}/settings`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ui: { [key]: value } }),
  }).catch((err: unknown) => {
    console.warn('[durablePrefs] sidecar sync failed:', err);
  });
}

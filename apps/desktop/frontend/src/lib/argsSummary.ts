/**
 * Agent-observability privacy whitelist for tool-call `args_summary`.
 *
 * Privacy invariant: `args_summary` is metadata-only. Prompt text and raw
 * argument *content* must never reach the UI. This whitelist is the single
 * source of truth and must be applied at every render site (the activity feed
 * and the session-detail drawer) - never render `args_summary` directly, even
 * if the sidecar is expected to pre-filter it, so a backend regression can't
 * leak content through the client. See the agent-observability privacy rule.
 */
export const SAFE_ARG_KEYS = ['category', 'project', 'limit', 'status', 'query', 'tool'] as const;

/**
 * Return a copy of `args_summary` containing only the whitelisted metadata
 * keys. Anything not on the whitelist is dropped. Use this before rendering
 * args anywhere (including JSON.stringify dumps).
 */
export function sanitizeArgsSummary(
  args: Record<string, unknown> | null | undefined,
): Record<string, unknown> {
  if (!args) return {};
  const safe: Record<string, unknown> = {};
  for (const key of SAFE_ARG_KEYS) {
    const val = args[key as string];
    if (val !== null && val !== undefined) safe[key] = val;
  }
  return safe;
}

/**
 * One-line `key=val · key=val` rendering for compact rows (activity feed).
 * Returns null when no whitelisted metadata is present.
 */
export function formatArgsSummary(args: Record<string, unknown> | null): string | null {
  const safe = sanitizeArgsSummary(args);
  const parts: string[] = [];
  for (const key of SAFE_ARG_KEYS) {
    if (!(key in safe)) continue;
    let display = String(safe[key]);
    // Truncate long query strings to keep the row compact.
    if (key === 'query' && display.length > 30) {
      display = `${display.slice(0, 30)}…`;
    }
    parts.push(`${key}=${display}`);
  }
  return parts.length > 0 ? parts.join(' · ') : null;
}

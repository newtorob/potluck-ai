/**
 * Time formatting utilities for the agents dashboard.
 *
 * Kept as pure functions — no side effects, no dependencies.
 * All accept ISO-8601 UTC strings (the backend contract).
 */

/**
 * Returns a human-readable relative string like "12s ago", "2m ago", "1h ago".
 * For times older than 24 hours, falls back to a locale date string.
 */
export function relativeTime(isoString: string): string {
  const now = Date.now();
  const then = new Date(isoString).getTime();
  const diffMs = now - then;

  if (diffMs < 0) return 'just now';

  const seconds = Math.floor(diffMs / 1_000);
  if (seconds < 60) return `${seconds}s ago`;

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;

  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;

  return new Date(isoString).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
  });
}

/**
 * "Connected 14m ago" / "Connected 2h ago".
 */
export function connectedDuration(startedAt: string): string {
  const now = Date.now();
  const then = new Date(startedAt).getTime();
  const diffMs = now - then;

  const seconds = Math.floor(diffMs / 1_000);
  if (seconds < 60) return `Connected ${seconds}s ago`;

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `Connected ${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `Connected ${hours}h ago`;

  const days = Math.floor(hours / 24);
  return `Connected ${days}d ago`;
}

/**
 * Format a UTC ISO string as a short local time like "14:32".
 */
export function shortTime(isoString: string): string {
  return new Date(isoString).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
  });
}

/**
 * Format a full UTC ISO string for display in detail panels.
 * e.g. "Apr 21, 2026, 14:32:07"
 */
export function fullDateTime(isoString: string): string {
  return new Date(isoString).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

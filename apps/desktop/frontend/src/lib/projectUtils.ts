/**
 * Shared utilities for project name handling.
 *
 * Rules:
 *   - Trim whitespace from both ends
 *   - Lowercase everything for consistency ("Potluck" → "potluck")
 *   - Accept: A-Za-z0-9, spaces, hyphens, underscores
 *   - Strip any other characters
 *   - Collapse multiple spaces/hyphens/underscores to a single hyphen
 *   - Return null if the result is empty (caller should abort)
 */

const ALLOWED_RE = /[^a-z0-9\s\-_]/g;
const COLLAPSE_RE = /[\s\-_]+/g;

export function normaliseProjectName(raw: string): string | null {
  const trimmed = raw.trim().toLowerCase();
  const stripped = trimmed.replace(ALLOWED_RE, '');
  const collapsed = stripped.replace(COLLAPSE_RE, '-');
  const final = collapsed.replace(/^-+|-+$/g, ''); // trim leading/trailing hyphens
  return final.length > 0 ? final : null;
}

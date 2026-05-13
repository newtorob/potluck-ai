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

// ---------------------------------------------------------------------------
// Project filter discriminated union
//
// Used by MemoryView and TaskStateView to scope their data fetches.
//   active  — resolved to the current activeProject from useProject()
//   all     — fetch across all projects (include_all=true)
//   global  — fetch only project-less memories/tasks
//   project — fetch a specific named project
// ---------------------------------------------------------------------------

export type ProjectFilter =
  | { kind: 'active' }
  | { kind: 'all' }
  | { kind: 'global' }
  | { kind: 'project'; name: string };

/**
 * Resolve a ProjectFilter to the `project` param expected by the API.
 *   - 'active' → the current activeProject value (string | null)
 *   - 'all'    → undefined (signals include_all=true to the caller)
 *   - 'global' → null  (no project)
 *   - 'project'→ the named project string
 */
export function resolveProjectFilter(
  filter: ProjectFilter,
  activeProject: string | null,
): { project: string | null | undefined; includeAll: boolean } {
  switch (filter.kind) {
    case 'active':
      return { project: activeProject, includeAll: false };
    case 'all':
      return { project: undefined, includeAll: true };
    case 'global':
      return { project: null, includeAll: false };
    case 'project':
      return { project: filter.name, includeAll: false };
  }
}

const ALLOWED_RE = /[^a-z0-9\s\-_]/g;
const COLLAPSE_RE = /[\s\-_]+/g;

export function normaliseProjectName(raw: string): string | null {
  const trimmed = raw.trim().toLowerCase();
  const stripped = trimmed.replace(ALLOWED_RE, '');
  const collapsed = stripped.replace(COLLAPSE_RE, '-');
  const final = collapsed.replace(/^-+|-+$/g, ''); // trim leading/trailing hyphens
  return final.length > 0 ? final : null;
}

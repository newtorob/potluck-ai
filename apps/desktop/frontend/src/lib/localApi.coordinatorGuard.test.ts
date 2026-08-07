/**
 * Recurrence guard: no coordinator call without the session bearer.
 *
 * `coordinatorSessionRequest` (coordinatorAuth.ts) is the only path in this
 * codebase that attaches the member session bearer to a coordinator call —
 * see cc66f83 and the credit/contribution migration that followed it. A
 * bare `nativeFetch`/`timedFetch` straight to `coordinatorBase()` silently
 * drops the bearer and 401s in prod (session auth is enforced there).
 *
 * This is a static scan, not a unit test with mocks: it reads localApi.ts's
 * own source and fails the build if a *new* bare fetch to the coordinator
 * shows up without an explicit, reviewed opt-out. Legitimately public
 * coordinator routes (health, transparency/fleet) are allow-listed inline
 * with a `coordinator-bare-fetch-ok:` comment directly above the call, so
 * this guard both documents and enforces the invariant rather than
 * special-casing routes here.
 */
import { describe, it, expect } from 'vitest';
// Vite's `?raw` import loads the file as a plain string — the simplest way
// to statically scan localApi.ts's own source without relying on node:fs
// path resolution (which is awkward to make portable under vitest's
// transformed import.meta.url).
import localApiSource from './localApi.ts?raw';

const OPT_OUT_MARKER = 'coordinator-bare-fetch-ok:';
// Bare-fetch wrappers used in this file. Matches `nativeFetch(`/`timedFetch(`
// whose first argument's template literal interpolates coordinatorBase() —
// i.e. a coordinator call that bypasses coordinatorSessionRequest.
const BARE_COORDINATOR_FETCH_RE =
  /(nativeFetch|timedFetch)\(\s*`\$\{coordinatorBase\(\)\}/;

describe('localApi coordinator call guard (static scan)', () => {
  it('every bare fetch to coordinatorBase() carries a documented opt-out', () => {
    const lines = localApiSource.split('\n');
    const violations: string[] = [];

    lines.forEach((line, i) => {
      if (!BARE_COORDINATOR_FETCH_RE.test(line)) return;
      // Opt-out marker may sit on the same line or in the small comment
      // block immediately preceding it (JSDoc or line comments).
      const context = lines.slice(Math.max(0, i - 6), i + 1).join('\n');
      if (!context.includes(OPT_OUT_MARKER)) {
        violations.push(`localApi.ts:${i + 1}: ${line.trim()}`);
      }
    });

    expect(
      violations,
      `Found bare fetch(es) to the coordinator with no session bearer and no ` +
        `'${OPT_OUT_MARKER}' opt-out comment. Route these through ` +
        `coordinatorSessionRequest (see coordinatorAuth.ts), or, if the route ` +
        `is genuinely public / uses a different auth mechanism, document why ` +
        `with an inline '${OPT_OUT_MARKER} <reason>' comment above the call:\n` +
        violations.join('\n'),
    ).toEqual([]);
  });

  it('the credit/contribution/jobs-stats routes are migrated to coordinatorSessionRequest', () => {
    // Only look at the API surface (the `localApi` object body) — the type
    // definitions above it legitimately mention these paths in JSDoc, which
    // would otherwise be mistaken for the call site.
    const apiSurfaceStart = localApiSource.indexOf('export const localApi = {');
    expect(apiSurfaceStart, 'expected to find the localApi export').toBeGreaterThan(-1);
    const src = localApiSource.slice(apiSurfaceStart);

    for (const path of [
      '/me/credit-balance',
      '/me/credit-history',
      '/me/contributions',
      '/me/contributions/weekly',
      '/jobs/stats',
    ]) {
      // The last occurrence of the path is its actual call site — the
      // history()/myContributions() JSDoc above each call also names it.
      const idx = src.lastIndexOf(path);
      expect(idx, `expected localApi.ts's API surface to call ${path}`).toBeGreaterThan(-1);
      // The nearest preceding coordinator call site for this path must be
      // coordinatorSessionRequest, not a bare fetch.
      const before = src.slice(0, idx);
      const lastSessionCall = before.lastIndexOf('coordinatorSessionRequest(');
      const lastBareFetch = Math.max(
        before.lastIndexOf('nativeFetch(`${coordinatorBase()}'),
        before.lastIndexOf('timedFetch(`${coordinatorBase()}'),
      );
      expect(
        lastSessionCall,
        `${path} should be called via coordinatorSessionRequest`,
      ).toBeGreaterThan(lastBareFetch);
    }
  });
});

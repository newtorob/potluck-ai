/**
 * Minimal semver comparator — no dependency, just enough to gate the
 * client-version handshake (running vs `minimum`/`latest` from
 * GET /client/version). Handles plain "x.y.z" releases; anything after the
 * patch number (pre-release/build metadata, e.g. "0.1.0-beta.1") is ignored
 * for comparison purposes.
 *
 * Malformed input never throws and never claims an ordering — callers that
 * gate on the result (the "must update" block) always fail open (unknown =
 * "don't block") rather than closed.
 */

const SEMVER_TRIPLET = /^(\d+)\.(\d+)\.(\d+)/;

type Triplet = readonly [number, number, number];

/** Parses the leading "x.y.z" out of a version string, or null if it doesn't look like one. */
export function parseSemverTriplet(version: string): Triplet | null {
  const match = SEMVER_TRIPLET.exec(version.trim());
  if (!match) return null;
  const [, major, minor, patch] = match;
  if (major === undefined || minor === undefined || patch === undefined) return null;
  return [Number(major), Number(minor), Number(patch)];
}

/**
 * Compares two version strings: -1 if a < b, 1 if a > b, 0 if equal OR if
 * either string fails to parse (equal is the safe default — it never signals
 * an ordering the caller can't trust).
 */
export function compareSemver(a: string, b: string): number {
  const ta = parseSemverTriplet(a);
  const tb = parseSemverTriplet(b);
  if (!ta || !tb) return 0;
  for (let i = 0; i < 3; i++) {
    const av = ta[i] ?? 0;
    const bv = tb[i] ?? 0;
    if (av !== bv) return av < bv ? -1 : 1;
  }
  return 0;
}

/**
 * True only when both `version` and `floor` parse AND `version` is strictly
 * below `floor`. Malformed input on either side resolves to false — a
 * client we can't parse a version for is never blocked from launching.
 */
export function isVersionBelow(version: string, floor: string): boolean {
  if (!parseSemverTriplet(version) || !parseSemverTriplet(floor)) return false;
  return compareSemver(version, floor) < 0;
}

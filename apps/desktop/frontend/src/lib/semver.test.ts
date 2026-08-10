import { describe, it, expect } from 'vitest';
import { compareSemver, isVersionBelow, parseSemverTriplet } from '@/lib/semver';

describe('parseSemverTriplet', () => {
  it('parses a plain x.y.z version', () => {
    expect(parseSemverTriplet('1.2.3')).toEqual([1, 2, 3]);
  });

  it('parses the leading triplet off a pre-release suffix', () => {
    expect(parseSemverTriplet('0.1.0-beta.1')).toEqual([0, 1, 0]);
  });

  it('returns null for malformed input', () => {
    expect(parseSemverTriplet('not-a-version')).toBeNull();
    expect(parseSemverTriplet('')).toBeNull();
    expect(parseSemverTriplet('1.2')).toBeNull();
  });
});

describe('compareSemver', () => {
  it('returns 0 for equal versions', () => {
    expect(compareSemver('1.2.3', '1.2.3')).toBe(0);
  });

  it('returns -1 when a < b', () => {
    expect(compareSemver('1.2.3', '1.3.0')).toBe(-1);
    expect(compareSemver('0.1.0', '0.1.1')).toBe(-1);
  });

  it('returns 1 when a > b', () => {
    expect(compareSemver('2.0.0', '1.9.9')).toBe(1);
  });

  it('treats malformed input as equal (never claims an ordering it cannot trust)', () => {
    expect(compareSemver('garbage', '1.0.0')).toBe(0);
    expect(compareSemver('1.0.0', 'garbage')).toBe(0);
  });
});

describe('isVersionBelow', () => {
  it('is true when the running version is below the floor', () => {
    expect(isVersionBelow('0.1.0', '0.2.0')).toBe(true);
  });

  it('is false when the running version equals the floor', () => {
    expect(isVersionBelow('0.2.0', '0.2.0')).toBe(false);
  });

  it('is false when the running version is above the floor', () => {
    expect(isVersionBelow('0.3.0', '0.2.0')).toBe(false);
  });

  it('never blocks on malformed input (fails open)', () => {
    expect(isVersionBelow('not-a-version', '0.2.0')).toBe(false);
    expect(isVersionBelow('0.1.0', 'not-a-version')).toBe(false);
    expect(isVersionBelow('', '')).toBe(false);
  });
});

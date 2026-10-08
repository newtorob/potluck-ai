import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  canRemovePerson,
  circleErrorMessage,
  circleRoleLabel,
  cleanInviteInput,
  formatInviteDate,
  inviteUsesLabel,
  normalizeCircle,
  peopleCountLabel,
  shortKey,
  sinceLabel,
  upsertCircle,
  withoutKey,
  withoutPerson,
} from './circles';
import type { Circle, CirclePerson } from './localApi';

function person(over: Partial<CirclePerson> = {}): CirclePerson {
  return { id: 'p-1', name: 'Ada', role: 'member', is_you: false, joined_at: '2026-10-01T12:00:00Z', ...over };
}

function circle(over: Partial<Circle> = {}): Circle {
  return {
    id: 'c-1',
    name: 'Friends',
    created_at: '2026-10-01T12:00:00Z',
    role: 'owner',
    people: [],
    members: [],
    ...over,
  };
}

describe('normalizeCircle', () => {
  it('reads a circle from an older coordinator as owned, with no people', () => {
    const out = normalizeCircle({
      id: 'c-1',
      name: 'Friends',
      created_at: '2026-10-01T12:00:00Z',
      members: [{ member_pubkey: 'abc', added_at: '2026-10-01T12:01:00Z' }],
    });
    expect(out.role).toBe('owner');
    expect(out.people).toEqual([]);
    expect(out.members).toHaveLength(1);
  });

  it('keeps the role and people a current coordinator sends', () => {
    const people = [person({ role: 'owner' }), person({ id: 'p-2', is_you: true })];
    const out = normalizeCircle({
      id: 'c-1',
      name: 'Friends',
      created_at: '2026-10-01T12:00:00Z',
      role: 'member',
      people,
      members: [],
    });
    expect(out.role).toBe('member');
    expect(out.people).toBe(people);
  });

  it('treats members left out of a member response as none', () => {
    const out = normalizeCircle({ id: 'c-1', name: 'Friends', created_at: '2026-10-01T12:00:00Z', role: 'member' });
    expect(out.members).toEqual([]);
  });
});

describe('invite input and labels', () => {
  it('trims what was pasted and nothing else', () => {
    expect(cleanInviteInput('  abcd-efgh-jkmn \n')).toBe('abcd-efgh-jkmn');
    expect(cleanInviteInput('\thttps://example.test/join/ABCD-EFGH-JKMN ')).toBe('https://example.test/join/ABCD-EFGH-JKMN');
    expect(cleanInviteInput('   ')).toBe('');
  });

  it('says which role the caller has', () => {
    expect(circleRoleLabel('owner')).toBe('You own this circle');
    expect(circleRoleLabel('member')).toBe('You are a member');
  });

  it('counts people', () => {
    expect(peopleCountLabel(1)).toBe('1 person');
    expect(peopleCountLabel(2)).toBe('2 people');
    expect(peopleCountLabel(0)).toBe('0 people');
  });

  it('reports invite use', () => {
    expect(inviteUsesLabel(0, 1)).toBe('0 of 1 used');
    expect(inviteUsesLabel(3, 5)).toBe('3 of 5 used');
  });

  it('formats an expiry as a calendar date', () => {
    expect(formatInviteDate('2026-10-15T12:00:00Z', { locale: 'en-US', timeZone: 'UTC' })).toBe('Oct 15, 2026');
  });

  it('formats nothing for a value that is not a date', () => {
    expect(formatInviteDate('soon')).toBe('');
  });

  describe('sinceLabel', () => {
    afterEach(() => vi.useRealTimers());

    it('reads a moment ago as just now rather than 0s', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-10-08T12:00:30Z'));
      expect(sinceLabel('2026-10-08T12:00:00Z')).toBe('just now');
    });

    it('counts minutes, hours and days', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-10-08T12:00:00Z'));
      expect(sinceLabel('2026-10-08T11:57:00Z')).toBe('3m ago');
      expect(sinceLabel('2026-10-08T09:00:00Z')).toBe('3h ago');
      expect(sinceLabel('2026-10-05T12:00:00Z')).toBe('3d ago');
    });
  });

  it('cuts a long key to its ends and leaves a short one alone', () => {
    expect(shortKey('ExampleDevicePublicKeyAAAAAAAAAAAAAAAAAAAAA')).toBe('ExampleDevic…AAAAAAAA');
    expect(shortKey('short-key')).toBe('short-key');
  });
});

describe('canRemovePerson', () => {
  it('lets an owner remove another member', () => {
    expect(canRemovePerson(circle(), person())).toBe(true);
  });

  it('never offers the owner row or the caller row', () => {
    expect(canRemovePerson(circle(), person({ role: 'owner' }))).toBe(false);
    expect(canRemovePerson(circle(), person({ is_you: true }))).toBe(false);
  });

  it('offers nothing to a member', () => {
    expect(canRemovePerson(circle({ role: 'member' }), person())).toBe(false);
  });
});

describe('list edits', () => {
  it('adds a circle that is not in the list yet', () => {
    const out = upsertCircle([circle({ id: 'a' })], circle({ id: 'b' }));
    expect(out.map((c) => c.id)).toEqual(['a', 'b']);
  });

  it('replaces a circle that is already there without moving it', () => {
    const out = upsertCircle([circle({ id: 'a' }), circle({ id: 'b' })], circle({ id: 'a', name: 'Renamed' }));
    expect(out.map((c) => c.name)).toEqual(['Renamed', 'Friends']);
  });

  it('drops one person by id', () => {
    const out = withoutPerson(circle({ people: [person({ id: 'p-1' }), person({ id: 'p-2' })] }), 'p-1');
    expect(out.people.map((p) => p.id)).toEqual(['p-2']);
  });

  it('drops one legacy key', () => {
    const out = withoutKey(
      circle({
        members: [
          { member_pubkey: 'k1', added_at: '2026-10-01T12:00:00Z' },
          { member_pubkey: 'k2', added_at: '2026-10-01T12:00:00Z' },
        ],
      }),
      'k1',
    );
    expect(out.members.map((m) => m.member_pubkey)).toEqual(['k2']);
  });
});

describe('circleErrorMessage', () => {
  it('shows the coordinator sentence for a refused invite', () => {
    const detail = 'That invite is not valid. It may have expired or already been used. Ask for a new one.';
    expect(circleErrorMessage(new Error(`[localApi] 404: ${detail}`), 'Could not check that invite.')).toBe(detail);
  });

  it('shows the full-circle sentence', () => {
    expect(circleErrorMessage(new Error('[localApi] 409: This circle is full.'), 'x')).toBe('This circle is full.');
  });

  it('uses the fallback when the refusal carried no readable message', () => {
    expect(circleErrorMessage(new Error('[localApi] 422: unknown error'), 'Could not create circle.')).toBe(
      'Could not create circle.',
    );
  });

  it('says the coordinator cannot be reached for a transport failure', () => {
    expect(circleErrorMessage(new Error('[localApi] 0: network error'), 'x')).toBe(
      "Can't reach the coordinator. Check your connection, then try again.",
    );
    expect(circleErrorMessage(new TypeError('Failed to fetch'), 'x')).toBe(
      "Can't reach the coordinator. Check your connection, then try again.",
    );
  });

  it('says the coordinator had an error for a 5xx', () => {
    expect(circleErrorMessage(new Error('[localApi] 503: down'), 'x')).toBe(
      'The coordinator had an error. Try again shortly.',
    );
  });

  it('asks the user to sign in again for a 401', () => {
    expect(circleErrorMessage(new Error('[localApi] 401: unauthorized'), 'x')).toBe(
      'Your sign-in has expired. Sign in again, then try again.',
    );
  });
});

/**
 * Pure helpers for trusted circles: reading what the coordinator sends,
 * labels for the Circles view, and the list edits that follow a successful
 * call. Kept free of React so they are cheap to test (see circles.test.ts).
 */

import { classifyCoordinatorError, defaultCoordinatorErrorCopy } from './coordinatorError';
import type { Circle, CirclePerson, CircleRole, RawCircle } from './localApi';
import { relativeTimeFromMs } from './timeFormat';

/**
 * Older coordinators omit `role` and `people`, and send `members` only to the
 * owner. A circle with no role came from the owner-only era, so it reads as
 * owned by the caller.
 */
export function normalizeCircle(raw: RawCircle): Circle {
  return {
    ...raw,
    role: raw.role ?? 'owner',
    people: raw.people ?? [],
    members: raw.members ?? [],
  };
}

/** The coordinator accepts the code in any case, with or without dashes, or as the whole link. Only the edges need cleaning. */
export function cleanInviteInput(text: string): string {
  return text.trim();
}

export function circleRoleLabel(role: CircleRole): string {
  return role === 'owner' ? 'You own this circle' : 'You are a member';
}

export function peopleCountLabel(count: number): string {
  return `${count} ${count === 1 ? 'person' : 'people'}`;
}

export function inviteUsesLabel(uses: number, maxUses: number): string {
  return `${uses} of ${maxUses} used`;
}

/** "just now", "3m ago", "2d ago", or "Oct 1" once it is a week old. */
export function sinceLabel(iso: string): string {
  return relativeTimeFromMs(Date.parse(iso));
}

/** "Oct 15, 2026". Empty for a value that is not a date. */
export function formatInviteDate(
  iso: string,
  opts: { locale?: string; timeZone?: string } = {},
): string {
  const time = Date.parse(iso);
  if (Number.isNaN(time)) return '';
  return new Date(time).toLocaleDateString(opts.locale, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    ...(opts.timeZone !== undefined && { timeZone: opts.timeZone }),
  });
}

/** A long machine key cut to its ends. The full key stays available as a title. */
export function shortKey(key: string): string {
  return key.length > 24 ? `${key.slice(0, 12)}…${key.slice(-8)}` : key;
}

/** Only the owner removes people, and never the owner's own row. */
export function canRemovePerson(circle: Pick<Circle, 'role'>, person: CirclePerson): boolean {
  return circle.role === 'owner' && person.role !== 'owner' && !person.is_you;
}

/** Replace the circle with the same id, or add it at the end. */
export function upsertCircle(circles: readonly Circle[], circle: Circle): Circle[] {
  return circles.some((c) => c.id === circle.id)
    ? circles.map((c) => (c.id === circle.id ? circle : c))
    : [...circles, circle];
}

export function withoutPerson(circle: Circle, personId: string): Circle {
  return { ...circle, people: circle.people.filter((p) => p.id !== personId) };
}

export function withoutKey(circle: Circle, memberPubkey: string): Circle {
  return { ...circle, members: circle.members.filter((m) => m.member_pubkey !== memberPubkey) };
}

/**
 * What to tell the user when a circle call fails. A 4xx carries the
 * coordinator's own sentence ("That invite is not valid...", "This circle is
 * full."), so it is shown as written. Transport and server failures get the
 * shared coordinator copy, and a refusal with no readable message gets the
 * caller's fallback.
 */
export function circleErrorMessage(err: unknown, fallback: string): string {
  const state = classifyCoordinatorError(err);
  switch (state.kind) {
    case 'unauthenticated':
      return 'Your sign-in has expired. Sign in again, then try again.';
    case 'forbidden':
    case 'unreachable':
    case 'server_error': {
      const copy = defaultCoordinatorErrorCopy(state);
      return `${copy.title}. ${copy.description}`;
    }
    case 'error':
      // fromSessionResult writes 'unknown error' when the body has no string `detail`.
      return state.detail.length > 0 && state.detail !== 'unknown error' ? state.detail : fallback;
  }
}

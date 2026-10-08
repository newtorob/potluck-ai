/**
 * The app reads circle responses the way the coordinator sends them.
 *
 * The Circles view is tested against a stand-in coordinator. This reads the
 * examples the real coordinator's suite pins its response models to
 * (services/coordinator/tests/fixtures/circle_api_shapes.json) and checks
 * that every field the app's types declare is there, with the type the app
 * expects. A renamed or dropped field fails here instead of in someone's app.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { normalizeCircle } from './circles';
import type {
  Circle,
  CircleAvailability,
  CircleInvite,
  CircleInviteCreated,
  CircleInvitePreview,
} from './localApi';

const fixture = JSON.parse(
  readFileSync(
    resolve(__dirname, '../../../../../services/coordinator/tests/fixtures/circle_api_shapes.json'),
    'utf8',
  ),
) as Record<string, Record<string, unknown>>;

type Kind = 'string' | 'number' | 'boolean' | 'object';

function expectFields(value: Record<string, unknown>, fields: Record<string, Kind>) {
  expect(Object.keys(value).sort()).toEqual(Object.keys(fields).sort());
  for (const [key, kind] of Object.entries(fields)) expect(typeof value[key], key).toBe(kind);
}

describe('circle responses as the coordinator sends them', () => {
  it('a circle, with its people and legacy keys', () => {
    const circle = fixture['circle'] as unknown as Circle;
    expectFields(circle as unknown as Record<string, unknown>, {
      id: 'string', name: 'string', created_at: 'string', role: 'string', people: 'object', members: 'object',
    });
    for (const person of circle.people) {
      expectFields(person as unknown as Record<string, unknown>, {
        id: 'string', name: 'string', role: 'string', is_you: 'boolean', joined_at: 'string',
      });
    }
    for (const key of circle.members) {
      expectFields(key as unknown as Record<string, unknown>, { member_pubkey: 'string', added_at: 'string' });
    }
    // What the view works from is the response itself, unchanged.
    expect(normalizeCircle(circle)).toEqual(circle);
  });

  it('a new invite, an open invite and an invite preview', () => {
    expectFields(fixture['invite_created'] as Record<string, unknown>, {
      id: 'string', code: 'string', url: 'string', expires_at: 'string', max_uses: 'number',
    } satisfies Record<keyof CircleInviteCreated, Kind>);
    expectFields(fixture['invite'] as Record<string, unknown>, {
      id: 'string', created_at: 'string', expires_at: 'string', max_uses: 'number', uses: 'number',
    } satisfies Record<keyof CircleInvite, Kind>);
    expectFields(fixture['invite_preview'] as Record<string, unknown>, {
      circle_name: 'string', invited_by: 'string', people: 'number', expires_at: 'string', already_in: 'boolean',
    } satisfies Record<keyof CircleInvitePreview, Kind>);
  });

  it('what a circle can run now', () => {
    const availability = fixture['availability'] as unknown as CircleAvailability;
    expectFields(availability as unknown as Record<string, unknown>, { circle_id: 'string', models: 'object' });
    for (const model of availability.models) {
      expectFields(model as unknown as Record<string, unknown>, { slug: 'string', machines: 'number' });
    }
  });
});

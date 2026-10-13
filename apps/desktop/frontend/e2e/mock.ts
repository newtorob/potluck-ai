import type { Page } from '@playwright/test';

/**
 * Canned member/roster fixtures matching the cloud API's camelCase shapes
 * (see services/api routers/members.py + admin.py).
 */
export const MEMBER = {
  id: 'm-1',
  email: 'sam@example.com',
  displayName: 'Sam',
  avatarUrl: null,
  membershipPlan: null,
  membershipStatus: null,
  isFoundingMember: false,
  isAdmin: false,
  joinedAt: '2026-07-01T12:00:00Z',
};

export const ADMIN = {
  ...MEMBER,
  id: 'm-admin',
  email: 'admin@example.com',
  displayName: 'Admin',
  isAdmin: true,
  isFoundingMember: true,
};

export const ADMIN_ROSTER = {
  total: 2,
  limit: 100,
  offset: 0,
  members: [
    {
      id: 'm-a',
      email: 'member-a@example.com',
      displayName: 'Member A',
      isAdmin: false,
      isFoundingMember: true,
      foundingMemberAt: '2026-06-01T00:00:00Z',
      membershipPlan: 'founding',
      membershipStatus: 'active',
      stripeCustomerId: 'cus_a',
      joinedAt: '2026-06-01T00:00:00Z',
    },
    {
      id: 'm-b',
      email: 'member-b@example.com',
      displayName: null,
      isAdmin: false,
      isFoundingMember: false,
      foundingMemberAt: null,
      membershipPlan: null,
      membershipStatus: null,
      stripeCustomerId: null,
      joinedAt: '2026-06-15T00:00:00Z',
    },
  ],
};

const DEFAULT_MEMBERSHIP = {
  plan: null,
  status: 'none',
  isFoundingMember: false,
  currentPeriodEnd: null,
};

interface MockOpts {
  /** /me response; pass null to simulate a signed-out 401. */
  me?: Record<string, unknown> | null;
  membership?: Record<string, unknown>;
  adminMembers?: typeof ADMIN_ROSTER;
  /** Override the /billing/checkout response (defaults to 503 not-configured). */
  checkout?: { status: number; body: Record<string, unknown> };
}

/**
 * The dev server runs on 127.0.0.1:5173 while the cloud client targets
 * localhost:8080 - a different origin. The app fetches with credentials, so the
 * browser drops any mocked response that lacks CORS headers. Echo the request
 * origin + allow credentials so intercepted responses are actually delivered.
 */
function corsHeaders(route: import('@playwright/test').Route): Record<string, string> {
  const origin = route.request().headers()['origin'] || '*';
  return {
    'access-control-allow-origin': origin,
    'access-control-allow-credentials': 'true',
    'access-control-allow-methods': 'GET,POST,PATCH,DELETE,OPTIONS',
    'access-control-allow-headers': 'content-type,authorization',
  };
}

function fulfillJson(
  route: import('@playwright/test').Route,
  body: unknown,
  status = 200,
): Promise<void> {
  // Answer the CORS preflight (OPTIONS) that credentialed POSTs trigger.
  if (route.request().method() === 'OPTIONS') {
    return route.fulfill({ status: 204, headers: corsHeaders(route) });
  }
  return route.fulfill({
    status,
    headers: { 'content-type': 'application/json', ...corsHeaders(route) },
    body: JSON.stringify(body),
  });
}

/**
 * Stub the backends so screens render deterministically:
 *  - localStorage flag skips the FirstRun wizard and forces the dark theme.
 *  - cloud API (:8080) returns canned auth/membership/admin/billing responses.
 *  - sidecar (:8321) is aborted; the app degrades gracefully by design.
 */
export async function mockBackends(page: Page, opts: MockOpts = {}): Promise<void> {
  await page.addInitScript(() => {
    localStorage.setItem('potluck.onboarding_completed', 'true');
    localStorage.setItem('potluck.theme', 'dark');
  });

  // Match the cloud endpoints by path on any host, so the mock works whether the
  // client's base URL resolves to localhost:8080 (dev override) or the prod host.
  await page.route(/\/me(\?.*)?$/, (route) => {
    if (opts.me == null) {
      return fulfillJson(route, { detail: 'Not authenticated' }, 401);
    }
    return fulfillJson(route, opts.me);
  });

  await page.route(/\/me\/membership(\?.*)?$/, (route) =>
    fulfillJson(route, opts.membership ?? DEFAULT_MEMBERSHIP),
  );

  await page.route(/\/admin\/members(\?.*)?$/, (route) =>
    fulfillJson(route, opts.adminMembers ?? { total: 0, limit: 100, offset: 0, members: [] }),
  );

  await page.route(/\/billing\/(checkout|portal)(\?.*)?$/, (route) =>
    fulfillJson(
      route,
      opts.checkout?.body ?? { detail: 'Billing is not configured' },
      opts.checkout?.status ?? 503,
    ),
  );

  // Sidecar unavailable in the browser harness; the app handles this gracefully.
  await page.route(/:8321\//, (route) => route.abort());
}

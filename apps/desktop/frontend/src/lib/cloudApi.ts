/**
 * HTTP client for the cloud API at api.trypotluck.ai.
 *
 * Handles: auth (magic link), member profile, membership status,
 * billing (Stripe checkout + portal), and governance (proposals + voting).
 *
 * IMPORTANT: This API never receives chat messages or model weights.
 * Only account and membership data crosses the network.
 *
 * Transport is dual-path (see `request` below): the packaged app renders from
 * file:// where browser CORS blocks direct calls, so it proxies through the
 * Electron main process (`potluck.cloud`) with a bearer token held in main; the
 * dev renderer runs on http://localhost:5173 and uses plain fetch + cookie.
 * See guide §4.5 and services/api/ for the server-side counterpart.
 */

import type {
  MemberProfile,
  Session,
  GovernanceProposal,
  MembershipPlan,
  AdminMembersPage,
  AdminMemberSummary,
} from '@/types/api';

const CLOUD_BASE =
  (import.meta.env['VITE_CLOUD_API_URL'] as string | undefined) ??
  'https://api.trypotluck.ai';

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/** The Electron main-process cloud bridge, present only in the packaged app. */
interface CloudBridge {
  request: (
    method: string,
    path: string,
    body?: unknown,
  ) => Promise<{ status: number; body: unknown }>;
  setToken: (token: string) => Promise<void>;
  clearToken: () => Promise<void>;
}

/**
 * Return the main-process bridge when we must route through it (packaged app,
 * file:// origin, where browser CORS blocks direct calls). In dev the renderer
 * is http://localhost:5173 (CORS-allowlisted), so this is undefined and we use
 * plain fetch + cookie.
 */
function cloudBridge(): CloudBridge | undefined {
  if (typeof window === 'undefined' || window.location.protocol !== 'file:') {
    return undefined;
  }
  return (window as unknown as { potluck?: { cloud?: CloudBridge } }).potluck?.cloud;
}

function detailOf(body: unknown, fallback: string): string {
  return typeof body === 'object' && body !== null && 'detail' in body
    ? String(body.detail)
    : fallback;
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body: unknown = await res.json().catch(() => ({}));
    throw new Error(`[cloudApi] ${res.status}: ${detailOf(body, res.statusText)}`);
  }
  if (res.status === 204) return undefined as T;
  const data: unknown = await res.json().catch(() => undefined);
  return data as T;
}

async function request<T>(
  method: 'GET' | 'POST' | 'DELETE',
  path: string,
  body?: unknown,
): Promise<T> {
  const bridge = cloudBridge();
  if (bridge) {
    const { status, body: resBody } = await bridge.request(method, path, body);
    if (status < 200 || status >= 300) {
      throw new Error(`[cloudApi] ${status}: ${detailOf(resBody, `HTTP ${status}`)}`);
    }
    return resBody as T;
  }
  const init: RequestInit = { method, credentials: 'include' };
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  return fetch(`${CLOUD_BASE}${path}`, init).then((r) => handleResponse<T>(r));
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>('POST', path, body);
}

function get<T>(path: string): Promise<T> {
  return request<T>('GET', path);
}

function del<T>(path: string): Promise<T> {
  return request<T>('DELETE', path);
}

// ---------------------------------------------------------------------------
// API surface
// ---------------------------------------------------------------------------

export const cloudApi = {
  // ---- Auth ----------------------------------------------------------------

  /**
   * POST /auth/request-link — sends a magic link to the user's email.
   * Returns immediately; the link is delivered asynchronously via Resend.
   */
  requestMagicLink(email: string): Promise<{ status: string }> {
    return post<{ status: string }>('/auth/request-link', { email });
  },

  /**
   * GET /auth/callback?token=... — exchanges the one-time token for a session.
   *
   * SECURITY: does NOT persist the session. A potluck://auth/callback?token=
   * deep link can hand this app ANY token — including a valid one for an
   * attacker's own account (request a magic link to yourself, wrap it as a
   * deep link, get the victim to click it) — so the exchange result must be
   * confirmed by the user (see useAuth.confirmSignIn) before it becomes the
   * active session. Callers must call `commitSession()` only after that
   * confirmation; on cancel, simply drop the returned session.
   *
   * The web/dev cookie transport is the one exception: the server sets the
   * httpOnly cookie as a side effect of this GET itself, before we ever see
   * the response. If the caller cancels, call `logout()` to revoke it.
   */
  async verifyMagicLink(token: string): Promise<Session> {
    return request<Session>('GET', `/auth/callback?token=${encodeURIComponent(token)}`);
  },

  /**
   * Persist a verified session as the active one. Call ONLY after the user
   * has explicitly confirmed the account from `verifyMagicLink`'s result —
   * this is the one call that turns a token exchange into a signed-in app.
   */
  async commitSession(session: Session): Promise<void> {
    const bridge = cloudBridge();
    if (bridge && session.token) {
      await bridge.setToken(session.token);
    }
    // Cookie transport (dev/browser): the httpOnly cookie was already set by
    // the /auth/callback response itself — nothing further to persist here.
  },

  /** POST /auth/logout — revokes the server-side session and clears the token */
  async logout(): Promise<void> {
    try {
      await request<void>('POST', '/auth/logout');
    } finally {
      await cloudBridge()?.clearToken();
    }
  },

  // ---- Member --------------------------------------------------------------

  /** GET /me — fetches the current member's profile */
  getMe(): Promise<MemberProfile> {
    return get<MemberProfile>('/me');
  },

  /** GET /me/membership — fetches plan and status details */
  getMembership(): Promise<{
    plan: MembershipPlan | null;
    status: string;
    isFoundingMember: boolean;
    currentPeriodEnd: string | null;
  }> {
    return get('/me/membership');
  },

  // ---- Billing -------------------------------------------------------------

  /**
   * POST /billing/checkout — creates a Stripe Checkout session.
   * Returns a URL to open in an external browser.
   */
  createCheckout(plan: MembershipPlan): Promise<{ url: string }> {
    return post<{ url: string }>('/billing/checkout', { plan });
  },

  /**
   * POST /billing/portal — creates a Stripe Billing Portal session.
   * Returns a URL to open in an external browser for subscription management.
   */
  getBillingPortal(): Promise<{ url: string }> {
    return post<{ url: string }>('/billing/portal');
  },

  // ---- Governance ----------------------------------------------------------

  /** GET /proposals — lists all active and recent governance proposals */
  getProposals(): Promise<GovernanceProposal[]> {
    return get<GovernanceProposal[]>('/proposals');
  },

  /**
   * POST /votes/:proposalId — cast or change a vote.
   * @param choice  'for' | 'against'
   */
  vote(proposalId: string, choice: 'for' | 'against'): Promise<{ status: string }> {
    return post<{ status: string }>(`/votes/${encodeURIComponent(proposalId)}`, { choice });
  },

  // ---- Admin ---------------------------------------------------------------
  // All admin routes require an admin session (is_admin). A non-admin session
  // gets 403; an unauthenticated one gets 401. See services/api routers/admin.py.

  /** GET /admin/members — paginated member roster with latest membership. */
  adminListMembers(limit = 100, offset = 0): Promise<AdminMembersPage> {
    return get<AdminMembersPage>(`/admin/members?limit=${limit}&offset=${offset}`);
  },

  /**
   * POST /admin/members/:id/grant-founding — stamp founding status.
   * Idempotent; `granted` is false if the member was already founding.
   */
  adminGrantFounding(
    memberId: string,
  ): Promise<{ granted: boolean; member: AdminMemberSummary }> {
    return post<{ granted: boolean; member: AdminMemberSummary }>(
      `/admin/members/${encodeURIComponent(memberId)}/grant-founding`,
    );
  },

  /**
   * DELETE /admin/members/:id/founding — revoke founding status.
   * Idempotent; `revoked` is false if the member was not founding.
   */
  adminRevokeFounding(
    memberId: string,
  ): Promise<{ revoked: boolean; member: AdminMemberSummary }> {
    return del<{ revoked: boolean; member: AdminMemberSummary }>(
      `/admin/members/${encodeURIComponent(memberId)}/founding`,
    );
  },
};

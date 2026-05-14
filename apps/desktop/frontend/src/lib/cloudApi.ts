/**
 * HTTP client for the cloud API at api.trypotluck.ai.
 *
 * Handles: auth (magic link), member profile, membership status,
 * billing (Stripe checkout + portal), and governance (proposals + voting).
 *
 * IMPORTANT: This API never receives chat messages or model weights.
 * Only account and membership data crosses the network.
 *
 * Session is maintained via httpOnly cookies (credentials: 'include').
 * See guide §4.5 and services/api/ for the server-side counterpart.
 */

import type {
  MemberProfile,
  Session,
  GovernanceProposal,
  MembershipPlan,
} from '@/types/api';

const CLOUD_BASE =
  (import.meta.env['VITE_CLOUD_API_URL'] as string | undefined) ??
  'https://api.trypotluck.ai';

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body: unknown = await res.json().catch(() => ({}));
    const msg =
      typeof body === 'object' && body !== null && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : res.statusText;
    throw new Error(`[cloudApi] ${res.status}: ${msg}`);
  }
  return res.json() as Promise<T>;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  const init: RequestInit = {
    method: 'POST',
    credentials: 'include',
  };
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  return fetch(`${CLOUD_BASE}${path}`, init).then((r) => handleResponse<T>(r));
}

function get<T>(path: string): Promise<T> {
  return fetch(`${CLOUD_BASE}${path}`, {
    credentials: 'include',
  }).then((r) => handleResponse<T>(r));
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
   * The server sets an httpOnly session cookie; this returns the session metadata.
   */
  verifyMagicLink(token: string): Promise<Session> {
    return fetch(`${CLOUD_BASE}/auth/callback?token=${encodeURIComponent(token)}`, {
      credentials: 'include',
    }).then((r) => handleResponse<Session>(r));
  },

  /** POST /auth/logout — clears the server-side session and cookie */
  logout(): Promise<void> {
    return fetch(`${CLOUD_BASE}/auth/logout`, {
      method: 'POST',
      credentials: 'include',
    }).then((r) => {
      if (!r.ok) throw new Error(`[cloudApi] logout failed: ${r.status}`);
    });
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
};

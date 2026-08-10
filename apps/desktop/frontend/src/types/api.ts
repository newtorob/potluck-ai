/**
 * Shared API types — used by both localApi and cloudApi clients.
 */

export interface ApiError {
  /** HTTP status code */
  status: number;
  /** Machine-readable error code, e.g. "model_not_found" */
  code: string;
  /** Human-readable message safe to display */
  message: string;
  /** Optional structured details for debugging */
  detail?: unknown;
}

/** Discriminated union for API call results — avoids try/catch at call sites */
export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: ApiError };

/** Membership plan identifiers as used by Stripe and the cloud API */
export type MembershipPlan = 'founding' | 'monthly';

export type MembershipStatus = 'active' | 'lapsed' | 'cancelled' | 'trialing';

export interface MemberProfile {
  id: string;
  email: string;
  displayName: string | null;
  avatarUrl: string | null;
  membershipPlan: MembershipPlan | null;
  membershipStatus: MembershipStatus | null;
  isFoundingMember: boolean;
  isAdmin: boolean;
  joinedAt: string;
}

/** One row in the admin member list (superset of MemberProfile). */
export interface AdminMemberSummary {
  id: string;
  email: string;
  displayName: string | null;
  isAdmin: boolean;
  isFoundingMember: boolean;
  foundingMemberAt: string | null;
  membershipPlan: MembershipPlan | null;
  membershipStatus: string | null;
  stripeCustomerId: string | null;
  joinedAt: string;
}

/** Paginated response from GET /admin/members. */
export interface AdminMembersPage {
  total: number;
  limit: number;
  offset: number;
  members: AdminMemberSummary[];
}

export interface Session {
  token: string;
  expiresAt: string;
  memberId: string;
  /**
   * The account email, when the server includes it on /auth/callback.
   * Older API responses may omit this — callers must not assume it's
   * present (see the sign-in confirmation prompt in useAuth/authStore,
   * which falls back to a generic message when it's absent).
   */
  email?: string;
}

/** GET /client/version — public, unauthenticated version handshake. */
export interface ClientVersionInfo {
  /** Newest published desktop version. */
  latest: string;
  /** Oldest desktop version still allowed to run; below this, the app blocks. */
  minimum: string;
  /** Optional link to release notes / the download page. */
  notesUrl?: string;
}

export interface GovernanceProposal {
  id: string;
  title: string;
  summary: string;
  status: 'open' | 'closed' | 'passed' | 'rejected';
  votesFor: number;
  votesAgainst: number;
  closesAt: string;
  myVote: 'for' | 'against' | null;
}

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
  joinedAt: string;
}

export interface Session {
  token: string;
  expiresAt: string;
  memberId: string;
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

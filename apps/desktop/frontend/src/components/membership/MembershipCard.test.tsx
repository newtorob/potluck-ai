import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// Hoisted mutable holders so each test can set the auth/membership state the
// card renders against, and assert on the billing actions it invokes.
const h = vi.hoisted(() => ({
  startCheckout: vi.fn(),
  openBillingPortal: vi.fn(),
  signOut: vi.fn(),
  membership: null as unknown,
  memberProfile: null as unknown,
}));

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ memberProfile: h.memberProfile, signOut: h.signOut }),
}));
vi.mock('@/hooks/useMembership', () => ({
  useMembership: () => ({
    membership: h.membership,
    openBillingPortal: h.openBillingPortal,
    startCheckout: h.startCheckout,
  }),
}));

import { MembershipCard } from '@/components/membership/MembershipCard';

beforeEach(() => {
  h.startCheckout.mockReset().mockResolvedValue(undefined);
  h.openBillingPortal.mockReset().mockResolvedValue(undefined);
  h.signOut.mockReset().mockResolvedValue(undefined);
  h.memberProfile = { email: 'x@example.com', isFoundingMember: false, isAdmin: false };
  h.membership = { plan: null, status: 'none', isFoundingMember: false, currentPeriodEnd: null };
});

describe('MembershipCard', () => {
  it('offers the founding checkout CTA when there is no active plan', () => {
    render(<MembershipCard />);
    expect(screen.getByRole('button', { name: /become a founding member/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /manage billing/i })).not.toBeInTheDocument();
  });

  it('starts founding checkout when the CTA is clicked', async () => {
    render(<MembershipCard />);
    await userEvent.click(screen.getByRole('button', { name: /become a founding member/i }));
    expect(h.startCheckout).toHaveBeenCalledWith('founding');
  });

  it('offers manage-billing (not checkout) for an active plan', () => {
    h.membership = { plan: 'monthly', status: 'active', isFoundingMember: false, currentPeriodEnd: null };
    render(<MembershipCard />);
    expect(screen.getByRole('button', { name: /manage billing/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /become a founding member/i })).not.toBeInTheDocument();
  });

  it('renders the founding badge for a founding member', () => {
    h.memberProfile = { email: 'x@example.com', isFoundingMember: true, isAdmin: false };
    h.membership = { plan: 'founding', status: 'active', isFoundingMember: true, currentPeriodEnd: null };
    render(<MembershipCard />);
    expect(screen.getByText(/founding badge/i)).toBeInTheDocument();
  });

  it('shows an honest fallback when checkout is unavailable', async () => {
    h.startCheckout.mockRejectedValueOnce(new Error('503'));
    render(<MembershipCard />);
    await userEvent.click(screen.getByRole('button', { name: /become a founding member/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/isn't available yet/i);
  });
});

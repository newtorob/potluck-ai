/**
 * AuthGate — required sign-in wall coverage.
 *
 * Exercises the three states described in AuthGate's docstring, with a
 * particular focus on the offline case: a cached account (persisted token,
 * getMe() failed for a network reason) must render the app, not the wall.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const h = vi.hoisted(() => ({
  isAuthLoading: false,
  isSignedIn: false,
  hasCachedAccount: false,
}));

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    isAuthLoading: h.isAuthLoading,
    isSignedIn: h.isSignedIn,
    hasCachedAccount: h.hasCachedAccount,
    pendingSession: null,
    authError: null,
    confirmSignIn: vi.fn(),
    cancelSignIn: vi.fn(),
    requestMagicLink: vi.fn(),
  }),
}));

vi.mock('@/hooks/useDeepLinkAuth', () => ({
  useDeepLinkAuth: vi.fn(),
}));

import { AuthGate } from '@/components/auth/AuthGate';

function renderGate() {
  return render(
    <MemoryRouter>
      <AuthGate>
        <div>App content</div>
      </AuthGate>
    </MemoryRouter>,
  );
}

describe('AuthGate', () => {
  beforeEach(() => {
    h.isAuthLoading = false;
    h.isSignedIn = false;
    h.hasCachedAccount = false;
  });

  it('shows a neutral loading state while the probe is in flight — never the wall or the app', () => {
    h.isAuthLoading = true;
    renderGate();
    expect(screen.getByRole('status', { name: /loading/i })).toBeInTheDocument();
    expect(screen.queryByText('App content')).not.toBeInTheDocument();
    expect(screen.queryByText(/sign in to use potluck/i)).not.toBeInTheDocument();
  });

  it('shows the sign-in wall when resolved signed-out with no cached account', () => {
    renderGate();
    expect(screen.getByText(/sign in to use potluck/i)).toBeInTheDocument();
    expect(screen.queryByText('App content')).not.toBeInTheDocument();
  });

  it('renders children when signed in', () => {
    h.isSignedIn = true;
    renderGate();
    expect(screen.getByText('App content')).toBeInTheDocument();
    expect(screen.queryByText(/sign in to use potluck/i)).not.toBeInTheDocument();
  });

  it('renders children (offline mode) for a cached account, even though not actively signed in', () => {
    h.hasCachedAccount = true;
    renderGate();
    expect(screen.getByText('App content')).toBeInTheDocument();
    expect(screen.queryByText(/sign in to use potluck/i)).not.toBeInTheDocument();
  });
});

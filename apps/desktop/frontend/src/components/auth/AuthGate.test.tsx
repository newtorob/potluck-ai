/**
 * AuthGate - required sign-in wall coverage.
 *
 * Exercises the three states described in AuthGate's docstring, with a
 * particular focus on the offline case: a cached account (persisted token,
 * getMe() failed for a network reason) must render the app, not the wall.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
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

import { useUiPrefsStore } from '@/stores/uiPrefsStore';
import { useChatStore } from '@/stores/chatStore';
import { persistUiPref } from '@/lib/sidecarPersist';
vi.mock('@/lib/sidecarPersist', () => ({ persistUiPref: vi.fn() }));
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
    useUiPrefsStore.setState({ localUseEnabled: false });
    h.isAuthLoading = false;
    h.isSignedIn = false;
    h.hasCachedAccount = false;
  });

  it('offers immediate local entry while the account probe is still in flight', () => {
    h.isAuthLoading = true;
    useChatStore.setState({ routingScope: 'circle', activeCircleId: 'keep-circle' });
    renderGate();
    expect(screen.getByRole('status')).toHaveTextContent('Checking your account');
    fireEvent.click(screen.getByRole('button', { name: 'Use on this machine' }));
    expect(screen.getByText('App content')).toBeInTheDocument();
    expect(useUiPrefsStore.getState().localUseEnabled).toBe(true);
    expect(localStorage.getItem('potluck.local_use_enabled')).toBe('true');
    expect(persistUiPref).toHaveBeenCalledWith('potluck.local_use_enabled', 'true');
    expect(useChatStore.getState().routingScope).toBe('local');
    expect(useChatStore.getState().activeCircleId).toBe('keep-circle');
  });

  it('keeps local content mounted during a later sign-in probe', () => {
    useUiPrefsStore.setState({ localUseEnabled: true });
    h.isAuthLoading = true;
    renderGate();
    expect(screen.getByText('App content')).toBeInTheDocument();
    expect(screen.queryByText(/start with potluck/i)).not.toBeInTheDocument();
  });

  it('shows the sign-in wall when resolved signed-out with no cached account', () => {
    renderGate();
    expect(screen.getByText(/start with potluck/i)).toBeInTheDocument();
    expect(screen.queryByText('App content')).not.toBeInTheDocument();
  });

  it('renders children when signed in', () => {
    h.isSignedIn = true;
    renderGate();
    expect(screen.getByText('App content')).toBeInTheDocument();
    expect(screen.queryByText(/start with potluck/i)).not.toBeInTheDocument();
  });

  it('renders children (offline mode) for a cached account, even though not actively signed in', () => {
    h.hasCachedAccount = true;
    renderGate();
    expect(screen.getByText('App content')).toBeInTheDocument();
    expect(screen.queryByText(/start with potluck/i)).not.toBeInTheDocument();
  });
});

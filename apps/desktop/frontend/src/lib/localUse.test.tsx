import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

const mocks = vi.hoisted(() => ({
  settings: vi.fn(), persist: vi.fn(), getMe: vi.fn(), commit: vi.fn(), verify: vi.fn(),
}));
vi.mock('@/lib/localApi', () => ({ localApi: { getSettings: mocks.settings } }));
vi.mock('@/lib/sidecarPersist', () => ({ persistUiPref: mocks.persist }));
vi.mock('@/lib/cloudApi', () => ({
  CloudApiError: class extends Error { status = 0; },
  cloudApi: { hasToken: async () => false, getMe: mocks.getMe, commitSession: mocks.commit,
    verifyMagicLink: mocks.verify, logout: async () => {}, clearToken: async () => {} },
}));

import { continueLocally } from './localUse';
import { hydrateDurablePrefs } from './durablePrefs';
import { AccountRequired } from '@/components/auth/AccountRequired';
import { useAuth } from '@/hooks/useAuth';
import { hasAccountAccess, useAuthStore } from '@/stores/authStore';
import { useChatStore } from '@/stores/chatStore';
import { useUiPrefsStore } from '@/stores/uiPrefsStore';
import { useSettingsStore } from '@/stores/settingsStore';

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  useAuthStore.setState({ session: null, memberProfile: null, hasPersistedToken: false,
    authFailureKind: null, isAuthLoading: false, pendingSession: null });
  useUiPrefsStore.setState({ localUseEnabled: false, localUseRevision: 0 });
  useChatStore.setState({ routingScope: 'local', activeCircleId: 'remember-circle', conversations: {}, activeConversationId: null });
});

const durable = (ui: Record<string, string>) => ({ network: { coordinator_url: '' }, ui });

describe('local entry and durable preferences', () => {
  it('keeps a new local choice when older remote routing finishes loading later', async () => {
    let finish!: (settings: unknown) => void;
    mocks.settings.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const hydration = hydrateDurablePrefs();
    continueLocally();
    finish(durable({ 'potluck.routing_scope': 'circle', 'potluck.local_use_enabled': 'false' }));
    await hydration;
    expect(useChatStore.getState().routingScope).toBe('local');
    expect(useChatStore.getState().activeCircleId).toBe('remember-circle');
    expect(useUiPrefsStore.getState().localUseEnabled).toBe(true);
  });

  it('restores the account-free entry choice after browser storage is cleared', async () => {
    useChatStore.setState({ routingScope: 'circle' });
    mocks.settings.mockResolvedValue(durable({ 'potluck.local_use_enabled': 'true', 'potluck.routing_scope': 'cooperative' }));
    await hydrateDurablePrefs();
    expect(useUiPrefsStore.getState().localUseEnabled).toBe(true);
    // Preserve the saved choice; UI and dispatch clamp it until an explicit exit from local use.
    expect(useChatStore.getState().routingScope).toBe('circle');
  });

  it('does not undo an explicit remote choice when an older local preference arrives', async () => {
    let finish!: (settings: unknown) => void;
    mocks.settings.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const hydration = hydrateDurablePrefs();
    useChatStore.getState().setRoutingScope('household');
    useUiPrefsStore.getState().setLocalUseEnabled(false);
    finish(durable({ 'potluck.local_use_enabled': 'true', 'potluck.routing_scope': 'local' }));
    await hydration;
    expect(useUiPrefsStore.getState().localUseEnabled).toBe(false);
    expect(useChatStore.getState().routingScope).toBe('household');
  });

  it('accepts a durable false over stale browser storage when there is no new choice', async () => {
    useUiPrefsStore.setState({ localUseEnabled: true });
    mocks.settings.mockResolvedValue(durable({ 'potluck.local_use_enabled': 'false', 'potluck.routing_scope': 'circle' }));
    await hydrateDurablePrefs();
    expect(useUiPrefsStore.getState().localUseEnabled).toBe(false);
    expect(useChatStore.getState().routingScope).toBe('circle');
  });

  it('still restores saved routing for members who have not chosen local entry', async () => {
    mocks.settings.mockResolvedValue(durable({ 'potluck.routing_scope': 'circle' }));
    await hydrateDurablePrefs();
    expect(useChatStore.getState().routingScope).toBe('circle');
  });
});

it('asks for sign-in without mounting account-backed controls, including pending accounts', () => {
  const mounted = vi.fn();
  function NetworkControls() { mounted(); return <div>Connected machines</div>; }
  useAuthStore.setState({ pendingSession: { memberId: 'pending', token: 'unconfirmed', expiresAt: '' } });
  const view = render(<MemoryRouter><AccountRequired feature="connect your machines"><NetworkControls /></AccountRequired></MemoryRouter>);
  expect(mounted).not.toHaveBeenCalled();
  expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/membership');
  act(() => useAuthStore.setState({ session: { memberId: 'member', token: '', expiresAt: '' } }));
  expect(screen.getByText('Connected machines')).toBeInTheDocument();
  view.unmount();
});

it('keeps local history and settings through verified, explicitly confirmed sign-in', async () => {
  mocks.getMe.mockRejectedValueOnce(new Error('offline'));
  const { result } = renderHook(() => useAuth());
  await waitFor(() => expect(useAuthStore.getState().authFailureKind).toBe('network'));
  continueLocally();
  useChatStore.setState({ conversations: {
    local: { id: 'local', title: 'Keep me', modelSlug: null, createdAt: 1, updatedAt: 1, persisted: true,
      messages: [{ id: 'message', role: 'user', content: 'Local history', createdAt: 1, status: 'complete' }] },
  }, activeConversationId: 'local' });
  const conversations = useChatStore.getState().conversations;
  const settings = useSettingsStore.getState().settings;
  const session = { memberId: 'member', token: 'test-session', expiresAt: '', email: 'member@example.test' };
  mocks.verify.mockResolvedValue(session);
  mocks.getMe.mockResolvedValue({ id: 'member', email: 'member@example.test', displayName: null,
    avatarUrl: null, membershipPlan: null, membershipStatus: null, isFoundingMember: false, isAdmin: false, joinedAt: '' });
  await act(async () => { await result.current.verifyMagicLink('test-link'); });
  expect(hasAccountAccess(useAuthStore.getState())).toBe(false);
  expect(mocks.commit).not.toHaveBeenCalled();
  await act(async () => { await result.current.confirmSignIn(); });
  expect(mocks.commit).toHaveBeenCalledWith(session);
  expect(hasAccountAccess(useAuthStore.getState())).toBe(true);
  expect(useChatStore.getState().conversations).toBe(conversations);
  expect(useChatStore.getState().activeConversationId).toBe('local');
  expect(useChatStore.getState().activeCircleId).toBe('remember-circle');
  expect(useChatStore.getState().routingScope).toBe('local');
  expect(useSettingsStore.getState().settings).toBe(settings);
  expect(useUiPrefsStore.getState().localUseEnabled).toBe(true);
});

it('removes cached account access on explicit sign-out', () => {
  useAuthStore.setState({ hasPersistedToken: true, authFailureKind: 'network' });
  expect(hasAccountAccess(useAuthStore.getState())).toBe(true);
  useAuthStore.getState().clearSession();
  expect(hasAccountAccess(useAuthStore.getState())).toBe(false);
});

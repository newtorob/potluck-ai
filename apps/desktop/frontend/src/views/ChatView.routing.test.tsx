import { useUiPrefsStore } from '@/stores/uiPrefsStore';
import { useAuthStore } from '@/stores/authStore';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const mocks = vi.hoisted(() => ({
  coordinator: { reachable: true, lastCheckedAt: 1 as number | null },
  modelLoaded: true,
  loadedRpcServers: [] as string[],
  isStreaming: false,
  send: vi.fn(),
  abort: vi.fn(),
  circles: vi.fn(),
  circleModels: vi.fn(),
  meshStart: vi.fn(),
}));

vi.mock('@/hooks/useChat', () => ({
  useChat: () => ({
    messages: [],
    isStreaming: mocks.isStreaming,
    send: mocks.send,
    abort: mocks.abort,
  }),
}));
vi.mock('@/hooks/useHealth', () => ({
  useHealth: () => ({ modelLoaded: mocks.modelLoaded, nCtxLoaded: 8192, loadedRpcServers: mocks.loadedRpcServers, loadedPipeline: null }),
}));
vi.mock('@/hooks/useSettings', () => ({
  useSettings: () => ({ settings: null, isLoaded: false }),
}));
vi.mock('@/hooks/useCoordinatorHealth', () => ({
  useCoordinatorHealth: () => mocks.coordinator,
}));
vi.mock('@/components/chat/ModelSelector', () => ({ ModelSelector: () => null }));
vi.mock('@/lib/sidecarPersist', () => ({ persistUiPref: vi.fn() }));
vi.mock('@/lib/localApi', () => ({
  localApi: { circles: { list: mocks.circles, availability: mocks.circleModels } },
}));

import { ChatView } from './ChatView';
import { CooperativeConnectionPanel } from '@/components/settings/CooperativeConnectionPanel';
import { useChatStore } from '@/stores/chatStore';
import { useMeshStore } from '@/stores/meshStore';

const chat = () => (
  <MemoryRouter>
    <ChatView />
  </MemoryRouter>
);

async function openScopes() {
  await waitFor(() => expect(useChatStore.getState().activeCircleId).toBe('circle-1'));
  fireEvent.click(screen.getByRole('button', { name: /Routing scope:/ }));
  return within(screen.getByRole('listbox', { name: 'Routing scope' }));
}

beforeEach(() => {
  useUiPrefsStore.setState({ localUseEnabled: false });
  useAuthStore.setState({ session: { memberId: 'member', token: '', expiresAt: '' }, hasPersistedToken: false, authFailureKind: null });
  mocks.loadedRpcServers = [];
  mocks.coordinator = { reachable: true, lastCheckedAt: 1 };
  mocks.modelLoaded = true;
  mocks.isStreaming = false;
  mocks.send.mockReset().mockResolvedValue(undefined);
  mocks.abort.mockReset();
  mocks.meshStart.mockReset();
  mocks.circles.mockReset().mockResolvedValue({ circles: [{ id: 'circle-1', name: 'Team' }] });
  mocks.circleModels.mockReset().mockResolvedValue({ models: [] });
  useChatStore.setState({
    routingScope: 'local',
    networkMode: false,
    householdMode: false,
    activeCircleId: null,
    networkModelSlug: 'example-model',
  });
  useMeshStore.setState({ peers: [], status: null, bridge: 'reachable', start: mocks.meshStart });
});

describe('Chat routing disclosure and recovery', () => {
  it('discloses contributor and coordinator access without promising anonymity', async () => {
    render(chat());
    const scopes = await openScopes();
    const network = scopes.getByRole('option', { name: /Cooperative network/ });
    expect(network).toHaveTextContent(
      'The contributor and Potluck’s coordinator can read the prompt you send.',
    );
    expect(network).toHaveTextContent('omits your account identity');
    expect(network).toHaveTextContent('your prompt may identify you');
    expect(network).not.toHaveTextContent('do not know it came from you');
    expect(useChatStore.getState().routingScope).toBe('local');
    expect(mocks.send).not.toHaveBeenCalled();

    fireEvent.click(network);
    expect(useChatStore.getState().routingScope).toBe('cooperative');
    expect(
      screen.getByText(/The contributor and Potluck’s coordinator can read/),
    ).toHaveTextContent('your prompt may identify you');
    expect(mocks.send).not.toHaveBeenCalled();
  });

  it('explains circle operator access and conditional coordinator access before choosing it', async () => {
    render(chat());
    const scopes = await openScopes();
    const circle = scopes.getByRole('option', { name: /Trusted circle/ });
    expect(circle).toHaveTextContent(
      'The member operating the answering machine can read your conversation.',
    );
    expect(circle).toHaveTextContent('coordinator can too when relaying it');
    expect(circle).toHaveTextContent('Its contents may identify you');

    fireEvent.click(circle);
    expect(useChatStore.getState().routingScope).toBe('circle');
    expect(useChatStore.getState().activeCircleId).toBe('circle-1');
    expect(screen.getByText(/The member operating the answering machine/)).toHaveTextContent(
      'coordinator can too when relaying it',
    );
    await waitFor(() => expect(mocks.circleModels).toHaveBeenCalledWith('circle-1'));
    expect(mocks.send).not.toHaveBeenCalled();
  });

  it('keeps an unavailable network disabled and points to automatic retry and local chat', async () => {
    mocks.coordinator = { reachable: false, lastCheckedAt: 1 };
    render(chat());
    const scopes = await openScopes();
    const network = scopes.getByRole('option', { name: /Cooperative network/ });
    expect(network).toBeDisabled();
    expect(network).toHaveTextContent('Check your connection; Potluck retries automatically.');
    expect(network).toHaveTextContent('Choose Local only to chat with a loaded model.');
    expect(network).not.toHaveTextContent('make up');
    fireEvent.click(network);
    expect(useChatStore.getState().routingScope).toBe('local');
  });

  it('preserves the draft on an outage and requires an explicit choice to resume network routing', async () => {
    useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
    const { rerender } = render(chat());
    const input = screen.getByRole('textbox', { name: 'Chat message' });
    fireEvent.change(input, { target: { value: 'Keep this draft' } });

    mocks.coordinator = { reachable: false, lastCheckedAt: 2 };
    rerender(chat());
    await waitFor(() => expect(useChatStore.getState().routingScope).toBe('local'));
    expect(input).toHaveValue('Keep this draft');
    expect(screen.getByRole('button', { name: 'Send message' })).toBeEnabled();

    mocks.coordinator = { reachable: true, lastCheckedAt: 3 };
    rerender(chat());
    expect(useChatStore.getState().routingScope).toBe('local');
    expect(mocks.send).not.toHaveBeenCalled();
    const scopes = await openScopes();
    fireEvent.click(scopes.getByRole('option', { name: /Cooperative network/ }));
    expect(useChatStore.getState().routingScope).toBe('cooperative');
  });

  it('does not allow sending after fallback if no local model is loaded', async () => {
    mocks.modelLoaded = false;
    useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
    const { rerender } = render(chat());
    fireEvent.change(screen.getByRole('textbox', { name: 'Chat message' }), {
      target: { value: 'Keep this draft too' },
    });
    mocks.coordinator = { reachable: false, lastCheckedAt: 2 };
    rerender(chat());
    await waitFor(() => expect(useChatStore.getState().routingScope).toBe('local'));
    expect(screen.getByRole('textbox', { name: 'Chat message' })).toHaveValue(
      'Keep this draft too',
    );
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
    expect(mocks.send).not.toHaveBeenCalled();
  });

  it('waits for the first reachability result before falling back and locks scope while streaming', async () => {
    mocks.coordinator = { reachable: false, lastCheckedAt: null };
    mocks.isStreaming = true;
    useChatStore.setState({ routingScope: 'cooperative', networkMode: true });
    render(chat());
    await waitFor(() => expect(useChatStore.getState().activeCircleId).toBe('circle-1'));
    expect(useChatStore.getState().routingScope).toBe('cooperative');
    expect(screen.getByRole('button', { name: /Routing scope:/ })).toBeDisabled();
  });
});

describe('Network connection recovery message', () => {
  it('shows user-level recovery during an outage and clears it after reconnection', () => {
    mocks.coordinator = { reachable: false, lastCheckedAt: 1 };
    const { rerender } = render(<CooperativeConnectionPanel />);
    expect(
      screen.getByText(/The network coordinator helps your machines connect/),
    ).toHaveTextContent('Potluck chooses its address automatically.');
    expect(screen.queryByText(/Phase G|sidecar/)).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Check your internet connection');
    expect(screen.getByRole('status')).toHaveTextContent('Potluck retries automatically.');
    expect(screen.getByRole('status')).toHaveTextContent('choosing Local only in Chat');
    expect(screen.getByRole('status')).not.toHaveTextContent('make up');
    mocks.coordinator = { reachable: true, lastCheckedAt: 2 };
    rerender(<CooperativeConnectionPanel />);
    expect(screen.getByRole('status')).toHaveTextContent('Coordinator available');
    expect(screen.queryByText(/Cannot connect to the network/)).not.toBeInTheDocument();
  });
});


describe('signed-out local chat', () => {
  it('clamps a restored remote scope and preserves the remembered circle without fetching it', async () => {
    useAuthStore.getState().clearSession();
    useChatStore.setState({ routingScope: 'circle', activeCircleId: 'remember-me' });
    render(chat());
    expect(screen.getByRole('button', { name: /Routing scope: Local only/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Routing scope: Local only/ }));
    for (const label of [/My machines/, /Trusted circle/, /Cooperative network/]) {
      const choice = screen.getByRole('option', { name: label });
      expect(choice).toBeDisabled();
      expect(choice).toHaveTextContent('Sign in from Settings → Account');
    }
    expect(mocks.circles).not.toHaveBeenCalled();
    expect(useChatStore.getState().activeCircleId).toBe('remember-me');
    expect(useChatStore.getState().routingScope).toBe('circle');
  });

  it('explains an existing split runtime and disables unsigned sending without changing it', () => {
    useAuthStore.getState().clearSession();
    mocks.loadedRpcServers = ['192.0.2.2:50052'];
    render(chat());
    expect(screen.getByRole('alert')).toHaveTextContent('The loaded model uses other machines.');
    expect(screen.getByRole('heading', { name: 'Choose a model for local chat' })).toBeInTheDocument();
    expect(screen.queryByText('No model loaded yet')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open Models' })).toBeInTheDocument();
    fireEvent.change(screen.getByRole('textbox', { name: 'Chat message' }), { target: { value: 'Private draft' } });
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
    expect(mocks.send).not.toHaveBeenCalled();
  });
});


it('keeps local use after account restoration until the member chooses the loaded split model', () => {
  useUiPrefsStore.setState({ localUseEnabled: true });
  useChatStore.setState({ routingScope: 'circle', activeCircleId: 'circle-1' });
  mocks.loadedRpcServers = ['192.0.2.2:50052'];
  render(chat());
  expect(screen.getByRole('alert')).toHaveTextContent('The loaded model uses other machines.');
  fireEvent.change(screen.getByRole('textbox', { name: 'Chat message' }), { target: { value: 'Keep this draft' } });
  expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Use this model' }));
  expect(useUiPrefsStore.getState().localUseEnabled).toBe(false);
  expect(useChatStore.getState().routingScope).toBe('local');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Send message' })).toBeEnabled();
  expect(screen.getByRole('textbox', { name: 'Chat message' })).toHaveValue('Keep this draft');
  expect(mocks.send).not.toHaveBeenCalled();
});

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const mocks = vi.hoisted(() => ({
  coordinator: { reachable: true, lastCheckedAt: 1 as number | null },
  modelLoaded: true,
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
  useHealth: () => ({ modelLoaded: mocks.modelLoaded, nCtxLoaded: 8192 }),
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

import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { useAuthStore } from '@/stores/authStore';
import type { PotluckSettings } from '@/lib/localApi';

const mocks = vi.hoisted(() => ({ update: vi.fn(), rotateKey: vi.fn() }));
vi.mock('@/lib/localApi', () => ({ localApi: { rotateGatewayKey: mocks.rotateKey } }));
vi.mock('@/lib/features', () => ({ FEATURES: { agentWorkroom: false } }));
import { GatewayPanel } from './GatewayPanel';

beforeEach(() => {
  vi.clearAllMocks();
  mocks.update.mockResolvedValue(undefined);
  useAuthStore.setState({ session: null, hasPersistedToken: false, authFailureKind: null });
});

function panel(enabled: boolean, key: string, pool = false) {
  const settings = { gateway: { enabled, api_key: key, pool_routing: pool }, active_model_id: null } as PotluckSettings;
  return render(<MemoryRouter><GatewayPanel settings={settings} isSaving={false} saveError={null} onUpdate={mocks.update} /></MemoryRouter>);
}

it('requires sign-in before enabling a gateway or generating a key', () => {
  panel(false, '');
  expect(screen.getByText(/It can route tool requests to your other machines/)).toBeInTheDocument();
  expect(screen.getByRole('switch', { name: 'Enable the gateway' })).toBeDisabled();
  const generate = screen.getByRole('button', { name: 'Generate API key' });
  expect(generate).toBeDisabled();
  fireEvent.click(generate);
  expect(mocks.rotateKey).not.toHaveBeenCalled();
  expect(mocks.update).not.toHaveBeenCalled();
});

it('keeps a configured service unchanged and permits turning it and pool routing off', () => {
  panel(true, 'existing-test-key', true);
  expect(screen.getByText(/Your existing gateway is still running/)).toBeInTheDocument();
  expect(mocks.update).not.toHaveBeenCalled();
  const toggle = screen.getByRole('switch', { name: 'Enable the gateway' });
  expect(toggle).toBeEnabled();
  fireEvent.click(toggle);
  expect(mocks.update).toHaveBeenCalledWith({ gateway: { enabled: false } });
  fireEvent.click(screen.getByRole('switch', { name: 'Allow open-pool routing' }));
  expect(mocks.update).toHaveBeenCalledWith({ gateway: { pool_routing: false } });
  expect(screen.getByRole('button', { name: 'Regenerate key' })).toBeDisabled();
});

it('does not enable a remembered gateway or pool configuration while signed out', () => {
  panel(false, 'existing-test-key');
  expect(screen.getByRole('switch', { name: 'Enable the gateway' })).toBeDisabled();
  expect(screen.getByRole('switch', { name: 'Allow open-pool routing' })).toBeDisabled();
});

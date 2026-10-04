import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { MachineModels } from './MachineModels';
import { localApi } from '@/lib/localApi';
import type { ModelAvailability } from '@/types/modelAvailability';
import type { CatalogModel } from '@/stores/modelStore';

vi.mock('@/lib/localApi', () => ({ localApi: {
  modelAvailability: vi.fn(), splitStatus: vi.fn(), modelIdentity: vi.fn(),
  splitPlan: vi.fn(), splitStart: vi.fn(), splitStop: vi.fn(),
} }));
const pin = { id: 'coder', format: 'gguf' as const, sha256: 'a'.repeat(64), sizeBytes: 100 };
const inventory = (): ModelAvailability => ({ schemaVersion: 1, scope: 'mine', checkedAt: 1,
  installedModels: [{ id: 'coder', sizeBytes: 100 }],
  models: [{ id: 'coder', sha256: pin.sha256, local: false, idleMachineCount: 1, machines: [{ name: 'Desktop', meshIp: '100.64.0.2', idle: true }] }],
  machines: [{ name: 'Desktop', meshIp: '100.64.0.2', responding: true, splitReady: true, splitCompatible: true, backend: 'cpu', freeMemoryBytes: 10000000 }],
  additionalModels: 1, splitWorkerCount: 1, splitEngineAvailable: true, truncated: false });
const models = [{ slug: 'coder', name: 'Coder', installed: true } as CatalogModel];
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(localApi.modelAvailability).mockResolvedValue(inventory());
  vi.mocked(localApi.splitStatus).mockResolvedValue({ state: 'idle', error: null, session: null });
  vi.mocked(localApi.modelIdentity).mockResolvedValue(pin);
  vi.mocked(localApi.splitPlan).mockResolvedValue({});
  vi.mocked(localApi.splitStart).mockResolvedValue({ state: 'loading', error: null, session: null });
});
function show() { render(<MemoryRouter><MachineModels models={models} /></MemoryRouter>); }
async function select() {
  await screen.findByText('+1');
  fireEvent.click(screen.getByText('Run one model across computers'));
  fireEvent.change(screen.getByLabelText('Installed model'), { target: { value: 'coder' } });
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: 'Preview split' }));
  await screen.findByText('Ready to attempt this split');
}
it('requires a preview and a distinct start action with the exact bytes and workers', async () => {
  show(); await select();
  expect(localApi.splitStart).not.toHaveBeenCalled();
  expect(localApi.splitPlan).toHaveBeenCalledWith({ model: pin, peers: ['100.64.0.2'], context: 2048 });
  fireEvent.click(screen.getByRole('button', { name: 'Start split' }));
  await waitFor(() => expect(localApi.splitStart).toHaveBeenCalledWith({ model: pin, peers: ['100.64.0.2'], context: 2048 }));
});
it('discards a preview when its model/context changes', async () => {
  show(); await select();
  fireEvent.change(screen.getByLabelText('Context'), { target: { value: '1024' } });
  expect(screen.queryByRole('button', { name: 'Start split' })).not.toBeInTheDocument();
});
it('withdraws departed models and workers before another launch', async () => {
  show(); await select();
  vi.mocked(localApi.modelAvailability).mockResolvedValue({ ...inventory(), models: [], machines: [], additionalModels: 0, splitWorkerCount: 0 });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh machine models' }));
  await screen.findByText('1 no longer available.');
  expect(screen.queryByRole('button', { name: 'Start split' })).not.toBeInTheDocument();
  expect(localApi.splitStart).not.toHaveBeenCalled();
});
it('clears ready claims after a failed refresh', async () => {
  show(); await select();
  vi.mocked(localApi.modelAvailability).mockRejectedValue(new Error('offline'));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh machine models' }));
  await screen.findByRole('alert');
  expect(screen.queryByText('+1')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Start split' })).not.toBeInTheDocument();
});

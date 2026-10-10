import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const mocks = vi.hoisted(() => ({
  listModels: vi.fn(),
  health: vi.fn(),
  loadModel: vi.fn(),
  refresh: vi.fn(),
}));
vi.mock('@/lib/localApi', () => ({
  localApi: { listModels: mocks.listModels, health: mocks.health, loadModel: mocks.loadModel },
}));

import { ModelSelector } from './ModelSelector';
import { useAcceptanceStore } from '@/stores/acceptanceStore';
import { parseAcceptance } from '@/lib/acceptance';

const FALLBACK_MESSAGE =
  'The GPU gave wrong answers for this model, so it is running on the CPU, which is slower. Updating the graphics driver usually fixes this.';
const MODELS = [
  { slug: 'qwen3-8b-q4', name: 'Qwen3 8B', parameters: '8B', installed: true },
  { slug: 'small-3b', name: 'Small 3B', parameters: '3B', installed: true },
];
const fellBack = (modelId: string | null = 'qwen3-8b-q4') =>
  parseAcceptance({
    status: 'passed',
    model_id: modelId,
    verdict: 'gpu_wrong_output',
    fallback: { from: 'gpu', to: 'cpu', reason: 'gpu_wrong_output' },
    message: FALLBACK_MESSAGE,
  });
const renderSelector = () =>
  render(
    <MemoryRouter>
      <ModelSelector />
    </MemoryRouter>,
  );

beforeEach(() => {
  mocks.listModels.mockReset().mockResolvedValue(MODELS);
  mocks.health.mockReset().mockResolvedValue({ active_model_id: 'qwen3-8b-q4' });
  mocks.loadModel.mockReset().mockResolvedValue({});
  mocks.refresh.mockReset().mockResolvedValue(undefined);
  useAcceptanceStore.setState({ acceptance: null, dismissedKey: null, refresh: mocks.refresh });
});

describe('ModelSelector answer check label', () => {
  it('says "On CPU" next to the loaded model after the check moved it, with the engine sentence as the hint', async () => {
    useAcceptanceStore.setState({ acceptance: fellBack() });
    renderSelector();
    expect(await screen.findByText('Qwen3 8B')).toBeInTheDocument();
    const label = screen.getByText('On CPU');
    expect(label).toHaveAttribute('title', FALLBACK_MESSAGE);
  });

  it('shows no label when the model is on the GPU, the check passed, or the engine has no check', async () => {
    renderSelector();
    expect(await screen.findByText('Qwen3 8B')).toBeInTheDocument();
    expect(screen.queryByText('On CPU')).not.toBeInTheDocument();
    act(() =>
      useAcceptanceStore.setState({
        acceptance: parseAcceptance({ status: 'passed', model_id: 'qwen3-8b-q4', message: null }),
      }),
    );
    expect(screen.queryByText('On CPU')).not.toBeInTheDocument();
  });

  it('shows no label when the result is about another model than the loaded one', async () => {
    useAcceptanceStore.setState({ acceptance: fellBack('small-3b') });
    renderSelector();
    expect(await screen.findByText('Qwen3 8B')).toBeInTheDocument();
    expect(screen.queryByText('On CPU')).not.toBeInTheDocument();
  });

  it('shows no label while no model is loaded', async () => {
    mocks.health.mockResolvedValue({ active_model_id: null });
    useAcceptanceStore.setState({ acceptance: fellBack(null) });
    renderSelector();
    expect(await screen.findByText('No model loaded')).toBeInTheDocument();
    expect(screen.queryByText('On CPU')).not.toBeInTheDocument();
  });

  it('reads the answer check again after the person loads another model', async () => {
    renderSelector();
    fireEvent.click(await screen.findByRole('button', { name: 'Select model' }));
    fireEvent.click(await screen.findByRole('option', { name: /Small 3B/ }));
    await waitFor(() => expect(mocks.loadModel).toHaveBeenCalledWith('small-3b'));
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalledTimes(1));
  });
});

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  health: { sidecarUp: true, activeModelId: 'qwen3-8b-q4' as string | null },
  refresh: vi.fn(),
}));
vi.mock('@/hooks/useHealth', () => ({ useHealth: () => mocks.health }));

import { AnswerCheckBanner } from './AnswerCheckBanner';
import { useAcceptanceStore } from '@/stores/acceptanceStore';
import { useModelStore, type CatalogModel } from '@/stores/modelStore';
import { parseAcceptance, type Acceptance } from '@/lib/acceptance';

const FALLBACK_MESSAGE =
  'The GPU gave wrong answers for this model, so it is running on the CPU, which is slower. Updating the graphics driver usually fixes this.';
const FAILED_MESSAGE =
  'This model did not pass its answer check on this machine. You can still use it yourself. It is not offered to other people.';

function acceptance(over: Record<string, unknown>): Acceptance {
  const parsed = parseAcceptance({
    status: 'passed',
    model_id: 'qwen3-8b-q4',
    verdict: 'ok',
    fallback: null,
    message: null,
    ...over,
  });
  if (parsed === null) throw new Error('bad fixture');
  return parsed;
}
const fellBack = (over: Record<string, unknown> = {}) =>
  acceptance({
    verdict: 'gpu_wrong_output',
    fallback: { from: 'gpu', to: 'cpu', reason: 'gpu_wrong_output' },
    message: FALLBACK_MESSAGE,
    ...over,
  });
const failed = (over: Record<string, unknown> = {}) =>
  acceptance({ status: 'failed', verdict: 'model_failed', message: FAILED_MESSAGE, ...over });
const show = (a: Acceptance | null) => act(() => useAcceptanceStore.setState({ acceptance: a }));

beforeEach(() => {
  mocks.health = { sidecarUp: true, activeModelId: 'qwen3-8b-q4' };
  mocks.refresh.mockReset().mockResolvedValue(undefined);
  useAcceptanceStore.setState({ acceptance: null, dismissedKey: null, refresh: mocks.refresh });
  useModelStore.setState({ models: [{ slug: 'qwen3-8b-q4', name: 'Qwen3 8B' } as CatalogModel] });
});

describe('AnswerCheckBanner', () => {
  it('shows the engine sentence and names the model on the CPU after a fallback', () => {
    useAcceptanceStore.setState({ acceptance: fellBack() });
    render(<AnswerCheckBanner />);
    expect(screen.getByText(FALLBACK_MESSAGE)).toBeInTheDocument();
    expect(screen.getByText('Qwen3 8B is running on the CPU.')).toBeInTheDocument();
  });

  it('shows only the engine sentence when the model failed the check', () => {
    useAcceptanceStore.setState({ acceptance: failed() });
    render(<AnswerCheckBanner />);
    expect(screen.getByText(FAILED_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByText(/running on the CPU/)).not.toBeInTheDocument();
  });

  it('adds nothing of its own to the strip: the sentence, the CPU line and the dismiss button', () => {
    useAcceptanceStore.setState({ acceptance: fellBack() });
    const { container } = render(<AnswerCheckBanner />);
    expect(container.textContent).toBe(`${FALLBACK_MESSAGE}Qwen3 8B is running on the CPU.`);
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.getAllByRole('button')).toHaveLength(1);
  });

  it('falls back to a readable model name when the catalog does not list it', () => {
    useModelStore.setState({ models: [] });
    useAcceptanceStore.setState({ acceptance: fellBack() });
    render(<AnswerCheckBanner />);
    expect(screen.getByText('Qwen3 8B is running on the CPU.')).toBeInTheDocument();
  });

  it('says "This model" when the engine names none', () => {
    useAcceptanceStore.setState({ acceptance: fellBack({ model_id: null }) });
    render(<AnswerCheckBanner />);
    expect(screen.getByText('This model is running on the CPU.')).toBeInTheDocument();
  });

  it.each([
    ['passed', {}],
    ['running', { status: 'running' }],
    ['skipped', { status: 'skipped' }],
    ['not run', { status: 'not_run' }],
    ['passed with a stored proof', { from_proof: true }],
  ])('shows nothing when the check %s', (_name, over) => {
    useAcceptanceStore.setState({ acceptance: acceptance(over) });
    const { container } = render(<AnswerCheckBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows nothing for an older engine, with no result at all', () => {
    const { container } = render(<AnswerCheckBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows nothing when the engine sent no sentence, rather than inventing one', () => {
    useAcceptanceStore.setState({ acceptance: failed({ message: null }) });
    const { container } = render(<AnswerCheckBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows engine text as plain text, without escape sequences or markup', () => {
    useAcceptanceStore.setState({
      acceptance: failed({ message: '\u001b[31mNo good\u001b[0m <b>at all</b>' }),
    });
    const { container } = render(<AnswerCheckBanner />);
    expect(container.textContent).toContain('No good <b>at all</b>');
    expect(container.querySelector('b')).toBeNull();
    expect(container.textContent).not.toContain('\u001b');
  });

  it('can be dismissed for the session', () => {
    useAcceptanceStore.setState({ acceptance: fellBack() });
    const { container } = render(<AnswerCheckBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(container).toBeEmptyDOMElement();
    // The same state arriving again, as a later read of the engine would deliver it, stays closed.
    show(fellBack());
    expect(container).toBeEmptyDOMElement();
  });

  it('comes back when the model changes', () => {
    useAcceptanceStore.setState({ acceptance: fellBack() });
    render(<AnswerCheckBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByText(FALLBACK_MESSAGE)).not.toBeInTheDocument();
    show(fellBack({ model_id: 'other-model' }));
    expect(screen.getByText(FALLBACK_MESSAGE)).toBeInTheDocument();
  });

  it('comes back when the verdict changes', () => {
    useAcceptanceStore.setState({ acceptance: fellBack() });
    render(<AnswerCheckBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    show(failed());
    expect(screen.getByText(FAILED_MESSAGE)).toBeInTheDocument();
  });

  it('reads the check when the engine is up, and again when the loaded model changes', () => {
    const { rerender } = render(<AnswerCheckBanner />);
    expect(mocks.refresh).toHaveBeenCalledTimes(1);
    mocks.health = { sidecarUp: true, activeModelId: 'another-model' };
    rerender(<AnswerCheckBanner />);
    expect(mocks.refresh).toHaveBeenCalledTimes(2);
  });

  it('does not read the check while the engine is not up', () => {
    mocks.health = { sidecarUp: false, activeModelId: null };
    render(<AnswerCheckBanner />);
    expect(mocks.refresh).not.toHaveBeenCalled();
  });

  it('never uses an em dash in anything it adds', () => {
    useAcceptanceStore.setState({ acceptance: fellBack() });
    const { container } = render(<AnswerCheckBanner />);
    expect(container.textContent).not.toContain('—');
  });
});

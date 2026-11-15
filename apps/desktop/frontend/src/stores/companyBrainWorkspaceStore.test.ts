import { beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('@/lib/sidecarPersist', () => ({ persistUiPref: vi.fn().mockResolvedValue(undefined) }));
import { persistUiPref } from '@/lib/sidecarPersist';
import {
  COMPANY_BRAIN_WORKSPACE_KEY,
  parseCompanyBrainWorkspace,
  useCompanyBrainWorkspaceStore,
  validateCompanyBrainWorkspace,
} from './companyBrainWorkspaceStore';

const snapshot = {
  version: 1 as const,
  workspace: 'Acme',
  collectionIds: ['a', 'b'],
  modelSlug: 'model',
};
beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  useCompanyBrainWorkspaceStore.setState({ saved: null, revision: 0 });
});

describe('Company Brain workspace preferences', () => {
  it('rejects malformed, unknown version and out-of-bounds data', () => {
    expect(parseCompanyBrainWorkspace('{')).toBeNull();
    expect(parseCompanyBrainWorkspace(JSON.stringify({ ...snapshot, version: 2 }))).toBeNull();
    expect(validateCompanyBrainWorkspace({ ...snapshot, workspace: ' ' })).toBeNull();
    expect(validateCompanyBrainWorkspace({ ...snapshot, workspace: 'x'.repeat(257) })).toBeNull();
    expect(validateCompanyBrainWorkspace({ ...snapshot, modelSlug: 'x'.repeat(129) })).toBeNull();
    expect(
      validateCompanyBrainWorkspace({ ...snapshot, collectionIds: Array(101).fill('a') }),
    ).toBeNull();
    expect(validateCompanyBrainWorkspace({ ...snapshot, collectionIds: [''] })).toBeNull();
  });
  it('allows a new workspace with no model or collections selected', () => {
    expect(
      validateCompanyBrainWorkspace({ ...snapshot, modelSlug: '', collectionIds: [] }),
    ).toEqual({ ...snapshot, modelSlug: '', collectionIds: [] });
  });
  it('normalizes duplicates and strips approvals and content', () => {
    expect(
      validateCompanyBrainWorkspace({
        ...snapshot,
        workspace: ' Acme ',
        collectionIds: ['a', ' a ', 'b'],
        approved: true,
        question: 'secret',
        pilotConfirmed: true,
      }),
    ).toEqual(snapshot);
  });
  it('writes cache and durable settings on save, but never on hydration', async () => {
    useCompanyBrainWorkspaceStore.getState().saveWorkspace(snapshot);
    expect(localStorage.getItem(COMPANY_BRAIN_WORKSPACE_KEY)).toBe(JSON.stringify(snapshot));
    await vi.waitFor(() =>
      expect(persistUiPref).toHaveBeenCalledWith(
        COMPANY_BRAIN_WORKSPACE_KEY,
        JSON.stringify(snapshot),
      ),
    );
    vi.mocked(persistUiPref).mockClear();
    const remote = { ...snapshot, workspace: 'Remote' };
    useCompanyBrainWorkspaceStore.getState().hydrateFromSidecar(JSON.stringify(remote), 1);
    expect(useCompanyBrainWorkspaceStore.getState().saved).toEqual(remote);
    expect(localStorage.getItem(COMPANY_BRAIN_WORKSPACE_KEY)).toBe(JSON.stringify(remote));
    expect(persistUiPref).not.toHaveBeenCalled();
  });
  it('fences late hydration after a local edit', async () => {
    const revision = useCompanyBrainWorkspaceStore.getState().revision;
    useCompanyBrainWorkspaceStore.getState().saveWorkspace(snapshot);
    useCompanyBrainWorkspaceStore
      .getState()
      .hydrateFromSidecar(JSON.stringify({ ...snapshot, workspace: 'Old' }), revision);
    expect(useCompanyBrainWorkspaceStore.getState().saved).toEqual(snapshot);
    expect(localStorage.getItem(COMPANY_BRAIN_WORKSPACE_KEY)).toBe(JSON.stringify(snapshot));
    await vi.waitFor(() => expect(persistUiPref).toHaveBeenCalledOnce());
  });
  it('continues when cache writes are inaccessible', async () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    try {
      useCompanyBrainWorkspaceStore.getState().saveWorkspace(snapshot);
      expect(useCompanyBrainWorkspaceStore.getState().saved).toEqual(snapshot);
      await vi.waitFor(() => expect(persistUiPref).toHaveBeenCalledOnce());
    } finally {
      spy.mockRestore();
    }
  });
  it('initializes safely when cache reads are inaccessible', async () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    try {
      vi.resetModules();
      const fresh = await import('./companyBrainWorkspaceStore');
      expect(fresh.useCompanyBrainWorkspaceStore.getState().saved).toBeNull();
    } finally {
      spy.mockRestore();
    }
  });
  it('serializes durable writes in save order', async () => {
    let resolveFirst!: () => void;
    vi.mocked(persistUiPref).mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          resolveFirst = resolve;
        }),
    );
    useCompanyBrainWorkspaceStore.getState().saveWorkspace(snapshot);
    useCompanyBrainWorkspaceStore.getState().saveWorkspace({ ...snapshot, workspace: 'Second' });
    await vi.waitFor(() => expect(persistUiPref).toHaveBeenCalledTimes(1));
    resolveFirst();
    await vi.waitFor(() => expect(persistUiPref).toHaveBeenCalledTimes(2));
    expect(persistUiPref).toHaveBeenLastCalledWith(
      COMPANY_BRAIN_WORKSPACE_KEY,
      JSON.stringify({ ...snapshot, workspace: 'Second' }),
    );
  });
});

it('does not trigger a second restore for identical durable preferences', () => {
  useCompanyBrainWorkspaceStore.setState({ saved: snapshot, revision: 0 });
  const listener = vi.fn();
  const unsubscribe = useCompanyBrainWorkspaceStore.subscribe(listener);
  useCompanyBrainWorkspaceStore.getState().hydrateFromSidecar(JSON.stringify(snapshot), 0);
  expect(listener).not.toHaveBeenCalled();
  unsubscribe();
});

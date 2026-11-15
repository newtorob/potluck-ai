import { create } from 'zustand';
import { persistUiPref } from '@/lib/sidecarPersist';

export const COMPANY_BRAIN_WORKSPACE_KEY = 'potluck.company_brain_workspace';
export interface CompanyBrainWorkspaceSnapshot {
  version: 1;
  workspace: string;
  collectionIds: string[];
  modelSlug: string;
}

export function validateCompanyBrainWorkspace(
  value: unknown,
): CompanyBrainWorkspaceSnapshot | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const data = value as Record<string, unknown>;
  if (
    data.version !== 1 ||
    typeof data.workspace !== 'string' ||
    typeof data.modelSlug !== 'string' ||
    !Array.isArray(data.collectionIds)
  )
    return null;
  const workspace = data.workspace.trim();
  const modelSlug = data.modelSlug.trim();
  if (
    !workspace ||
    workspace.length > 256 ||
    modelSlug.length > 128 ||
    data.collectionIds.length > 100
  )
    return null;
  const collectionIds: string[] = [];
  for (const id of data.collectionIds) {
    if (typeof id !== 'string') return null;
    const trimmed = id.trim();
    if (!trimmed || trimmed.length > 256) return null;
    if (!collectionIds.includes(trimmed)) collectionIds.push(trimmed);
  }
  return { version: 1, workspace, collectionIds, modelSlug };
}

export function parseCompanyBrainWorkspace(
  serialized: string | null | undefined,
): CompanyBrainWorkspaceSnapshot | null {
  if (!serialized) return null;
  try {
    return validateCompanyBrainWorkspace(JSON.parse(serialized) as unknown);
  } catch {
    return null;
  }
}

function readCache(): CompanyBrainWorkspaceSnapshot | null {
  try {
    return parseCompanyBrainWorkspace(window.localStorage.getItem(COMPANY_BRAIN_WORKSPACE_KEY));
  } catch {
    return null;
  }
}
function writeCache(serialized: string): void {
  try {
    window.localStorage.setItem(COMPANY_BRAIN_WORKSPACE_KEY, serialized);
  } catch {
    /* Cache may be unavailable. */
  }
}

interface CompanyBrainWorkspaceState {
  saved: CompanyBrainWorkspaceSnapshot | null;
  revision: number;
  saveWorkspace: (snapshot: CompanyBrainWorkspaceSnapshot) => void;
  hydrateFromSidecar: (serialized: string | undefined, expectedRevision: number) => void;
}

let pendingWrite: Promise<void> = Promise.resolve();
export const useCompanyBrainWorkspaceStore = create<CompanyBrainWorkspaceState>()((set, get) => ({
  saved: readCache(),
  revision: 0,
  saveWorkspace(snapshot) {
    const safe = validateCompanyBrainWorkspace(snapshot);
    if (!safe) return;
    const serialized = JSON.stringify(safe);
    writeCache(serialized);
    set((state) => ({ saved: safe, revision: state.revision + 1 }));
    pendingWrite = pendingWrite
      .catch(() => {})
      .then(() => persistUiPref(COMPANY_BRAIN_WORKSPACE_KEY, serialized));
  },
  hydrateFromSidecar(serialized, expectedRevision) {
    if (get().revision !== expectedRevision) return;
    const safe = parseCompanyBrainWorkspace(serialized);
    if (!safe) return;
    if (JSON.stringify(get().saved) === JSON.stringify(safe)) return;
    writeCache(JSON.stringify(safe));
    set({ saved: safe });
  },
}));

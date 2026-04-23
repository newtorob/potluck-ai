/**
 * Memory store — Zustand store for the persistent memory system.
 *
 * Wraps the Python sidecar memory API via localApi.memory.*.
 * Falls back to a thin inline fetcher if the memory namespace
 * hasn't been added to localApi yet (Agent B coordination boundary).
 *
 * Optimistic updates with rollback:
 *   updateMemory / deleteMemory mutate the local array immediately,
 *   then call the API. On failure, the previous array is restored and
 *   the error surfaces in the component.
 *
 * See implementation guide §9.5, §9.9.
 */

import { create } from 'zustand';
import type {
  Memory,
  MemoryCategory,
  MemoryCreateInput,
  MemoryUpdateInput,
  MemoryImportResult,
} from '@/types/memory';
import { localApi } from '@/lib/localApi';

// ---------------------------------------------------------------------------
// Fallback fetcher — used when localApi.memory isn't wired yet.
// Agent B will add localApi.memory; once that lands, this is the dead code
// path. We keep it so the build compiles regardless of merge order.
// ---------------------------------------------------------------------------

const LOCAL_BASE = (import.meta.env['VITE_LOCAL_API_URL'] as string | undefined) ?? 'http://127.0.0.1:8321';

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${LOCAL_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const body: unknown = await res.json().catch(() => ({}));
    const msg =
      typeof body === 'object' && body !== null && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : res.statusText;
    throw new Error(`[memoryStore] ${res.status}: ${msg}`);
  }
  // 204 No Content
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// API builder — all store actions call buildApi() directly.
// When Agent B lands localApi.memory, the store can be updated to delegate
// to that namespace. For now, the inline fetcher is the single implementation.
// ---------------------------------------------------------------------------

function buildApi() {
  return {
    list(params: { category?: MemoryCategory; limit?: number; offset?: number }): Promise<Memory[]> {
      const qs = new URLSearchParams();
      if (params.category) qs.set('category', params.category);
      if (params.limit !== undefined) qs.set('limit', String(params.limit));
      if (params.offset !== undefined) qs.set('offset', String(params.offset));
      const query = qs.toString() ? `?${qs.toString()}` : '';
      return apiFetch<Memory[]>(`/memory${query}`);
    },

    create(body: MemoryCreateInput): Promise<Memory> {
      return apiFetch<Memory>('/memory', {
        method: 'POST',
        body: JSON.stringify(body),
      });
    },

    update(id: string, patch: MemoryUpdateInput): Promise<Memory> {
      return apiFetch<Memory>(`/memory/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: JSON.stringify(patch),
      });
    },

    remove(id: string): Promise<void> {
      return apiFetch<void>(`/memory/${encodeURIComponent(id)}`, { method: 'DELETE' });
    },

    search(body: {
      query?: string;
      current_messages?: Array<{ role: string; content: string }>;
      max_memories?: number;
      min_confidence?: number;
    }): Promise<Memory[]> {
      return apiFetch<Memory[]>('/memory/search', {
        method: 'POST',
        body: JSON.stringify(body),
      });
    },

    wipe(): Promise<{ status: 'wiped' }> {
      return apiFetch<{ status: 'wiped' }>('/memory/wipe', {
        method: 'POST',
        body: JSON.stringify({ confirm: true }),
      });
    },

    export(): Promise<{ memories: Memory[] }> {
      return apiFetch<{ memories: Memory[] }>('/memory/export');
    },

    importMemories(body: { memories: Memory[] }): Promise<MemoryImportResult> {
      return apiFetch<MemoryImportResult>('/memory/import', {
        method: 'POST',
        body: JSON.stringify(body),
      });
    },
  };
}

// ---------------------------------------------------------------------------
// Store state
// ---------------------------------------------------------------------------

export interface MemoryState {
  memories: Memory[];
  isFetching: boolean;
  fetchError: string | null;
  wipeError: string | null;
  lastFetchedAt: number | null;

  // Actions
  fetchMemories: (opts?: {
    category?: MemoryCategory;
    limit?: number;
    offset?: number;
    project?: string | null;
    include_all?: boolean;
  }) => Promise<void>;
  createMemory: (input: MemoryCreateInput) => Promise<Memory>;
  updateMemory: (id: string, patch: MemoryUpdateInput) => Promise<void>;
  deleteMemory: (id: string) => Promise<void>;
  boostConfidence: (id: string, delta?: number) => Promise<void>;
  wipeAll: () => Promise<void>;
  exportToFile: () => Promise<void>;
  importFromFile: (file: File) => Promise<MemoryImportResult>;
  searchMemories: (query: string) => Promise<Memory[]>;
}

// ---------------------------------------------------------------------------
// Store implementation
// ---------------------------------------------------------------------------

export const useMemoryStore = create<MemoryState>()((set, get) => ({
  memories: [],
  isFetching: false,
  fetchError: null,
  wipeError: null,
  lastFetchedAt: null,

  async fetchMemories(opts = {}) {
    set({ isFetching: true, fetchError: null });
    try {
      // Prefer localApi over the inline fallback fetcher when possible
      const memories = await localApi.memory.list({
        category: opts.category,
        limit: opts.limit ?? 100,
        offset: opts.offset ?? 0,
        project: opts.project,
        include_all: opts.include_all,
      });
      set({ memories, isFetching: false, lastFetchedAt: Date.now() });
    } catch {
      // Fall back to the inline fetcher (build-time isolation for Agent B merge order)
      try {
        const api = buildApi();
        const memories = await api.list({
          category: opts.category,
          limit: opts.limit ?? 100,
          offset: opts.offset ?? 0,
        });
        set({ memories, isFetching: false, lastFetchedAt: Date.now() });
      } catch (err) {
        set({
          isFetching: false,
          fetchError: err instanceof Error ? err.message : 'Failed to fetch memories',
        });
      }
    }
  },

  async createMemory(input) {
    const api = buildApi();
    const created = await api.create(input);
    set((state) => ({ memories: [created, ...state.memories] }));
    return created;
  },

  async updateMemory(id, patch) {
    // Optimistic update
    const previous = get().memories;
    set((state) => ({
      memories: state.memories.map((m) =>
        m.id === id ? { ...m, ...patch } : m,
      ),
    }));
    try {
      const api = buildApi();
      const updated = await api.update(id, patch);
      set((state) => ({
        memories: state.memories.map((m) => (m.id === id ? updated : m)),
      }));
    } catch (err) {
      // Rollback
      set({ memories: previous });
      throw err;
    }
  },

  async deleteMemory(id) {
    // Optimistic removal
    const previous = get().memories;
    set((state) => ({
      memories: state.memories.filter((m) => m.id !== id),
    }));
    try {
      const api = buildApi();
      await api.remove(id);
    } catch (err) {
      // Rollback
      set({ memories: previous });
      throw err;
    }
  },

  async boostConfidence(id, delta = 0.1) {
    const memory = get().memories.find((m) => m.id === id);
    if (!memory) return;
    const newConfidence = Math.min(1, Math.max(0, memory.confidence + delta));
    await get().updateMemory(id, { confidence: newConfidence });
  },

  async wipeAll() {
    set({ wipeError: null });
    try {
      const api = buildApi();
      await api.wipe();
      await get().fetchMemories();
    } catch (err) {
      set({ wipeError: err instanceof Error ? err.message : 'Failed to wipe memories' });
      throw err;
    }
  },

  async exportToFile() {
    const api = buildApi();
    const data = await api.export();
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const filename = `potluck-memories-${timestamp}.json`;
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  },

  async importFromFile(file) {
    const text = await file.text();
    const parsed: unknown = JSON.parse(text);

    // Accept either { memories: [...] } or a raw array
    let memoriesArray: unknown[];
    if (
      typeof parsed === 'object' &&
      parsed !== null &&
      'memories' in parsed &&
      Array.isArray((parsed as { memories: unknown }).memories)
    ) {
      memoriesArray = (parsed as { memories: unknown[] }).memories;
    } else if (Array.isArray(parsed)) {
      memoriesArray = parsed;
    } else {
      throw new Error('Invalid import file format');
    }

    const api = buildApi();
    // We trust the file came from our own export — cast is safe here
    const result = await api.importMemories({ memories: memoriesArray as Memory[] });
    await get().fetchMemories();
    return result;
  },

  async searchMemories(query) {
    const api = buildApi();
    return api.search({ query, max_memories: 50 });
  },
}));

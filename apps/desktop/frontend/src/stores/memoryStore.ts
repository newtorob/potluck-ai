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
// Search mode type
// ---------------------------------------------------------------------------

export type SearchMode = 'semantic' | 'exact';


// ---------------------------------------------------------------------------
// Store state
// ---------------------------------------------------------------------------

export interface MemoryState {
  memories: Memory[];
  isFetching: boolean;
  fetchError: string | null;
  wipeError: string | null;
  lastFetchedAt: number | null;

  // Search state
  searchMode: SearchMode;
  searchResults: Memory[] | null;
  isSearching: boolean;

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
  pinMemory: (id: string, pinned: boolean) => Promise<void>;
  boostConfidence: (id: string, delta?: number) => Promise<void>;
  wipeAll: () => Promise<void>;
  exportToFile: () => Promise<void>;
  importFromFile: (file: File) => Promise<MemoryImportResult>;
  searchMemories: (query: string) => Promise<Memory[]>;

  // New search actions
  setSearchMode: (mode: SearchMode) => void;
  performSearch: (query: string, opts?: { project?: string | null; min_confidence?: number }) => Promise<void>;
  clearSearch: () => void;
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

  // Search state
  searchMode: 'semantic',
  searchResults: null,
  isSearching: false,

  async fetchMemories(opts = {}) {
    set({ isFetching: true, fetchError: null });
    try {
      const memories = await localApi.memory.list({
        ...(opts.category !== undefined ? { category: opts.category } : {}),
        limit: opts.limit ?? 100,
        offset: opts.offset ?? 0,
        ...(opts.project !== undefined ? { project: opts.project } : {}),
        ...(opts.include_all !== undefined ? { include_all: opts.include_all } : {}),
      });
      set({ memories, isFetching: false, lastFetchedAt: Date.now() });
    } catch (err) {
      set({
        isFetching: false,
        fetchError: err instanceof Error ? err.message : 'Failed to fetch memories',
      });
    }
  },

  async createMemory(input) {
    const created = await localApi.memory.create(input);
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
      const updated = await localApi.memory.update(id, patch);
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
      await localApi.memory.delete(id);
    } catch (err) {
      // Rollback
      set({ memories: previous });
      throw err;
    }
  },

  async pinMemory(id, pinned) {
    // Optimistic toggle of the pinned flag
    const previous = get().memories;
    set((state) => ({
      memories: state.memories.map((m) => (m.id === id ? { ...m, pinned } : m)),
    }));
    try {
      await localApi.memory.pin(id, pinned);
    } catch (err) {
      set({ memories: previous }); // rollback
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
      await localApi.memory.wipe();
      await get().fetchMemories();
    } catch (err) {
      set({ wipeError: err instanceof Error ? err.message : 'Failed to wipe memories' });
      throw err;
    }
  },

  async exportToFile() {
    const data = await localApi.memory.export();
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

    // We trust the file came from our own export — cast is safe here
    const result = await localApi.memory.import({ memories: memoriesArray as Memory[] });
    await get().fetchMemories();
    return result;
  },

  async searchMemories(query) {
    return localApi.memory.search({ query, max_memories: 50 });
  },

  setSearchMode(mode) {
    set({ searchMode: mode });
  },

  async performSearch(query, opts = {}) {
    if (!query.trim()) {
      set({ searchResults: null, isSearching: false });
      return;
    }
    set({ isSearching: true });
    try {
      const results = await localApi.memory.search({
        query,
        semantic: get().searchMode === 'semantic',
        max_memories: 100,
        ...(opts.project !== undefined ? { project: opts.project } : {}),
        ...(opts.min_confidence !== undefined ? { min_confidence: opts.min_confidence } : {}),
      });
      set({ searchResults: results, isSearching: false });
    } catch {
      set({ searchResults: [], isSearching: false });
    }
  },

  clearSearch() {
    set({ searchResults: null, isSearching: false });
  },
}));

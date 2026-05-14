/**
 * Mesh store — live state for the /household view.
 *
 * Polling cadence (matches the precedent set by /agents — memory a8909d48):
 *   - status: every 2s
 *   - peers:  every 5s
 *   - both pause when document.hidden
 *
 * Why polling instead of the WS /events stream the sidecar exposes:
 * single-user local-only scope doesn't need push, and a long-lived WS
 * makes Electron packaging + lifecycle harder. If a future feature needs
 * sub-second mesh-state UI (live punch-coord rendering, etc.) we'll add a
 * separate WS subscription path through the bridge.
 *
 * Lifecycle:
 *   - `start()` is idempotent — call from a top-level provider or the
 *     HouseholdView itself on mount.
 *   - `stop()` clears the timers but keeps the last fetched values so
 *     other components (e.g. the StatusBar mesh pill in task 799939d7)
 *     can still read them.
 */

import { create } from 'zustand';
import {
  meshClient,
  isMeshBridgeAvailable,
  MeshBridgeUnavailableError,
  MeshSidecarUnreachableError,
  MeshApiError,
} from '@/lib/meshClient';
import type { MeshStatus, MeshPeer } from '@/types/mesh';

const STATUS_POLL_MS = 2_000;
const PEERS_POLL_MS = 5_000;

// `reachability` lives on the connection between the renderer and the
// sidecar — distinct from the WG-level peer reachability already on each
// MeshPeer row.
export type BridgeReachability =
  | 'unknown'
  | 'bridge_missing' // running in a plain browser, not Electron
  | 'sidecar_offline' // bridge present, sidecar socket not accepting
  | 'reachable';

interface MeshState {
  status: MeshStatus | null;
  peers: MeshPeer[];
  bridge: BridgeReachability;
  lastError: string | null;
  isStarted: boolean;

  // Internals
  _statusTimer: ReturnType<typeof setTimeout> | null;
  _peersTimer: ReturnType<typeof setTimeout> | null;
  _visibilityHandler: (() => void) | null;

  start: () => void;
  stop: () => void;
  refresh: () => Promise<void>;
}

export const useMeshStore = create<MeshState>()((set, get) => ({
  status: null,
  peers: [],
  bridge: 'unknown',
  lastError: null,
  isStarted: false,

  _statusTimer: null,
  _peersTimer: null,
  _visibilityHandler: null,

  async refresh() {
    if (!isMeshBridgeAvailable()) {
      set({ bridge: 'bridge_missing', lastError: null });
      return;
    }
    try {
      const [status, peersRes] = await Promise.all([
        meshClient.status(),
        meshClient.peers(),
      ]);
      set({
        status,
        peers: peersRes.peers,
        bridge: 'reachable',
        lastError: null,
      });
    } catch (err) {
      handleError(err, set);
    }
  },

  start() {
    if (get().isStarted) return;

    if (!isMeshBridgeAvailable()) {
      set({ isStarted: true, bridge: 'bridge_missing' });
      return;
    }

    set({ isStarted: true });

    // Initial fetches — pull both in parallel.
    void get().refresh();

    const scheduleStatus = () => {
      const timer = setTimeout(async () => {
        if (typeof document === 'undefined' || !document.hidden) {
          try {
            const status = await meshClient.status();
            set({ status, bridge: 'reachable', lastError: null });
          } catch (err) {
            handleError(err, set);
          }
        }
        scheduleStatus();
      }, STATUS_POLL_MS);
      set({ _statusTimer: timer });
    };

    const schedulePeers = () => {
      const timer = setTimeout(async () => {
        if (typeof document === 'undefined' || !document.hidden) {
          try {
            const peersRes = await meshClient.peers();
            set({ peers: peersRes.peers, bridge: 'reachable' });
          } catch (err) {
            handleError(err, set);
          }
        }
        schedulePeers();
      }, PEERS_POLL_MS);
      set({ _peersTimer: timer });
    };

    scheduleStatus();
    schedulePeers();

    const onVisibility = () => {
      if (!document.hidden) void get().refresh();
    };
    document.addEventListener('visibilitychange', onVisibility);
    set({ _visibilityHandler: onVisibility });
  },

  stop() {
    const s = get();
    if (s._statusTimer !== null) clearTimeout(s._statusTimer);
    if (s._peersTimer !== null) clearTimeout(s._peersTimer);
    if (s._visibilityHandler !== null && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', s._visibilityHandler);
    }
    set({
      _statusTimer: null,
      _peersTimer: null,
      _visibilityHandler: null,
      isStarted: false,
    });
  },
}));

function handleError(err: unknown, set: (partial: Partial<MeshState>) => void): void {
  if (err instanceof MeshBridgeUnavailableError) {
    set({ bridge: 'bridge_missing', lastError: null });
    return;
  }
  if (err instanceof MeshSidecarUnreachableError) {
    set({ bridge: 'sidecar_offline', lastError: null });
    return;
  }
  if (err instanceof MeshApiError) {
    set({ lastError: err.message });
    return;
  }
  set({ lastError: err instanceof Error ? err.message : String(err) });
}

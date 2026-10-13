/**
 * Mesh store - live state for the /household view.
 *
 * Polling cadence (same approach as the /agents view):
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
 *   - `start()` is idempotent - call from a top-level provider or the
 *     HouseholdView itself on mount.
 *   - `stop()` clears the timers but keeps the last fetched values so
 *     other components (e.g. the StatusBar mesh pill)
 *     can still read them.
 */

import { create } from 'zustand';
import {
  meshClient,
  meshDaemon,
  isMeshBridgeAvailable,
  isMeshDaemonBridgeAvailable,
  MeshBridgeUnavailableError,
  MeshSidecarUnreachableError,
  MeshApiError,
} from '@/lib/meshClient';
import { useConnectionStore } from '@/stores/connectionStore';
import type { MeshStatus, MeshPeer, MeshDaemonStatus, MeshDaemonState } from '@/types/mesh';

const STATUS_POLL_MS = 2_000;
const PEERS_POLL_MS = 5_000;
/** While the socket answers, how often to ask the main process whether the
 *  running service is the sidecar this app shipped. Not every 2s: on Linux and
 *  Windows each answer costs a systemctl or PowerShell call. */
const SERVICE_VERSION_PROBE_MS = 30_000;

// `reachability` lives on the connection between the renderer and the
// sidecar - distinct from the WG-level peer reachability already on each
// MeshPeer row.
//
// The daemon_* values fold in the "app brings up the mesh" supervision layer
// (main.js's mesh:daemon-status bridge, macOS SMAppService today): they only
// ever get set as a refinement of sidecar_offline (see
// refineBridgeOnUnreachable below) - once the socket actually answers, the
// bridge always goes straight to 'reachable' regardless of daemon state.
export type BridgeReachability =
  | 'unknown'
  | 'bridge_missing' // running in a plain browser, not Electron
  | 'daemon_not_installed' // mesh daemon never registered (or lost track of)
  | 'daemon_requires_approval' // registered, needs a click in System Settings
  | 'daemon_starting' // registered + approved, socket not answering yet
  | 'daemon_failed' // installed but the service crashed / crash-looped
  | 'daemon_unsupported' // this platform's daemon supervision isn't built yet
  | 'sidecar_offline' // bridge present, socket not accepting, no daemon info
  | 'reachable';

interface MeshState {
  status: MeshStatus | null;
  peers: MeshPeer[];
  bridge: BridgeReachability;
  /** Raw daemon-supervision status behind the daemon_* bridge values above -
   *  carries `detail` for surfacing in the UI. Cleared once bridge is
   *  anything other than a daemon_* value. */
  daemonStatus: MeshDaemonStatus | null;
  /** Set while the sidecar answers but is the one from before the last app
   *  update (daemon state 'outdated'). Separate from `bridge`, which stays
   *  'reachable': the mesh works, the UI only offers a restart. */
  serviceOutdated: MeshDaemonStatus | null;
  lastError: string | null;
  isStarted: boolean;

  // Internals
  _serviceProbeAt: number;
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
  daemonStatus: null,
  serviceOutdated: null,
  lastError: null,
  isStarted: false,

  _serviceProbeAt: 0,
  _statusTimer: null,
  _peersTimer: null,
  _visibilityHandler: null,

  async refresh() {
    if (!isMeshBridgeAvailable()) {
      set({ bridge: 'bridge_missing', lastError: null, daemonStatus: null });
      return;
    }
    try {
      const [status, peersRes] = await Promise.all([
        meshClient.status(),
        meshClient.peers(),
      ]);
      useConnectionStore.getState().adoptSidecarUrl(status.coordinator_url);
      set({
        status,
        peers: peersRes.peers,
        bridge: 'reachable',
        daemonStatus: null,
        lastError: null,
      });
      // Called by the panels' buttons after they act, so look at once.
      void probeServiceVersion(get, set, true);
    } catch (err) {
      await handleStatusError(err, set);
    }
  },

  start() {
    if (get().isStarted) return;

    if (!isMeshBridgeAvailable()) {
      set({ isStarted: true, bridge: 'bridge_missing' });
      return;
    }

    set({ isStarted: true });

    // Initial fetches - pull both in parallel.
    void get().refresh();

    const scheduleStatus = () => {
      const timer = setTimeout(() => {
        void (async () => {
          if (typeof document === 'undefined' || !document.hidden) {
            try {
              const status = await meshClient.status();
              useConnectionStore.getState().adoptSidecarUrl(status.coordinator_url);
              set({ status, bridge: 'reachable', daemonStatus: null, lastError: null });
              void probeServiceVersion(get, set);
            } catch (err) {
              // Awaited (not fire-and-forget) so the daemon-status probe this
              // triggers finishes - and any set() it does lands - before the
              // reschedule below arms the next tick. Keeps daemon polling on
              // exactly this same 2s cadence rather than a second timer.
              await handleStatusError(err, set);
            }
          }
          scheduleStatus();
        })();
      }, STATUS_POLL_MS);
      set({ _statusTimer: timer });
    };

    const schedulePeers = () => {
      const timer = setTimeout(() => {
        void (async () => {
          if (typeof document === 'undefined' || !document.hidden) {
            try {
              const peersRes = await meshClient.peers();
              set({ peers: peersRes.peers, bridge: 'reachable', daemonStatus: null });
            } catch (err) {
              handlePeersError(err, set);
            }
          }
          schedulePeers();
        })();
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

/** Maps a daemon-supervision state to the bridge value HouseholdView reads.
 *  'running' is deliberately mapped to the generic offline fallback: we only
 *  ever get here because the socket probe that triggered this check just
 *  failed, so "daemon says running" is a stale/racy read (the daemon likely
 *  just came up) rather than something to show the user as fact - the next
 *  status poll (2s later) will pick up 'reachable' once it's really up. */
function daemonStateToBridge(state: MeshDaemonState): BridgeReachability {
  switch (state) {
    case 'not_installed':
      return 'daemon_not_installed';
    case 'requires_approval':
      return 'daemon_requires_approval';
    case 'starting':
      return 'daemon_starting';
    case 'failed':
      return 'daemon_failed';
    case 'unsupported':
      return 'daemon_unsupported';
    case 'running':
    case 'outdated':
      return 'sidecar_offline';
  }
}

/** Best-effort daemon-status probe. Never throws - a missing bridge or a
 *  failed IPC call just means "no daemon info available," not a hard error. */
async function probeDaemonStatus(): Promise<MeshDaemonStatus | null> {
  if (!isMeshDaemonBridgeAvailable()) return null;
  try {
    return await meshDaemon.status();
  } catch {
    return null;
  }
}

/**
 * While the socket answers, asks the main process whether the service that
 * answers is the sidecar this app shipped (daemon state 'outdated'). The first
 * call after start() and every refresh() go through; the 2s status poll is
 * limited to one per SERVICE_VERSION_PROBE_MS. A failed probe leaves the last
 * answer alone.
 */
async function probeServiceVersion(
  get: () => MeshState,
  set: (partial: Partial<MeshState>) => void,
  force = false,
): Promise<void> {
  const now = Date.now();
  if (!force && now - get()._serviceProbeAt < SERVICE_VERSION_PROBE_MS) return;
  set({ _serviceProbeAt: now });
  const daemon = await probeDaemonStatus();
  if (daemon === null) return;
  set({ serviceOutdated: daemon.state === 'outdated' ? daemon : null });
}

/**
 * Refines the generic "socket unreachable" outcome into a specific
 * daemon_* bridge value when daemon-supervision info is available (macOS
 * today), falling back to the old generic 'sidecar_offline' otherwise (dev
 * mode without the bridge, non-macOS, or the probe itself failing).
 *
 * This is the only place that queries the daemon bridge - it rides on the
 * status poll's existing 2s cadence (see scheduleStatus's catch block and
 * refresh() above) rather than running its own timer.
 */
async function refineBridgeOnUnreachable(
  set: (partial: Partial<MeshState>) => void,
): Promise<void> {
  const daemon = await probeDaemonStatus();
  if (daemon) {
    set({
      bridge: daemonStateToBridge(daemon.state),
      daemonStatus: daemon,
      serviceOutdated: null,
      lastError: null,
    });
  } else {
    set({ bridge: 'sidecar_offline', daemonStatus: null, serviceOutdated: null, lastError: null });
  }
}

/** Error handler for the status-poll cadence (refresh() + scheduleStatus) -
 *  the one place `bridge` gets decided when the socket isn't reachable. */
async function handleStatusError(
  err: unknown,
  set: (partial: Partial<MeshState>) => void,
): Promise<void> {
  if (err instanceof MeshBridgeUnavailableError) {
    set({ bridge: 'bridge_missing', daemonStatus: null, lastError: null });
    return;
  }
  if (err instanceof MeshSidecarUnreachableError) {
    await refineBridgeOnUnreachable(set);
    return;
  }
  if (err instanceof MeshApiError) {
    set({ lastError: err.message });
    return;
  }
  set({ lastError: err instanceof Error ? err.message : String(err) });
}

/**
 * Error handler for the peers-poll cadence (5s, schedulePeers). Deliberately
 * does not touch `bridge` on a sidecar-unreachable error: the status poll
 * (2s, more frequent, and the one that runs the daemon-status refinement
 * above) already owns `bridge` as the single source of truth. Letting this
 * slower poll also write a generic 'sidecar_offline' here would stomp a more
 * specific daemon_* state every 5s and flicker the UI between the two.
 */
function handlePeersError(err: unknown, set: (partial: Partial<MeshState>) => void): void {
  if (err instanceof MeshBridgeUnavailableError) {
    set({ bridge: 'bridge_missing', daemonStatus: null, lastError: null });
    return;
  }
  if (err instanceof MeshSidecarUnreachableError) {
    set({ lastError: null });
    return;
  }
  if (err instanceof MeshApiError) {
    set({ lastError: err.message });
    return;
  }
  set({ lastError: err instanceof Error ? err.message : String(err) });
}

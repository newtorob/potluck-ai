/**
 * meshDiagnostics — connectivity diagnostics for the mesh (VPN-resilience Pillar #1).
 *
 * Replaces the single "offline" boolean with a NAMED, root-cause-suppressed
 * model in the spirit of Tailscale's `health.Warnable`: every condition has a
 * code, a layer, a severity, a human title + detail, and (where we know it) a
 * one-line fix. The point is that a peer is never just "offline" — it's
 * "their machine is off" vs "no encrypted tunnel (firewall/VPN?)" vs "we can't
 * even reach the coordinator", and those are four different fixes with four
 * different owners.
 *
 * This first slice derives everything from data we ALREADY have (the Go mesh
 * sidecar's GET /status + GET /peers, plus the renderer↔sidecar bridge state).
 * Richer signals (explicit handshake age, clock skew, key drift, NAT type) get
 * added to /peers later and feed the same model — the shapes below are the
 * contract.
 *
 * Root-cause suppression is structural: we evaluate layers outward (local →
 * coordinator → per-peer) and STOP emitting downstream diagnostics once a
 * blocking upstream cause is found, so the UI shows the one thing to fix, not a
 * cascade of derivative symptoms.
 *
 * Privacy invariant: diagnostics are metadata-only — interface/handshake state,
 * never prompt or inference content. Peer name/id are already shown in the UI.
 */

import type { MeshStatus, MeshPeer } from '@/types/mesh';
import type { BridgeReachability } from '@/stores/meshStore';

// ---------------------------------------------------------------------------
// Model
// ---------------------------------------------------------------------------

/** The 5-layer decomposition, collapsed to the ones we can probe cheaply today.
 *  (L2 relay-health and L3 self-reachability/NAT arrive with the netcheck slice.) */
export type DiagLayer = 'local' | 'coordinator' | 'peer';

export type DiagSeverity = 'ok' | 'info' | 'warn' | 'error';

export interface Diagnostic {
  /** Stable machine code, e.g. 'COORD_UNREACHABLE'. */
  code: string;
  layer: DiagLayer;
  severity: DiagSeverity;
  /** Short human label for a banner row. */
  title: string;
  /** One-line plain explanation. */
  detail: string;
  /** What to do about it, when we know. */
  fix?: string;
}

/** The single resolved state for a peer's status pill. */
export type PeerState =
  | 'direct' // connected, peer-to-peer
  | 'relayed' // connected, but via a relay (slower)
  | 'connecting' // known to coordinator, tunnel still coming up
  | 'no_data_plane' // coordinator sees them, but no encrypted tunnel (firewall/VPN/NAT)
  | 'key_drift' // this peer's key isn't provisioned on our tunnel (stale key / re-pair)
  | 'status_unknown'; // we can't say — an upstream cause (e.g. coordinator down) suppresses this

export interface PeerDiagnostic {
  peerId: string;
  state: PeerState;
  severity: DiagSeverity;
  /** Pill text, e.g. "Relayed", "Blocked — no tunnel". */
  label: string;
  detail: string;
  fix?: string;
}

export interface DiagnosticsReport {
  /** Unsuppressed local + coordinator diagnostics, worst-first. */
  global: Diagnostic[];
  /** Per-peer resolved state, keyed by node_id. */
  perPeer: Record<string, PeerDiagnostic>;
  /** Worst severity across everything unsuppressed. */
  overall: DiagSeverity;
  ok: boolean;
  /** One-line summary for the preflight banner. */
  headline: string;
}

export interface DiagnosticsInput {
  bridge: BridgeReachability;
  status: MeshStatus | null;
  peers: MeshPeer[];
}

// ---------------------------------------------------------------------------
// Severity helpers
// ---------------------------------------------------------------------------

const SEVERITY_RANK: Record<DiagSeverity, number> = { ok: 0, info: 1, warn: 2, error: 3 };

export function worstSeverity(a: DiagSeverity, b: DiagSeverity): DiagSeverity {
  return SEVERITY_RANK[a] >= SEVERITY_RANK[b] ? a : b;
}

/** A peer just joined and its tunnel may still be coming up — don't alarm yet. */
const CONNECTING_GRACE_MS = 15_000;

// ---------------------------------------------------------------------------
// Derivation
// ---------------------------------------------------------------------------

/**
 * Pure function: given the current bridge/status/peers snapshot, produce the
 * full diagnostics report. No I/O, no time-of-check side effects beyond reading
 * `now` for the connecting grace window.
 */
export function deriveDiagnostics(input: DiagnosticsInput, now: number = Date.now()): DiagnosticsReport {
  const { bridge, status, peers } = input;
  const global: Diagnostic[] = [];

  // --- L0: local — is the mesh sidecar even reachable from the UI? -----------
  if (bridge === 'bridge_missing') {
    // Running outside Electron (plain browser). Not an error the user can fix;
    // it just means there's no mesh here. Suppress everything downstream.
    return report(
      [
        {
          code: 'NO_MESH_BRIDGE',
          layer: 'local',
          severity: 'info',
          title: 'Mesh unavailable here',
          detail: 'The mesh runs in the desktop app; this environment has no mesh bridge.',
        },
      ],
      {},
      'Mesh unavailable in this environment',
    );
  }

  if (bridge === 'sidecar_offline') {
    // The local mesh service isn't answering. ROOT CAUSE — we know nothing
    // downstream, so suppress coordinator + per-peer checks entirely.
    return report(
      [
        {
          code: 'SIDECAR_OFFLINE',
          layer: 'local',
          severity: 'error',
          title: 'Mesh service not running',
          detail: 'The local mesh sidecar isn’t responding, so none of your machines are reachable.',
          fix: 'Restart Potluck (or the mesh service) on this machine.',
        },
      ],
      {},
      'Mesh service not running on this machine',
    );
  }

  // bridge === 'unknown' (first tick) or 'reachable'. Without a status payload
  // yet, we can't assess — stay quiet rather than flash a false error.
  if (!status) {
    return report([], {}, 'Checking mesh…');
  }

  // --- L0: local — mesh sidecar state ---------------------------------------
  switch (status.mesh_status) {
    case 'starting':
      // Transient; suppress downstream so we don't alarm during bring-up.
      return report(
        [
          {
            code: 'MESH_STARTING',
            layer: 'local',
            severity: 'info',
            title: 'Mesh starting…',
            detail: 'The mesh is coming up. Peers will appear once it’s connected.',
          },
        ],
        {},
        'Mesh starting…',
      );
    case 'stopped':
      return report(
        [
          {
            code: 'MESH_STOPPED',
            layer: 'local',
            severity: 'error',
            title: 'Mesh stopped',
            detail: 'The mesh isn’t running on this machine, so your other machines are unreachable.',
            fix: 'Start the mesh from My Machines (or restart Potluck).',
          },
        ],
        {},
        'Mesh stopped on this machine',
      );
    case 'error':
      return report(
        [
          {
            code: 'MESH_ERROR',
            layer: 'local',
            severity: 'error',
            title: 'Mesh error',
            detail: status.last_error?.trim()
              ? `The mesh reported: ${status.last_error.trim()}`
              : 'The mesh hit an error and isn’t carrying traffic.',
            fix: 'Restart the mesh on this machine; if it persists, check the mesh logs.',
          },
        ],
        {},
        'Mesh error on this machine',
      );
    case 'running':
    default:
      break; // healthy — continue to coordinator + peers
  }

  // --- L1: coordinator (control plane) --------------------------------------
  // The data plane (WireGuard) is independent of the coordinator, so a peer
  // with a fresh handshake is genuinely connected even if the coordinator is
  // down. But peer *presence* (the roster) comes from the coordinator — so when
  // it's unreachable we can't trust "this peer is offline", only "this peer is
  // (still) connected". We reflect that in the per-peer pass below.
  let coordinatorTrustworthy = true;
  switch (status.coordinator_connection) {
    case 'connecting':
      global.push({
        code: 'COORD_CONNECTING',
        layer: 'coordinator',
        severity: 'info',
        title: 'Connecting to coordinator…',
        detail: 'Reconnecting to the coordinator; new machines may not appear yet.',
      });
      coordinatorTrustworthy = false;
      break;
    case 'disconnected':
      global.push({
        code: 'COORD_UNREACHABLE',
        layer: 'coordinator',
        severity: 'warn',
        title: 'Can’t reach the coordinator',
        detail:
          'New machines won’t appear and presence is stale. Already-connected machines keep working.',
        fix: 'Check that the coordinator host is up and reachable from this machine (VPN/route/firewall).',
      });
      coordinatorTrustworthy = false;
      break;
    case 'connected':
    default:
      break;
  }

  // --- L4: per-peer (data plane) --------------------------------------------
  const perPeer: Record<string, PeerDiagnostic> = {};
  for (const peer of peers) {
    perPeer[peer.node_id] = derivePeer(peer, coordinatorTrustworthy, now);
  }

  // --- Roll up ---------------------------------------------------------------
  let overall: DiagSeverity = 'ok';
  for (const d of global) overall = worstSeverity(overall, d.severity);
  for (const id in perPeer) overall = worstSeverity(overall, perPeer[id].severity);

  const headline = computeHeadline(global, perPeer, peers.length);
  return report(sortBySeverity(global), perPeer, headline, overall);
}

function derivePeer(peer: MeshPeer, coordinatorTrustworthy: boolean, now: number): PeerDiagnostic {
  // A fresh WireGuard handshake is authoritative regardless of coordinator state.
  if (peer.reachable) {
    if (peer.reachability_mode === 'derp') {
      return {
        peerId: peer.node_id,
        state: 'relayed',
        severity: 'ok', // connected & working — the relay nuance lives on the peer pill, not the global banner
        label: 'Relayed',
        detail:
          'Connected, but through a relay (slower). A direct peer-to-peer link hasn’t formed yet, usually because of a VPN or strict NAT on one side.',
        fix: 'Often upgrades to direct on its own. Allowing the mesh range past a VPN can enable a direct link.',
      };
    }
    return {
      peerId: peer.node_id,
      state: 'direct',
      severity: 'ok',
      label: 'Direct',
      detail: 'Connected directly, peer-to-peer.',
    };
  }

  // Not reachable. If the coordinator is down we genuinely can't say why.
  if (!coordinatorTrustworthy) {
    return {
      peerId: peer.node_id,
      state: 'status_unknown',
      severity: 'info',
      label: 'Status unknown',
      detail: 'Can’t confirm this machine’s status while the coordinator is unreachable.',
    };
  }

  // Key not provisioned on our tunnel → a stale/drifted key (e.g. the peer
  // re-paired) or config that never applied. Different fix (re-sync) than a
  // firewall block. `key_configured` is absent on older sidecars — only act on
  // an explicit false.
  if (peer.key_configured === false) {
    return {
      peerId: peer.node_id,
      state: 'key_drift',
      severity: 'warn',
      label: 'Key out of date',
      detail:
        'This machine’s key isn’t provisioned on the tunnel, usually a stale key after it re-paired.',
      fix: 'Restart the mesh on this machine to re-sync keys (or re-pair this machine).',
    };
  }

  // Coordinator sees this peer (it’s in the roster) but there’s no encrypted
  // tunnel. If it only just appeared, the tunnel may still be forming.
  const lastSeenMs = peer.last_seen_at ? Date.parse(peer.last_seen_at) : NaN;
  const justAppeared = Number.isFinite(lastSeenMs) && now - lastSeenMs < CONNECTING_GRACE_MS;
  if (justAppeared) {
    return {
      peerId: peer.node_id,
      state: 'connecting',
      severity: 'info',
      label: 'Connecting…',
      detail: 'Establishing the encrypted tunnel to this machine.',
    };
  }

  // No tunnel. Split "dropped recently" (a handshake existed, now stale) from
  // "never formed" (no handshake ever) — usually a different story. Falls back
  // to the "never" copy when the sidecar doesn't report handshake age.
  const hadHandshake = typeof peer.handshake_age_seconds === 'number';
  return {
    peerId: peer.node_id,
    state: 'no_data_plane',
    severity: 'warn',
    label: hadHandshake ? 'Tunnel dropped' : 'No tunnel',
    detail: hadHandshake
      ? 'The encrypted tunnel was up but has gone quiet. Often a VPN reconnect, sleep/wake, or a network change broke the data path.'
      : 'The coordinator can see this machine, but no encrypted tunnel ever formed. Typically a firewall or VPN kill-switch blocking the mesh on one side.',
    fix: 'On both machines, allow the mesh interface past any VPN/firewall (a kill-switch will silently drop it).',
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function sortBySeverity(diags: Diagnostic[]): Diagnostic[] {
  return [...diags].sort((a, b) => SEVERITY_RANK[b.severity] - SEVERITY_RANK[a.severity]);
}

function computeHeadline(
  global: Diagnostic[],
  perPeer: Record<string, PeerDiagnostic>,
  peerCount: number,
): string {
  const worstGlobal = sortBySeverity(global)[0];
  if (worstGlobal && worstGlobal.severity !== 'ok' && worstGlobal.severity !== 'info') {
    return worstGlobal.title;
  }

  const peerStates = Object.values(perPeer);
  const blocked = peerStates.filter((p) => p.state === 'no_data_plane').length;
  if (blocked > 0) {
    return blocked === 1 ? '1 machine has no tunnel' : `${blocked} machines have no tunnel`;
  }

  if (worstGlobal && worstGlobal.severity === 'info') return worstGlobal.title;

  if (peerCount === 0) return 'Mesh up, no other machines yet';
  const relayed = peerStates.filter((p) => p.state === 'relayed').length;
  const direct = peerStates.filter((p) => p.state === 'direct').length;
  if (relayed > 0 && direct === 0) return 'Mesh healthy, connected via relay';
  return 'Mesh healthy';
}

function report(
  global: Diagnostic[],
  perPeer: Record<string, PeerDiagnostic>,
  headline: string,
  overrideOverall?: DiagSeverity,
): DiagnosticsReport {
  let overall: DiagSeverity = overrideOverall ?? 'ok';
  if (overrideOverall === undefined) {
    for (const d of global) overall = worstSeverity(overall, d.severity);
    for (const id in perPeer) overall = worstSeverity(overall, perPeer[id].severity);
  }
  return { global, perPeer, overall, ok: overall === 'ok', headline };
}

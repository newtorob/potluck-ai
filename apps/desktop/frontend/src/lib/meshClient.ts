/**
 * Mesh client — typed wrapper over `window.potluck.mesh.request` (the
 * Electron bridge to the Go sidecar's Unix-socket IPC) plus a thin
 * coordinator-side helper for the pairing-token issuance flow.
 *
 * Two transport surfaces in this file:
 *
 *   1. **Local IPC** (via the Electron bridge). For status / peers / pair /
 *      unpair. Goes over the Unix socket — never network.
 *   2. **Coordinator HTTP** (via `coordinatorAuth.coordinatorSessionRequest`).
 *      For issuing pairing tokens. Session-authenticated — see that module's
 *      doc comment. Reads the coordinator URL from connectionStore so users
 *      can target a coordinator on another machine in their fleet.
 *
 * Error model:
 *   - `MeshBridgeUnavailableError` — `window.potluck.mesh` isn't there.
 *     Usually means we're running in a plain browser (Vite dev outside
 *     Electron); the UI should fall back to an offline message.
 *   - `MeshSidecarUnreachableError` — bridge is present but the sidecar
 *     socket isn't accepting connections. User hasn't installed / started
 *     the mesh sidecar yet.
 *   - `MeshApiError` — sidecar responded with HTTP >= 400. `code` is the
 *     stable identifier from `routers/mesh.go` (e.g. `already_enrolled`,
 *     `pair_in_progress`, `coordinator_error`).
 *   - `CoordinatorError` — coordinator's HTTP response was non-2xx.
 */

import type {
  MeshStatus,
  MeshPeersResponse,
  PairRequest,
  EnrollResponse,
  PairingTokenIssueResponse,
  CurrentHouseholdResponse,
  MeshBridgeResult,
  MeshDaemonStatus,
} from '@/types/mesh';
import { coordinatorSessionRequest, detailOf } from '@/lib/coordinatorAuth';

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export class MeshBridgeUnavailableError extends Error {
  readonly kind = 'bridge_unavailable' as const;
  constructor() {
    super(
      'Mesh bridge not available. Potluck must run inside the Electron app to talk to the sidecar.',
    );
    this.name = 'MeshBridgeUnavailableError';
  }
}

export class MeshSidecarUnreachableError extends Error {
  readonly kind = 'sidecar_unreachable' as const;
  constructor(reason?: string) {
    super(reason ?? 'The mesh sidecar is not running on this machine.');
    this.name = 'MeshSidecarUnreachableError';
  }
}

export class MeshApiError extends Error {
  readonly kind = 'api_error' as const;
  readonly code: string;
  readonly httpStatus: number;
  constructor(code: string, message: string, httpStatus: number) {
    super(message);
    this.name = 'MeshApiError';
    this.code = code;
    this.httpStatus = httpStatus;
  }
}

export class CoordinatorError extends Error {
  readonly kind = 'coordinator_error' as const;
  readonly httpStatus: number;
  constructor(message: string, httpStatus: number) {
    super(message);
    this.name = 'CoordinatorError';
    this.httpStatus = httpStatus;
  }
}

// ---------------------------------------------------------------------------
// Bridge access
// ---------------------------------------------------------------------------

interface MeshBridge {
  request: <T>(
    method: 'GET' | 'POST' | 'DELETE',
    path: string,
    body?: unknown,
  ) => Promise<MeshBridgeResult<T>>;
}

function getBridge(): MeshBridge {
  const w = window as unknown as { potluck?: { mesh?: MeshBridge } };
  if (!w.potluck || !w.potluck.mesh) {
    throw new MeshBridgeUnavailableError();
  }
  return w.potluck.mesh;
}

/** True when the Electron mesh bridge is present (Electron-only). */
export function isMeshBridgeAvailable(): boolean {
  const w = window as unknown as { potluck?: { mesh?: unknown } };
  return Boolean(w.potluck && w.potluck.mesh);
}

async function meshRequest<T>(
  method: 'GET' | 'POST' | 'DELETE',
  path: string,
  body?: unknown,
): Promise<T> {
  const bridge = getBridge();
  const result = await bridge.request<T>(method, path, body);

  // Transport-layer failure — status 0 with `error.code` of
  // `sidecar_unreachable` or `timeout` per main.js.
  if (result.status === 0) {
    const err = result.body as { error?: { code: string; message: string } } | null;
    const code = err?.error?.code ?? 'unknown';
    const message = err?.error?.message ?? 'sidecar unreachable';
    throw new MeshSidecarUnreachableError(`${code}: ${message}`);
  }

  // Application-layer error — sidecar returned HTTP 4xx/5xx with the
  // `{ error: { code, message } }` envelope.
  if (result.status >= 400) {
    const err = result.body as { error?: { code: string; message: string } } | null;
    const code = err?.error?.code ?? 'unknown';
    const message = err?.error?.message ?? `HTTP ${result.status}`;
    throw new MeshApiError(code, message, result.status);
  }

  // 204 No Content from /unpair has body === null. Caller types `T` as
  // `void` for those calls.
  return (result.body ?? undefined) as T;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export const meshClient = {
  /** Snapshot of sidecar state. Polled by the household view at 2s cadence. */
  status: () => meshRequest<MeshStatus>('GET', '/status'),

  /** Current household roster (sidecar's local cache, refreshed by signal). */
  peers: () => meshRequest<MeshPeersResponse>('GET', '/peers'),

  /**
   * Enroll this machine using a pairing token. Returns the EnrollResponse
   * with the freshly-allocated mesh IP. Pair-in-progress and already-
   * enrolled are surfaced as `MeshApiError` with stable `code` values.
   */
  pair: (req: PairRequest) => meshRequest<EnrollResponse>('POST', '/pair', req),

  /** Wipe local keys + leave the household. Returns nothing. */
  unpair: () => meshRequest<void>('POST', '/unpair'),

  /**
   * Start broadcasting a pairing invite on the local network so a machine on
   * the same LAN can discover it without manual copy-paste. Enrolled machine
   * only; the sidecar stops after a short window.
   *
   * The raw token is NEVER put on the wire — only a one-way fingerprint of
   * it. The response additively includes `confirmation_pin`, a 6-digit code
   * generated locally that the caller must display so a human can read it
   * to whoever is joining; without it, a LAN listener that only captured
   * the fingerprint cannot redeem a token via POST /pairing/reveal.
   */
  broadcastPairingToken: (token: string) =>
    meshRequest<{ broadcasting: boolean; window_seconds: number; confirmation_pin: string }>(
      'POST',
      '/pairing/broadcast',
      { token },
    ),

  /** A pairing invite heard on the LAN, if any (unenrolled machine only).
   *  Carries a fingerprint only — never a redeemable token. */
  discoveredPairing: () => meshRequest<DiscoveredPairing>('GET', '/pairing/discovered'),

  /**
   * Exchange a human-entered PIN for the actual pairing token held by the
   * inviter. The inviter caps wrong guesses at 5 per broadcast session
   * (server-side, `internal/lanpair.maxRevealAttempts`); after the cap the
   * broadcast is cancelled and further reveal calls time out rather than
   * returning a distinguishable "exhausted" error, so the caller is
   * expected to track its own attempt count as a UX nicety.
   */
  revealPairing: (fingerprint: string, pin: string) =>
    meshRequest<{ token: string }>('POST', '/pairing/reveal', { fingerprint, pin }),
};

/** Result of GET /pairing/discovered. */
export type DiscoveredPairing =
  | { found: false }
  | { found: true; hostname: string; fingerprint: string; pin_required: true };

// ---------------------------------------------------------------------------
// Daemon supervision — "is there a mesh sidecar to talk to at all"
// ---------------------------------------------------------------------------
//
// Separate bridge surface from `meshClient` above: that one assumes a
// running sidecar and talks to its socket. This one is about bringing the
// sidecar itself up — on macOS, registering it as a root LaunchDaemon via
// SMAppService. See apps/desktop/electron/main.js's mesh-daemon-supervision
// section for the full state machine and apps/desktop/electron/preload.js
// for the exact bridge shape this wraps.

interface MeshDaemonBridge {
  daemonStatus: () => Promise<MeshDaemonStatus>;
  enable: () => Promise<MeshDaemonStatus>;
  openLoginItems: () => Promise<{ platform: string; ok: boolean }>;
}

function getDaemonBridge(): MeshDaemonBridge {
  const w = window as unknown as { potluck?: { mesh?: Partial<MeshDaemonBridge> } };
  const bridge = w.potluck?.mesh;
  if (
    !bridge ||
    typeof bridge.daemonStatus !== 'function' ||
    typeof bridge.enable !== 'function' ||
    typeof bridge.openLoginItems !== 'function'
  ) {
    throw new MeshBridgeUnavailableError();
  }
  return bridge as MeshDaemonBridge;
}

/**
 * True when the daemon-supervision bridge (daemonStatus/enable/
 * openLoginItems) is present. Same "running outside Electron, or a stale
 * preload script" guard as `isMeshBridgeAvailable`, scoped to this
 * sub-surface — use this before calling into `meshDaemon` from code that
 * needs to degrade gracefully (a plain browser, or a dev preload that
 * predates this bridge) instead of throwing.
 */
export function isMeshDaemonBridgeAvailable(): boolean {
  const w = window as unknown as { potluck?: { mesh?: Partial<MeshDaemonBridge> } };
  return Boolean(w.potluck?.mesh && typeof w.potluck.mesh.daemonStatus === 'function');
}

/**
 * Mesh daemon supervision. macOS today: backed by SMAppService via main.js's
 * signed mesh-helper CLI. Linux/Windows always resolve
 * `{ state: 'unsupported' }` for now — see main.js for the TODOs.
 */
export const meshDaemon = {
  /** Current daemon status. Cheap enough to poll on the household 2s cadence. */
  status: (): Promise<MeshDaemonStatus> => getDaemonBridge().daemonStatus(),

  /**
   * Registers (or re-registers) the mesh daemon. Resolving with
   * `state: 'requires_approval'` is a normal, successful outcome — the
   * daemon is registered but macOS needs the user to approve it in System
   * Settings before it starts. It is not an error.
   */
  enable: (): Promise<MeshDaemonStatus> => getDaemonBridge().enable(),

  /** Opens System Settings so the user can approve the already-registered
   *  daemon. */
  openLoginItems: (): Promise<{ platform: string; ok: boolean }> =>
    getDaemonBridge().openLoginItems(),
};

// ---------------------------------------------------------------------------
// Coordinator helpers (session-authenticated HTTP — see coordinatorAuth.ts)
// ---------------------------------------------------------------------------

/**
 * Ensure the calling member has a household, then issue a fresh pairing
 * token. Returns the plaintext token (only exposed in this response — the
 * coordinator stores its primary-key form server-side).
 *
 * Both calls are session-authenticated (not gated by the coordinator token —
 * see `coordinatorSessionRequest`'s doc comment): the coordinator resolves
 * the calling member from the bearer this attaches, via
 * `resolve_session_member_id`.
 *
 * `POST /mesh/households` is idempotent for the calling member; we call it
 * unconditionally to side-step the 404-then-retry dance.
 */
export async function issuePairingToken(): Promise<PairingTokenIssueResponse> {
  // Idempotent household-bootstrap; ignore the body, we just need the
  // household row to exist so the next call doesn't 404.
  const householdRes = await coordinatorSessionRequest('POST', '/mesh/households', {
    name: 'My household',
  });
  if (householdRes.status !== 200 && householdRes.status !== 201) {
    throw new CoordinatorError(
      `Could not create household: ${detailOf(householdRes.body)}`,
      householdRes.status,
    );
  }

  const tokenRes = await coordinatorSessionRequest('POST', '/mesh/pairing-tokens');
  if (tokenRes.status !== 200 && tokenRes.status !== 201) {
    throw new CoordinatorError(
      `Could not issue pairing token: ${detailOf(tokenRes.body)}`,
      tokenRes.status,
    );
  }

  return tokenRes.body as PairingTokenIssueResponse;
}

/**
 * Detect whether the signed-in member already has a household (and which
 * machines are in it), so the unpaired-machine UI can offer a one-tap "join
 * your household" instead of a pairing code — and never spawn a duplicate
 * household. Session-authenticated, same bearer as issuePairingToken().
 * Returns `{ household: null, machines: [] }` when this is the first machine.
 */
export async function getCurrentHousehold(): Promise<CurrentHouseholdResponse> {
  const res = await coordinatorSessionRequest('GET', '/mesh/households/current');
  if (res.status !== 200) {
    throw new CoordinatorError(
      `Could not read your household: ${detailOf(res.body)}`,
      res.status,
    );
  }
  return res.body as CurrentHouseholdResponse;
}

/**
 * Remove a machine from the signed-in member's own household — for dropping a
 * stale/ghost node left behind when a machine was wiped or reinstalled without
 * unpairing. Session-authenticated; the coordinator verifies the node belongs
 * to the caller's household. Idempotent (204 even if already gone).
 */
export async function removeMachine(nodeId: string): Promise<void> {
  const res = await coordinatorSessionRequest(
    'DELETE',
    `/mesh/households/current/machines/${nodeId}`,
  );
  if (res.status !== 204 && res.status !== 200) {
    throw new CoordinatorError(
      `Could not remove machine: ${detailOf(res.body)}`,
      res.status,
    );
  }
}

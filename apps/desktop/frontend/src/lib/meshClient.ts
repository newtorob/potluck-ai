/**
 * Mesh client — typed wrapper over `window.potluck.mesh.request` (the
 * Electron bridge to the Go sidecar's Unix-socket IPC) plus a thin
 * coordinator-side helper for the pairing-token issuance flow.
 *
 * Two transport surfaces in this file:
 *
 *   1. **Local IPC** (via the Electron bridge). For status / peers / pair /
 *      unpair. Goes over the Unix socket — never network.
 *   2. **Coordinator HTTP** (direct fetch from the renderer). For issuing
 *      pairing tokens. Reads the coordinator URL from connectionStore so
 *      users can target a coordinator on another machine in their fleet.
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
  MeshBridgeResult,
} from '@/types/mesh';
import { coordinatorBase } from '@/stores/connectionStore';

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
   * Start broadcasting a pairing token on the local network so a machine on the
   * same LAN can pick it up without manual copy-paste. Enrolled machine only;
   * the sidecar stops after a short window. Token + hostname only on the wire.
   */
  broadcastPairingToken: (token: string) =>
    meshRequest<{ broadcasting: boolean; window_seconds: number }>(
      'POST',
      '/pairing/broadcast',
      { token },
    ),

  /** A pairing token heard on the LAN, if any (unenrolled machine only). */
  discoveredPairing: () => meshRequest<DiscoveredPairing>('GET', '/pairing/discovered'),
};

/** Result of GET /pairing/discovered. */
export type DiscoveredPairing =
  | { found: false }
  | { found: true; token: string; hostname: string };

// ---------------------------------------------------------------------------
// Coordinator helpers (direct HTTP, not via the bridge)
// ---------------------------------------------------------------------------

/**
 * Ensure the dev-stub member has a household, then issue a fresh pairing
 * token. Returns the plaintext token (only exposed in this response — the
 * coordinator stores its primary-key form server-side).
 *
 * `POST /mesh/households` is idempotent for the dev-stub member; we call
 * it unconditionally to side-step the 404-then-retry dance.
 *
 * When real member auth lands (v0.5), the household-create call moves out
 * of this helper and becomes part of the sign-in flow.
 */
export async function issuePairingToken(): Promise<PairingTokenIssueResponse> {
  const base = coordinatorBase();

  // Idempotent household-bootstrap; ignore the body, we just need the
  // household row to exist so the next call doesn't 404.
  const householdRes = await fetch(`${base}/mesh/households`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: 'My household' }),
  });
  if (!householdRes.ok && householdRes.status !== 201 && householdRes.status !== 200) {
    const detail = await safeDetail(householdRes);
    throw new CoordinatorError(
      `Could not create household: ${detail}`,
      householdRes.status,
    );
  }

  const tokenRes = await fetch(`${base}/mesh/pairing-tokens`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!tokenRes.ok) {
    const detail = await safeDetail(tokenRes);
    throw new CoordinatorError(
      `Could not issue pairing token: ${detail}`,
      tokenRes.status,
    );
  }

  return (await tokenRes.json()) as PairingTokenIssueResponse;
}

async function safeDetail(res: Response): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: unknown };
    if (data && typeof data.detail === 'string') return data.detail;
    return res.statusText || `HTTP ${res.status}`;
  } catch {
    return res.statusText || `HTTP ${res.status}`;
  }
}

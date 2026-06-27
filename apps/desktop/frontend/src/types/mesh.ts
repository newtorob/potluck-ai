/**
 * Mesh types — mirror the on-the-wire shapes from the Go sidecar's
 * `internal/localapi` package and the coordinator's `routers/mesh.py`.
 *
 * Sources of truth:
 *   - apps/desktop/go/potluck-mesh/internal/localapi/server.go
 *   - services/coordinator/coordinator/schemas/mesh.py
 *   - docs/phase-g/local-ipc.md
 *
 * Keep these in sync when the contract changes. The IPC sends an
 * X-Potluck-Protocol-Version header on every response; if we bump to
 * version 2 we must gate this at the bridge layer.
 */

// ---------------------------------------------------------------------------
// Local IPC: GET /status
// ---------------------------------------------------------------------------

export type MeshStatusValue = 'starting' | 'running' | 'stopped' | 'error';

export type CoordinatorConnState = 'connected' | 'connecting' | 'disconnected';

export interface MeshStatus {
  version: string;
  mesh_status: MeshStatusValue;
  node_id: string | null;
  household_id: string | null;
  mesh_ip: string | null;
  wg_public_key: string | null;
  external_endpoint: string | null;
  coordinator_connection: CoordinatorConnState;
  /** The coordinator URL the Go sidecar is actually dialing (from its launch
   *  flag). Always present per the v2 local IPC contract. Use this as the
   *  source of truth for which coordinator the mesh is connected to. */
  coordinator_url: string;
  peers_online_count: number;
  last_error: string | null;
}

// ---------------------------------------------------------------------------
// Local IPC: GET /peers
// ---------------------------------------------------------------------------

export type ReachabilityMode = 'direct' | 'derp';

export interface MeshPeer {
  node_id: string;
  node_name: string;
  mesh_ip: string;
  wg_public_key: string;
  last_endpoint: string | null;
  last_seen_at: string | null;
  platform: string | null;
  reachable: boolean;
  reachability_mode: ReachabilityMode | null;
  /** Age of the last WireGuard handshake, in seconds. Absent/null when no
   *  handshake has ever completed — lets the UI tell "stale" from "never".
   *  Optional: older mesh sidecars don't send it. */
  handshake_age_seconds?: number | null;
  /** Whether this peer's key is provisioned on the WG device. Absent = unknown
   *  (older sidecar). `false` while unreachable = key drift, not a firewall. */
  key_configured?: boolean | null;
}

export interface MeshPeersResponse {
  peers: MeshPeer[];
}

// ---------------------------------------------------------------------------
// Local IPC: POST /pair / POST /unpair
// ---------------------------------------------------------------------------

export interface PairRequest {
  pairing_token: string;
  node_name?: string;
}

export interface EnrollResponse {
  node_id: string;
  household_id: string;
  mesh_ip: string;
  mesh_cidr: string;
  peers: MeshPeer[];
}

// ---------------------------------------------------------------------------
// Coordinator: POST /mesh/pairing-tokens
// ---------------------------------------------------------------------------

export interface PairingTokenIssueResponse {
  token: string;
  household_id: string;
  expires_at: string; // ISO 8601
  created_at: string; // ISO 8601
}

// ---------------------------------------------------------------------------
// Bridge transport result (Electron main.js → renderer)
// ---------------------------------------------------------------------------

export interface MeshBridgeResult<T> {
  status: number;
  body: T | { error: { code: string; message: string } } | null;
}

// status === 0 in the bridge result indicates a transport-layer failure
// (sidecar unreachable or timeout). Renderer code translates this into
// MeshSidecarUnreachableError via the meshClient wrapper.

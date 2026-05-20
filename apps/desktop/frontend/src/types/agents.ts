/**
 * Agent observability types — mirrors the Python pydantic models in routers/agents.py.
 *
 * These types are the single source of truth for the /agents dashboard.
 * Never widen these — keep them in sync with the backend contract exactly.
 */

export type Transport = 'stdio' | 'tcp' | 'internal';
export type ModelSource = 'self-reported' | 'inferred';

/** Computed server-side from last_seen_at + disconnected_at.
 *  - 'live'         — heartbeat within the last 60s (connected now)
 *  - 'idle'         — last_seen 60s–5min (connected, quiet)
 *  - 'stale'        — last_seen >5min, not yet swept to disconnected
 *  - 'disconnected' — disconnected_at is set
 *  The AgentsView renders these in distinct sections. */
export type Liveness = 'live' | 'idle' | 'stale' | 'disconnected';

export interface AgentSession {
  id: string;
  client_name: string | null;         // "claude-code", "cursor", "potluck-chat"
  client_version: string | null;
  transport: Transport;
  pid: number | null;
  project: string | null;
  model: string | null;
  model_source: ModelSource | null;
  peer_mcps: string[];                // ["gmail", "supabase", ...]
  started_at: string;                 // ISO-8601 UTC
  last_seen_at: string;
  disconnected_at: string | null;
  tool_call_count: number;
  /** Stable adjective-animal nickname assigned at session create. Used to
   *  disambiguate multiple simultaneous instances of the same client.
   *  May be null for sessions created before the friendly_name migration. */
  friendly_name: string | null;
  liveness: Liveness;
}

export interface ToolCall {
  id: string;
  session_id: string;
  tool_name: string;                  // "memory_search", "task_create", etc.
  args_summary: Record<string, unknown> | null; // metadata only — never prompt content
  result_count: number | null;
  duration_ms: number | null;
  error: string | null;
  called_at: string;
  /** Version of the MCP tool-schema contract this call ran under.
   *  NULL for rows written before the 2026-05-20 contract-versioning rollout. */
  tool_schema_version: string | null;
}

export interface SessionDetail extends AgentSession {
  recent_tool_calls: ToolCall[];
}

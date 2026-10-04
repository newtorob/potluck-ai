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
  /** IDs of the memories this call read or wrote, when applicable. NULL on
   *  non-memory tools, and on pre-debugger rows. The debugger calls
   *  POST /agents/memory-previews to expand these into previews. */
  memory_ids_touched: string[] | null;
}

/** Lightweight memory preview returned by POST /agents/memory-previews —
 *  never includes full content, just the first 200 chars. */
export interface MemoryPreview {
  id: string;
  preview: string;
  category: string;
  project: string | null;
  created_at: string;
  is_active: boolean;
}

export interface SessionDetail extends AgentSession {
  recent_tool_calls: ToolCall[];
}

export type AgentProvider = 'codex' | 'claude';
export type AgentRunStatus = 'active' | 'completed' | 'failed' | 'interrupted';
export type AttributionConfidence = 'exact' | 'inferred' | 'unknown';
export type AgentConnectorState =
  | 'not-installed'
  | 'partial'
  | 'installed'
  | 'invalid'
  | 'unavailable';

export interface AgentConnectorStatus {
  provider: AgentProvider;
  project_path: string;
  config_path: string;
  state: AgentConnectorState;
  bridge_available: boolean;
  config_exists: boolean;
  installed_events: string[];
  missing_events: string[];
  stale_events: string[];
  planned_connect_events: string[];
  planned_disconnect_events: string[];
  manual_config: string;
  error: string | null;
}

export interface AgentRun {
  id: string;
  provider: AgentProvider;
  provider_session_id: string;
  project: string | null;
  model: string | null;
  status: AgentRunStatus;
  capture_profile: 'metadata-only' | 'managed-local';
  capture_note: string | null;
  control_mode: 'observed' | 'managed';
  task_id: string | null;
  cwd: string | null;
  started_at: string;
  ended_at: string | null;
  last_event_at: string;
  agent_count: number;
  event_count: number;
  error_count: number;
}

export interface AgentInstance {
  id: string;
  run_id: string;
  provider_agent_id: string;
  parent_instance_id: string | null;
  kind: 'root' | 'subagent';
  label: string;
  model: string | null;
  status: 'active' | 'completed' | 'failed';
  started_at: string;
  ended_at: string | null;
}

export interface AgentEvent {
  seq: number;
  id: string;
  run_id: string;
  agent_instance_id: string | null;
  event_type: string;
  provider_event_name: string;
  correlation_id: string | null;
  turn_id: string | null;
  tool_name: string | null;
  status: string;
  occurred_at: string;
  received_at: string;
  duration_ms: number | null;
  attribution_confidence: AttributionConfidence;
  metadata: Record<string, string>;
}

export interface AgentResource {
  id: number;
  event_id: string;
  kind: 'file' | 'memory' | 'task';
  resource_ref: string;
  action: string;
}

export interface AgentRunDetail {
  run: AgentRun;
  instances: AgentInstance[];
  events: AgentEvent[];
  resources: AgentResource[];
  next_cursor: number;
}

export interface AgentRunEventDelta {
  instances: AgentInstance[];
  events: AgentEvent[];
  resources: AgentResource[];
  next_cursor: number;
}

export type ManagedAgentStatus =
  | 'starting'
  | 'running'
  | 'waiting_approval'
  | 'idle'
  | 'interrupted'
  | 'failed'
  | 'stopped';
export type ManagedSandboxMode = 'read-only' | 'workspace-write';
export type ManagedApprovalDecision = 'accept' | 'acceptForSession' | 'decline' | 'cancel';

export interface ManagedAgentCapabilities {
  provider: 'codex';
  available: boolean;
  version: string | null;
  features: string[];
  error: string | null;
}

export interface ManagedAgentMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  status: 'streaming' | 'complete';
  created_at: string;
  kind: 'message' | 'steer';
}

export interface ManagedAgentItem {
  id: string;
  item_type: string;
  label: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  duration_ms: number | null;
  detail: string | null;
}

export interface ManagedAgentApproval {
  id: string;
  kind: 'command' | 'file_change';
  title: string;
  thread_id: string;
  turn_id: string;
  item_id: string;
  requested_at: string;
  command: string | null;
  cwd: string | null;
  reason: string | null;
  available_decisions: string[];
}

export interface ManagedAgentSession {
  id: string;
  run_id: string;
  provider: 'codex';
  control_mode: 'managed';
  task_id: string;
  task_name: string;
  cwd: string;
  project: string;
  model: string | null;
  sandbox: ManagedSandboxMode;
  status: ManagedAgentStatus;
  active_turn_id: string | null;
  started_at: string;
  updated_at: string;
  error: string | null;
  messages: ManagedAgentMessage[];
  items: ManagedAgentItem[];
  approvals: ManagedAgentApproval[];
  capabilities: string[];
}

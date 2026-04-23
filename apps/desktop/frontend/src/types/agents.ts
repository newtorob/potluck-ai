/**
 * Agent observability types — mirrors the Python pydantic models in routers/agents.py.
 *
 * These types are the single source of truth for the /agents dashboard.
 * Never widen these — keep them in sync with the backend contract exactly.
 */

export type Transport = 'stdio' | 'tcp' | 'internal';
export type ModelSource = 'self-reported' | 'inferred';

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
}

export interface SessionDetail extends AgentSession {
  recent_tool_calls: ToolCall[];
}

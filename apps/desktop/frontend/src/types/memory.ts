/**
 * Memory domain types — mirrors the Python sidecar Memory model.
 *
 * Single source of truth for the Memory shape across the frontend.
 * All memory-related components, stores, and hooks import from here.
 */

export type MemoryCategory =
  | 'preference'
  | 'fact'
  | 'decision'
  | 'project'
  | 'person'
  | 'instruction';

export type MemorySourceType = 'extracted' | 'explicit' | 'agent_state';

export interface Memory {
  id: string;
  content: string;
  category: MemoryCategory;
  confidence: number; // 0-1
  source_type: MemorySourceType;
  source_conversation_id: string | null;
  created_at: string; // ISO-8601 UTC
  last_accessed_at: string;
  access_count: number;
  is_active: boolean;
  superseded_by: string | null;
  project?: string | null;
}

// ---------------------------------------------------------------------------
// Project summary — returned by GET /memory/projects
// ---------------------------------------------------------------------------

export interface ProjectSummary {
  project: string;
  memory_count: number;
  task_count: number;
}

// ---------------------------------------------------------------------------
// API request/response shapes
// ---------------------------------------------------------------------------

export interface MemoryListParams {
  category?: MemoryCategory;
  limit?: number;
  offset?: number;
  project?: string | null;
  include_all?: boolean;
}

export interface MemoryCreateInput {
  content: string;
  category: MemoryCategory;
  conversation_id?: string;
  confidence?: number;
  project?: string | null;
}

export interface MemoryUpdateInput {
  content?: string;
  category?: MemoryCategory;
  confidence?: number;
  project?: string | null;
}

export interface MemorySearchInput {
  query?: string;
  current_messages?: Array<{ role: string; content: string }>;
  max_memories?: number;
  min_confidence?: number;
  project?: string | null;
}

export interface MemoryImportResult {
  imported: number;
  skipped: number;
}

export interface MemoryWipeResult {
  status: 'wiped';
}

export interface MemoryExportResult {
  memories: Memory[];
}

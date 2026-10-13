/**
 * Task domain types - mirrors the Python sidecar TaskState model.
 *
 * Single source of truth for task shapes across the frontend.
 * All task-related components, stores, and hooks import from here.
 */

export type TaskStatus = 'active' | 'paused' | 'completed' | 'abandoned';

export interface TaskError {
  error: string;
  resolution: string;
  timestamp: string;
}

export interface TaskState {
  id: string;
  task_name: string;
  status: TaskStatus;
  objective: string;
  completed_steps: string[];
  pending_steps: string[];
  known_constraints: string[];
  context_summary: string;
  files_touched: string[];
  errors_encountered: TaskError[];
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  conversation_id: string | null;
  project?: string | null;
}

// ---------------------------------------------------------------------------
// API request/response shapes
// ---------------------------------------------------------------------------

export interface TaskCreateInput {
  task_name: string;
  objective: string;
  known_constraints?: string[];
  pending_steps?: string[];
  metadata?: Record<string, unknown>;
  conversation_id?: string;
  project?: string | null;
}

export type TaskUpdateInput = Partial<
  Pick<
    TaskState,
    | 'task_name'
    | 'status'
    | 'objective'
    | 'completed_steps'
    | 'pending_steps'
    | 'known_constraints'
    | 'context_summary'
    | 'files_touched'
    | 'errors_encountered'
    | 'metadata'
    | 'conversation_id'
  >
>;

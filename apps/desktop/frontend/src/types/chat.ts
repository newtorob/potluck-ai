/**
 * Chat domain types.
 *
 * Shapes mirror the Python sidecar's Pydantic models so serialisation
 * round-trips without transformation. See guide §4.5 and
 * apps/desktop/python/potluck_local/routers/chat.py.
 */

export type MessageRole = 'system' | 'user' | 'assistant';

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  /** Unix timestamp (ms) when the message was created */
  createdAt: number;
}

export interface ChatOptions {
  temperature?: number;
  maxTokens?: number;
  /** AbortSignal from the caller so streaming can be cancelled */
  abortSignal?: AbortSignal;
}

/**
 * A single chunk from the SSE stream.
 * Shape matches the OpenAI-compatible format emitted by the Python runtime.
 */
export interface StreamChunk {
  choices: [
    {
      delta: {
        content?: string;
        role?: MessageRole;
      };
      finish_reason: string | null;
    },
  ];
}

export interface Conversation {
  id: string;
  title: string;
  messages: ChatMessage[];
  createdAt: number;
  updatedAt: number;
}

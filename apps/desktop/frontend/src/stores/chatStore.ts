/**
 * Chat store — conversations, streaming state, and send/abort actions.
 *
 * All conversation data is in-memory only (v0). No disk persistence.
 * A single empty conversation is seeded on initialization.
 *
 * The `sendMessage` action is the core orchestrator:
 *   1. Appends the user message.
 *   2. Creates an empty assistant placeholder.
 *   3. Opens the SSE stream via localApi.streamChat.
 *   4. Appends tokens as they arrive.
 *   5. Marks the stream complete (or partial on abort/error).
 */

import { create } from 'zustand';
import type { ChatMessage, Conversation } from '@/types/chat';
import { localApi } from '@/lib/localApi';
import { streamTokens } from '@/lib/streaming';

// ---------------------------------------------------------------------------
// Extended message type with streaming metadata
// ---------------------------------------------------------------------------

export type MessageStatus = 'complete' | 'streaming' | 'partial' | 'error';

export interface AppMessage extends ChatMessage {
  status: MessageStatus;
  /** Only set on error messages */
  errorText?: string;
}

export interface AppConversation extends Omit<Conversation, 'messages'> {
  modelSlug: string | null;
  messages: AppMessage[];
}

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------

interface ChatState {
  conversations: Record<string, AppConversation>;
  activeConversationId: string | null;
  isStreaming: boolean;
  streamingMessageId: string | null;
  /** Not serialised — held only for abort() */
  abortController: AbortController | null;

  // ---- Phase G m3 #19: contributor-network routing ---- //
  // When true, sendMessage() routes the latest user prompt through the
  // coordinator's POST /jobs/stream instead of the local /chat/completions.
  // Single-turn for now (history not forwarded — see localApi.streamNetworkJob).
  networkMode: boolean;
  networkModelSlug: string;

  // Actions
  createConversation: (modelSlug?: string) => string;
  deleteConversation: (id: string) => void;
  switchConversation: (id: string) => void;
  renameConversation: (id: string, title: string) => void;
  sendMessage: (content: string) => Promise<void>;
  abort: () => void;
  clearConversations: () => void;
  setNetworkMode: (on: boolean) => void;
  setNetworkModelSlug: (slug: string) => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeId(): string {
  return crypto.randomUUID();
}

function makeConversation(modelSlug: string | null = null): AppConversation {
  const now = Date.now();
  return {
    id: makeId(),
    title: 'New chat',
    modelSlug,
    messages: [],
    createdAt: now,
    updatedAt: now,
  };
}

/** Derive a conversation title from the first user message (max 40 chars). */
function titleFromContent(content: string): string {
  const trimmed = content.trim().replace(/\s+/g, ' ');
  return trimmed.length > 40 ? `${trimmed.slice(0, 40)}…` : trimmed;
}

// ---------------------------------------------------------------------------
// Seed
// ---------------------------------------------------------------------------

const seedConversation = makeConversation();

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useChatStore = create<ChatState>()((set, get) => ({
  conversations: { [seedConversation.id]: seedConversation },
  activeConversationId: seedConversation.id,
  isStreaming: false,
  streamingMessageId: null,
  abortController: null,
  networkMode: false,
  // Default slug — qwen2.5:3b-instruct-q4_K_M is what both the Linux
  // media server and the Windows gaming PC advertise via Ollama.
  // Configurable via the model picker in /network (task 776cf3a7).
  networkModelSlug: 'qwen2.5:3b-instruct-q4_K_M',

  createConversation(modelSlug) {
    const convo = makeConversation(modelSlug ?? null);
    set((s) => ({
      conversations: { ...s.conversations, [convo.id]: convo },
      activeConversationId: convo.id,
    }));
    return convo.id;
  },

  deleteConversation(id) {
    set((s) => {
      const next = { ...s.conversations };
      delete next[id];

      const remaining = Object.keys(next);
      let activeId = s.activeConversationId;

      if (activeId === id) {
        const firstId = remaining[0];
        if (firstId !== undefined) {
          // Pick the most-recently-updated remaining conversation
          activeId = remaining.reduce((best, cid) => {
            const bestConvo = next[best];
            const candidate = next[cid];
            if (!bestConvo || !candidate) return best;
            return candidate.updatedAt > bestConvo.updatedAt ? cid : best;
          }, firstId);
        } else {
          // No conversations left — create a fresh one
          const fresh = makeConversation();
          next[fresh.id] = fresh;
          activeId = fresh.id;
        }
      }

      return { conversations: next, activeConversationId: activeId };
    });
  },

  switchConversation(id) {
    const { conversations } = get();
    if (id in conversations) {
      set({ activeConversationId: id });
    }
  },

  renameConversation(id, title) {
    set((s) => {
      const convo = s.conversations[id];
      if (!convo) return s;
      return {
        conversations: {
          ...s.conversations,
          [id]: { ...convo, title: title.trim() || 'New chat' },
        },
      };
    });
  },

  async sendMessage(content) {
    if (get().isStreaming) return;

    const state = get();
    const maybeId = state.activeConversationId;

    // If for any reason there's no active conversation, create one
    const conversationId: string =
      maybeId && maybeId in state.conversations
        ? maybeId
        : get().createConversation();

    const userMessage: AppMessage = {
      id: makeId(),
      role: 'user',
      content,
      createdAt: Date.now(),
      status: 'complete',
    };

    // Auto-title from first user message
    const convo = get().conversations[conversationId];
    const isFirstMessage = convo ? convo.messages.length === 0 : true;

    set((s) => {
      const target = s.conversations[conversationId];
      if (!target) return s;
      const updated: AppConversation = {
        ...target,
        title: isFirstMessage ? titleFromContent(content) : target.title,
        messages: [...target.messages, userMessage],
        updatedAt: Date.now(),
      };
      return { conversations: { ...s.conversations, [conversationId]: updated } };
    });

    const assistantId = makeId();
    const assistantPlaceholder: AppMessage = {
      id: assistantId,
      role: 'assistant',
      content: '',
      createdAt: Date.now(),
      status: 'streaming',
    };

    const controller = new AbortController();

    set((s) => {
      const target = s.conversations[conversationId];
      if (!target) return s;
      return {
        isStreaming: true,
        streamingMessageId: assistantId,
        abortController: controller,
        conversations: {
          ...s.conversations,
          [conversationId]: {
            ...target,
            messages: [...target.messages, assistantPlaceholder],
            updatedAt: Date.now(),
          },
        },
      };
    });

    // Build the message history to send (all messages including the new user msg,
    // excluding the empty assistant placeholder)
    const currentConvo = get().conversations[conversationId];
    const apiMessages: ChatMessage[] = (currentConvo?.messages ?? [])
      .filter((m) => m.id !== assistantId)
      .map((m): ChatMessage => ({ id: m.id, role: m.role, content: m.content, createdAt: m.createdAt }));

    try {
      // Branch on network mode. Coordinator path is single-turn — only
      // the just-sent user message is forwarded — because the
      // JobAssignedMessage schema has no history slot today, and
      // adding one without a thoughtful design is a privacy hazard.
      const { networkMode, networkModelSlug } = get();
      const response = networkMode
        ? await localApi.streamNetworkJob(content, {
            modelSlug: networkModelSlug,
            abortSignal: controller.signal,
          })
        : await localApi.streamChat(apiMessages, {
            abortSignal: controller.signal,
          });

      if (!response.ok) {
        throw new Error(`[localApi] ${response.status}: ${response.statusText}`);
      }

      for await (const token of streamTokens(response)) {
        // Check abort between tokens
        if (controller.signal.aborted) break;

        set((s) => {
          const target = s.conversations[conversationId];
          if (!target) return s;
          return {
            conversations: {
              ...s.conversations,
              [conversationId]: {
                ...target,
                messages: target.messages.map((m) =>
                  m.id === assistantId
                    ? { ...m, content: m.content + token.content }
                    : m,
                ),
                updatedAt: Date.now(),
              },
            },
          };
        });
      }

      // Mark complete unless aborted
      if (!controller.signal.aborted) {
        set((s) => {
          const target = s.conversations[conversationId];
          if (!target) return s;
          return {
            isStreaming: false,
            streamingMessageId: null,
            abortController: null,
            conversations: {
              ...s.conversations,
              [conversationId]: {
                ...target,
                messages: target.messages.map((m) =>
                  m.id === assistantId ? { ...m, status: 'complete' as MessageStatus } : m,
                ),
              },
            },
          };
        });
      }
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        // abort() already handled state cleanup — nothing to do here
        return;
      }

      // Surface error inline as a message
      const errorText =
        err instanceof Error ? err.message : 'An unexpected error occurred.';

      set((s) => {
        const target = s.conversations[conversationId];
        if (!target) return s;
        return {
          isStreaming: false,
          streamingMessageId: null,
          abortController: null,
          conversations: {
            ...s.conversations,
            [conversationId]: {
              ...target,
              messages: target.messages.map((m) =>
                m.id === assistantId
                  ? {
                      ...m,
                      status: 'error' as MessageStatus,
                      errorText,
                      content: m.content,
                    }
                  : m,
              ),
            },
          },
        };
      });
    }
  },

  abort() {
    const { abortController, streamingMessageId, activeConversationId } = get();
    abortController?.abort();

    set((s) => {
      const target = activeConversationId ? s.conversations[activeConversationId] : null;
      if (!target || !streamingMessageId || !activeConversationId) {
        return { isStreaming: false, streamingMessageId: null, abortController: null };
      }
      return {
        isStreaming: false,
        streamingMessageId: null,
        abortController: null,
        conversations: {
          ...s.conversations,
          [activeConversationId]: {
            ...target,
            messages: target.messages.map((m) =>
              m.id === streamingMessageId
                ? { ...m, status: 'partial' as MessageStatus }
                : m,
            ),
          },
        },
      };
    });
  },

  clearConversations() {
    const fresh = makeConversation();
    set({
      conversations: { [fresh.id]: fresh },
      activeConversationId: fresh.id,
      isStreaming: false,
      streamingMessageId: null,
      abortController: null,
    });
  },

  setNetworkMode(on) {
    set({ networkMode: on });
  },

  setNetworkModelSlug(slug) {
    set({ networkModelSlug: slug });
  },
}));

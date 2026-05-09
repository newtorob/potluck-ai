/**
 * Chat store — conversations, streaming state, and send/abort actions.
 *
 * Local-first persistence: conversations + messages live in the sidecar's
 * SQLite (memory_db). Local component state mirrors what's on disk and
 * doubles as the UI source-of-truth during a session. API failures are
 * caught + logged but never block the UI; the chat still works fully
 * in-memory if the sidecar is unreachable, the user just loses history.
 *
 * Hydration: `hydrate()` is called once from App.tsx on mount. It loads
 * the conversation list (summaries only) and restores the most recent
 * (or the previously-active id from localStorage). Messages are lazy-
 * loaded on `switchConversation()`.
 *
 * The `sendMessage` action:
 *   1. Lazily creates a conversation if none active.
 *   2. Appends user message + persists immediately.
 *   3. Creates an empty assistant placeholder (local-only until done).
 *   4. Opens the SSE stream via localApi.streamChat or streamNetworkJob.
 *   5. Appends tokens as they arrive (memory-only during stream).
 *   6. On stream completion: persists the assistant message in one shot
 *      with all meta (served_by / output_tokens / duration_ms).
 *   7. Marks partial on abort, error on failure (each persisted too).
 */

import { create } from 'zustand';
import type { ChatMessage, Conversation } from '@/types/chat';
import { localApi } from '@/lib/localApi';
import type { PersistedMessage } from '@/lib/localApi';
import { streamTokens } from '@/lib/streaming';

const ACTIVE_CONVO_KEY = 'potluck.active_conversation_id';

// ---------------------------------------------------------------------------
// Extended message type with streaming metadata
// ---------------------------------------------------------------------------

export type MessageStatus = 'complete' | 'streaming' | 'partial' | 'error';

/** Routing + completion metadata for assistant messages. Set by the
 *  streaming layer when the SSE stream emits a `meta` envelope (only
 *  the coordinator's /jobs/stream does this; local /chat/completions
 *  doesn't, so meta stays undefined for local messages). Surfaced by
 *  MessageBubble as a small footer ("Served by a verified contributor
 *  ... duration / token count") under network-mode responses. */
export interface MessageMeta {
  served_by: 'contributor' | 'local';
  output_tokens?: number;
  duration_ms?: number;
  finish_reason?: string;
}

export interface AppMessage extends ChatMessage {
  status: MessageStatus;
  /** Set on assistant messages when the SSE stream emitted a meta
   *  envelope. Always undefined for user messages and for assistant
   *  messages that came from the local sidecar. */
  meta?: MessageMeta;
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
  /** Conversation ids whose messages are loaded into local state. List
   *  hydration loads summaries only; full messages come on switchConversation. */
  hydratedIds: Set<string>;
  /** True after hydrate() completes (success OR failure). UI uses this
   *  to distinguish "still loading" from "loaded with zero conversations." */
  isHydrated: boolean;
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
  hydrate: () => Promise<void>;
  createConversation: (modelSlug?: string) => Promise<string>;
  deleteConversation: (id: string) => Promise<void>;
  switchConversation: (id: string) => Promise<void>;
  renameConversation: (id: string, title: string) => Promise<void>;
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

/** Map a PersistedMessage from the API into the local AppMessage shape.
 *  Includes the meta envelope reconstruction for assistant messages
 *  that came via the contributor network. */
function persistedToAppMessage(p: PersistedMessage): AppMessage {
  const msg: AppMessage = {
    id: p.id,
    role: p.role,
    content: p.content,
    status: p.status,
    createdAt: new Date(p.created_at).getTime(),
  };
  if (p.served_by !== null && p.role === 'assistant') {
    msg.meta = {
      served_by: p.served_by,
      output_tokens: p.output_tokens ?? undefined,
      duration_ms: p.duration_ms ?? undefined,
      finish_reason: p.finish_reason ?? undefined,
    };
  }
  return msg;
}

/** Best-effort fire-and-forget — wraps a Promise so a sidecar outage
 *  doesn't kill the chat UX. We log the error and move on; in-memory
 *  state remains the user's source of truth for the session. */
function persist<T>(p: Promise<T>, label: string): void {
  void p.catch((err) => {
    console.warn(`[chatStore] persist:${label} failed`, err);
  });
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useChatStore = create<ChatState>()((set, get) => ({
  conversations: {},
  hydratedIds: new Set(),
  isHydrated: false,
  activeConversationId: null,
  isStreaming: false,
  streamingMessageId: null,
  abortController: null,
  networkMode: false,
  // Default slug — qwen2.5:3b-instruct-q4_K_M is what both the Linux
  // media server and the Windows gaming PC advertise via Ollama.
  // Configurable via the model picker in /network (task 776cf3a7).
  networkModelSlug: 'qwen2.5:3b-instruct-q4_K_M',

  async hydrate() {
    try {
      const persisted = await localApi.history.list({ include_all: true, limit: 200 });
      const conversations: Record<string, AppConversation> = {};
      for (const p of persisted) {
        conversations[p.id] = {
          id: p.id,
          title: p.title,
          modelSlug: null,
          messages: [],
          createdAt: new Date(p.created_at).getTime(),
          updatedAt: new Date(p.updated_at).getTime(),
        };
      }

      let activeId: string | null = null;
      const savedActiveId = typeof window !== 'undefined'
        ? window.localStorage.getItem(ACTIVE_CONVO_KEY)
        : null;
      if (savedActiveId !== null && savedActiveId in conversations) {
        activeId = savedActiveId;
      } else if (persisted.length > 0) {
        // Most-recently-updated wins (the API already returns this order).
        activeId = persisted[0]?.id ?? null;
      }

      set({ conversations, activeConversationId: activeId, isHydrated: true });

      // Lazy-load messages for the now-active conversation so it's ready
      // when the chat view first renders.
      if (activeId !== null) {
        await get().switchConversation(activeId);
      }
    } catch (err) {
      console.warn('[chatStore] hydrate failed; running in memory-only mode', err);
      set({ isHydrated: true });
    }
  },

  async createConversation(modelSlug) {
    const id = makeId();
    const now = Date.now();
    const optimistic: AppConversation = {
      id,
      title: 'New chat',
      modelSlug: modelSlug ?? null,
      messages: [],
      createdAt: now,
      updatedAt: now,
    };

    // Optimistic local insert — the UI reflects the new chat immediately.
    set((s) => ({
      conversations: { ...s.conversations, [id]: optimistic },
      activeConversationId: id,
      hydratedIds: new Set([...s.hydratedIds, id]),
    }));
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(ACTIVE_CONVO_KEY, id);
    }

    // Persist. Failure leaves the convo in memory-only mode (won't survive
    // restart) but the user can still chat in the current session.
    persist(
      localApi.history.create({
        id,
        title: 'New chat',
        network_mode: get().networkMode,
        network_model_slug: get().networkMode ? get().networkModelSlug : null,
      }),
      'create',
    );

    return id;
  },

  async deleteConversation(id) {
    set((s) => {
      const next = { ...s.conversations };
      delete next[id];
      const nextHydrated = new Set(s.hydratedIds);
      nextHydrated.delete(id);

      const remaining = Object.keys(next);
      let activeId = s.activeConversationId;

      if (activeId === id) {
        const firstId = remaining[0];
        if (firstId !== undefined) {
          activeId = remaining.reduce((best, cid) => {
            const bestConvo = next[best];
            const candidate = next[cid];
            if (!bestConvo || !candidate) return best;
            return candidate.updatedAt > bestConvo.updatedAt ? cid : best;
          }, firstId);
        } else {
          activeId = null;
        }
        if (typeof window !== 'undefined') {
          if (activeId === null) {
            window.localStorage.removeItem(ACTIVE_CONVO_KEY);
          } else {
            window.localStorage.setItem(ACTIVE_CONVO_KEY, activeId);
          }
        }
      }

      return { conversations: next, activeConversationId: activeId, hydratedIds: nextHydrated };
    });

    // If the new active needs hydration, lazy-load.
    const newActive = get().activeConversationId;
    if (newActive !== null && !get().hydratedIds.has(newActive)) {
      void get().switchConversation(newActive);
    }

    persist(localApi.history.delete(id), 'delete');
  },

  async switchConversation(id) {
    const { conversations, hydratedIds } = get();
    if (!(id in conversations)) return;
    set({ activeConversationId: id });
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(ACTIVE_CONVO_KEY, id);
    }

    // Lazy-load messages if not already cached.
    if (!hydratedIds.has(id)) {
      try {
        const detail = await localApi.history.get(id);
        const messages = detail.messages.map(persistedToAppMessage);
        set((s) => {
          const target = s.conversations[id];
          if (!target) return s;
          const nextHydrated = new Set(s.hydratedIds);
          nextHydrated.add(id);
          return {
            conversations: {
              ...s.conversations,
              [id]: { ...target, messages, title: detail.title },
            },
            hydratedIds: nextHydrated,
          };
        });
      } catch (err) {
        console.warn('[chatStore] failed to load conversation messages', err);
      }
    }
  },

  async renameConversation(id, title) {
    const trimmed = title.trim() || 'New chat';
    set((s) => {
      const convo = s.conversations[id];
      if (!convo) return s;
      return {
        conversations: { ...s.conversations, [id]: { ...convo, title: trimmed } },
      };
    });
    persist(localApi.history.update(id, { title: trimmed }), 'rename');
  },

  async sendMessage(content) {
    if (get().isStreaming) return;

    const state = get();
    const maybeId = state.activeConversationId;

    // If for any reason there's no active conversation, create one
    const conversationId: string =
      maybeId && maybeId in state.conversations
        ? maybeId
        : await get().createConversation();

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
    const newTitle = isFirstMessage ? titleFromContent(content) : null;

    set((s) => {
      const target = s.conversations[conversationId];
      if (!target) return s;
      const updated: AppConversation = {
        ...target,
        title: newTitle ?? target.title,
        messages: [...target.messages, userMessage],
        updatedAt: Date.now(),
      };
      return { conversations: { ...s.conversations, [conversationId]: updated } };
    });

    // Persist user message immediately. The first-message title rename
    // piggybacks here as a separate update — small extra round-trip but
    // keeps the API surface minimal (no compound endpoint).
    persist(
      localApi.history.appendMessage(conversationId, {
        id: userMessage.id,
        role: 'user',
        content,
        status: 'complete',
      }),
      'append:user',
    );
    if (newTitle !== null) {
      persist(localApi.history.update(conversationId, { title: newTitle }), 'rename:auto');
    }

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

      for await (const event of streamTokens(response)) {
        // Check abort between events
        if (controller.signal.aborted) break;

        if (event.type === 'meta') {
          // Coordinator-only — attach to the in-flight assistant message
          // so MessageBubble can render the "served by" footer when the
          // stream finishes. No-op for local-sidecar streams.
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
                      ? {
                          ...m,
                          meta: {
                            served_by: event.served_by,
                            output_tokens: event.output_tokens,
                            duration_ms: event.duration_ms,
                            finish_reason: event.finish_reason,
                          },
                        }
                      : m,
                  ),
                },
              },
            };
          });
          continue;
        }

        // event.type === 'token'
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
                    ? { ...m, content: m.content + event.content }
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

        // Persist the now-finished assistant message in one shot. Read
        // back from state so we capture the final content and any meta
        // envelope (network mode emits served_by / token count / duration).
        const finalConvo = get().conversations[conversationId];
        const finalMsg = finalConvo?.messages.find((m) => m.id === assistantId);
        if (finalMsg) {
          persist(
            localApi.history.appendMessage(conversationId, {
              id: assistantId,
              role: 'assistant',
              content: finalMsg.content,
              status: 'complete',
              served_by: finalMsg.meta?.served_by ?? null,
              output_tokens: finalMsg.meta?.output_tokens ?? null,
              duration_ms: finalMsg.meta?.duration_ms ?? null,
              finish_reason: finalMsg.meta?.finish_reason ?? null,
            }),
            'append:assistant',
          );
        }
      }
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        // abort() handled state + persistence — nothing to do here.
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

      // Persist the failed assistant message so history reflects what
      // actually happened. Status='error' isn't a valid persisted status
      // (the schema accepts complete|streaming|partial), so we stamp it
      // as 'partial' on disk and embed the error in content. UI keeps
      // the rich error styling because it reads from in-memory state
      // for the active session.
      const errConvo = get().conversations[conversationId];
      const errMsg = errConvo?.messages.find((m) => m.id === assistantId);
      if (errMsg) {
        persist(
          localApi.history.appendMessage(conversationId, {
            id: assistantId,
            role: 'assistant',
            content: errMsg.content || `[error] ${errorText}`,
            status: 'partial',
          }),
          'append:assistant-error',
        );
      }
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

    // Persist the partial assistant message so history reflects what
    // the user saw before aborting. If the user aborted before any
    // tokens arrived, we still persist an empty 'partial' so the
    // exchange isn't lost from history.
    if (activeConversationId !== null && streamingMessageId !== null) {
      const convo = get().conversations[activeConversationId];
      const msg = convo?.messages.find((m) => m.id === streamingMessageId);
      if (msg) {
        persist(
          localApi.history.appendMessage(activeConversationId, {
            id: streamingMessageId,
            role: 'assistant',
            content: msg.content,
            status: 'partial',
            served_by: msg.meta?.served_by ?? null,
            output_tokens: msg.meta?.output_tokens ?? null,
            duration_ms: msg.meta?.duration_ms ?? null,
            finish_reason: msg.meta?.finish_reason ?? null,
          }),
          'append:assistant-partial',
        );
      }
    }
  },

  clearConversations() {
    // State-only reset — does NOT delete persisted conversations from disk.
    // Use deleteConversation() per-id for that. This action exists for
    // tests and future "panic clear" UI; not currently bound to any view.
    set({
      conversations: {},
      hydratedIds: new Set(),
      activeConversationId: null,
      isStreaming: false,
      streamingMessageId: null,
      abortController: null,
    });
    if (typeof window !== 'undefined') {
      window.localStorage.removeItem(ACTIVE_CONVO_KEY);
    }
  },

  setNetworkMode(on) {
    set({ networkMode: on });
  },

  setNetworkModelSlug(slug) {
    set({ networkModelSlug: slug });
  },
}));

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
import { streamHouseholdInference } from '@/lib/householdInference';
import type { RoutingMode } from '@/lib/householdInference';
import { useMeshStore } from '@/stores/meshStore';
import { persistUiPref } from '@/lib/sidecarPersist';

const ROUTING_MODE_KEY = 'potluck.household_routing_mode';

// ---------------------------------------------------------------------------
// RoutingScope — the four-tier trust gradient
// ---------------------------------------------------------------------------

/** The user-facing routing tier. This is the primary setting; networkMode /
 *  householdMode are derived from it for backward-compat with existing
 *  readers (StatusBar, NetworkView, etc.).
 *
 *  local       → local sidecar, nothing leaves this machine.
 *  household   → direct to a paired peer over the WireGuard mesh.
 *  circle      → reserved for Phase 2 trusted-circle membership infra.
 *                Selectable in the UI but NOT wired; falls back to local.
 *  cooperative → coordinator blinded-dispatch to the open pool.
 */
export type RoutingScope = 'local' | 'household' | 'circle' | 'cooperative';

const ROUTING_SCOPE_KEY = 'potluck.routing_scope';

function readBootstrapRoutingScope(): RoutingScope {
  if (typeof window === 'undefined') return 'local';
  const v = window.localStorage.getItem(ROUTING_SCOPE_KEY);
  if (v === 'local' || v === 'household' || v === 'circle' || v === 'cooperative') return v;
  // Migrate from the old boolean pair: if networkMode was on, map to cooperative;
  // if householdMode was on, map to household; otherwise local.
  // (These keys are no longer written, so this handles first-run-after-upgrade.)
  return 'local';
}

function readBootstrapRoutingMode(): RoutingMode {
  if (typeof window === 'undefined') return 'capability';
  const v = window.localStorage.getItem(ROUTING_MODE_KEY);
  return v === 'round_robin' ? 'round_robin' : 'capability';
}

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
  // 'local'       — this machine's sidecar.
  // 'household'   — one of YOUR machines, direct over the WireGuard mesh.
  // 'contributor' — through the coordinator (open pool, or your own machine
  //                 when served_scope === 'mine').
  served_by: 'contributor' | 'local' | 'household';
  /** Coordinator routing tier for the network path: 'mine' = one of your own
   *  machines, 'pool' = an open-pool peer. Session-only (not persisted). */
  served_scope?: 'mine' | 'pool';
  /** Name of the machine that served a 'household' response. Session-only. */
  served_peer?: string;
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

  // ---- Four-tier routing scope (Phase 1 / Phase 2) ---- //
  // Primary setting — persisted to localStorage. The legacy networkMode /
  // householdMode fields below are kept for backward-compat with existing
  // readers (StatusBar, NetworkView, HouseholdView …) and are always
  // consistent with routingScope via the setRoutingScope setter.
  routingScope: RoutingScope;

  // ---- Circle routing (Phase 2) ---- //
  // When routingScope === 'circle', this is the circle id passed to
  // streamNetworkJob. Null means no circle selected yet (the UI must gate
  // the send button on this being set when the scope is 'circle').
  activeCircleId: string | null;

  // ---- Phase G m3 #19: contributor-network routing ---- //
  // Derived from routingScope === 'cooperative'. Kept as explicit state so
  // all existing readers (StatusBar etc.) work without changes.
  networkMode: boolean;
  networkModelSlug: string;

  // ---- Household routing (post-mesh-launch addition) ---- //
  // Derived from routingScope === 'household'. Mutually exclusive with
  // networkMode; setRoutingScope enforces consistency.
  householdMode: boolean;

  // How to pick which household peer serves the request. 'capability'
  // (default) scores peers by hardware + load + model fit. 'round_robin'
  // rotates evenly across eligible peers — useful when the user wants
  // to keep multiple machines warm rather than concentrate load on the
  // best one. Persisted to localStorage; per-machine setting.
  routingMode: RoutingMode;

  // Actions
  hydrate: () => Promise<void>;
  createConversation: (modelSlug?: string) => Promise<string>;
  deleteConversation: (id: string) => Promise<void>;
  switchConversation: (id: string) => Promise<void>;
  renameConversation: (id: string, title: string) => Promise<void>;
  sendMessage: (content: string) => Promise<void>;
  abort: () => void;
  clearConversations: () => void;
  /** Primary setter — persists scope and keeps networkMode/householdMode in sync. */
  setRoutingScope: (scope: RoutingScope) => void;
  /** Phase 2: set the active circle for the 'circle' routing scope. */
  setActiveCircleId: (id: string | null) => void;
  /** Legacy setters — kept for backward-compat; delegate to setRoutingScope. */
  setNetworkMode: (on: boolean) => void;
  setNetworkModelSlug: (slug: string) => void;
  setHouseholdMode: (on: boolean) => void;
  setRoutingMode: (mode: RoutingMode) => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeId(): string {
  return crypto.randomUUID();
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
    const meta: NonNullable<AppMessage['meta']> = { served_by: p.served_by };
    if (p.output_tokens !== null && p.output_tokens !== undefined) meta.output_tokens = p.output_tokens;
    if (p.duration_ms !== null && p.duration_ms !== undefined) meta.duration_ms = p.duration_ms;
    if (p.finish_reason !== null && p.finish_reason !== undefined) meta.finish_reason = p.finish_reason;
    msg.meta = meta;
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

export const useChatStore = create<ChatState>()((set, get) => {
  // Derive the initial boolean pair from the persisted scope so all existing
  // readers see consistent values from the very first render.
  const _initScope = readBootstrapRoutingScope();
  const _initNetworkMode = _initScope === 'cooperative';
  const _initHouseholdMode = _initScope === 'household';

  return {
  conversations: {},
  hydratedIds: new Set(),
  isHydrated: false,
  activeConversationId: null,
  isStreaming: false,
  streamingMessageId: null,
  abortController: null,
  routingScope: _initScope,
  activeCircleId: null,
  networkMode: _initNetworkMode,
  // Default slug — qwen2.5:3b-instruct-q4_K_M is what both the Linux
  // media server and the Windows gaming PC advertise via Ollama.
  // Configurable via the model picker in /network (task 776cf3a7).
  networkModelSlug: 'qwen2.5:3b-instruct-q4_K_M',
  householdMode: _initHouseholdMode,
  routingMode: readBootstrapRoutingMode(),

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

  // eslint-disable-next-line @typescript-eslint/require-await -- async by the store's uniform action contract; body is sync state + fire-and-forget persist
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

  // eslint-disable-next-line @typescript-eslint/require-await -- async by the store's uniform action contract; body is sync state + fire-and-forget persist
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

  // eslint-disable-next-line @typescript-eslint/require-await -- async by the store's uniform action contract; body is sync state + fire-and-forget persist
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
      // Branch on routing scope. Four tiers, three active dispatch paths:
      //   household   — direct to a household peer over the mesh
      //                 (full history forwarded; trusted machines we own)
      //   cooperative — through the coordinator to an open-pool peer
      //                 (single-turn only — JobAssignedMessage has no history slot
      //                 and bolting one on is a privacy-design exercise)
      //   circle      — NOT wired in Phase 1; falls back to local with a
      //                 'circle_unavailable' signal on the message. Must never
      //                 silently route to cooperative or household.
      //   local       — this machine's Python sidecar (default)
      const { routingScope, networkModelSlug, activeCircleId } = get();
      let response: Response;
      // Name of the household machine that served this turn (My Machines path),
      // captured so MessageBubble can render "Ran on <your machine>".
      let householdPeerName: string | null = null;
      if (routingScope === 'household') {
        const peers = useMeshStore.getState().peers;
        const { response: r, peer } = await streamHouseholdInference(apiMessages, peers, {
          abortSignal: controller.signal,
          routingMode: get().routingMode,
        });
        response = r;
        householdPeerName = peer.node_name;
      } else if (routingScope === 'circle' && activeCircleId !== null) {
        // Phase 2: route through the coordinator but restrict to the chosen circle.
        response = await localApi.streamNetworkJob(content, {
          modelSlug: networkModelSlug,
          abortSignal: controller.signal,
          circleId: activeCircleId,
        });
      } else if (routingScope === 'cooperative') {
        response = await localApi.streamNetworkJob(content, {
          modelSlug: networkModelSlug,
          abortSignal: controller.signal,
        });
      } else {
        // 'local', and 'circle' without a selected circle, both use the local
        // sidecar. The UI gates send when 'circle' has no activeCircleId.
        response = await localApi.streamChat(apiMessages, {
          abortSignal: controller.signal,
        });
      }

      if (!response.ok) {
        // Surface the response body's `detail` / `error` when present so the
        // user sees the actionable reason (e.g. coordinator's "No online peer
        // serves model 'X'") instead of a generic "Service Unavailable."
        let detail: string | undefined;
        try {
          const body = (await response.clone().json()) as { detail?: unknown; error?: unknown };
          if (typeof body.detail === 'string') detail = body.detail;
          else if (typeof body.error === 'string') detail = body.error;
        } catch {
          // Body wasn't JSON — fall through to statusText.
        }
        throw new Error(
          detail
            ? `[localApi] ${response.status}: ${detail}`
            : `[localApi] ${response.status}: ${response.statusText}`,
        );
      }

      // The household path has no SSE meta envelope (the peer streams plain
      // OpenAI deltas), so attach the routing meta ourselves — this is what
      // renders "Ran on <your machine>" under the response.
      if (householdPeerName) {
        const peerName = householdPeerName;
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
                    ? { ...m, meta: { served_by: 'household' as const, served_peer: peerName } }
                    : m,
                ),
              },
            },
          };
        });
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
                  messages: target.messages.map((m) => {
                    if (m.id !== assistantId) return m;
                    const meta: NonNullable<AppMessage['meta']> = { served_by: event.served_by };
                    if (event.served_scope !== undefined) meta.served_scope = event.served_scope;
                    if (event.output_tokens !== undefined) meta.output_tokens = event.output_tokens;
                    if (event.duration_ms !== undefined) meta.duration_ms = event.duration_ms;
                    if (event.finish_reason !== undefined) meta.finish_reason = event.finish_reason;
                    return { ...m, meta };
                  }),
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
                  m.id === assistantId ? { ...m, status: 'complete' } : m,
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
              // 'household' is session-only — persist only the coordinator-known
              // values so the history column stays 'contributor' | 'local' | null.
              served_by:
                finalMsg.meta?.served_by === 'contributor' || finalMsg.meta?.served_by === 'local'
                  ? finalMsg.meta.served_by
                  : null,
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
                      status: 'error',
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
                ? { ...m, status: 'partial' }
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

  setRoutingScope(scope) {
    // Persist the chosen scope and keep the legacy boolean pair in sync so
    // all existing readers (StatusBar, NetworkView, etc.) stay correct.
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(ROUTING_SCOPE_KEY, scope);
    }
    set({
      routingScope: scope,
      networkMode: scope === 'cooperative',
      householdMode: scope === 'household',
    });
    // Write-through to sidecar durable config so the scope survives
    // localStorage wipes, reinstalls, and dev-vs-packaged launches.
    persistUiPref(ROUTING_SCOPE_KEY, scope);
  },

  setActiveCircleId(id) {
    set({ activeCircleId: id });
  },

  setNetworkMode(on) {
    // Legacy compat — delegates to setRoutingScope so the scope field stays
    // the canonical source of truth.
    get().setRoutingScope(on ? 'cooperative' : 'local');
  },

  setHouseholdMode(on) {
    get().setRoutingScope(on ? 'household' : 'local');
  },

  setRoutingMode(mode) {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(ROUTING_MODE_KEY, mode);
    }
    set({ routingMode: mode });
    persistUiPref(ROUTING_MODE_KEY, mode);
  },

  setNetworkModelSlug(slug) {
    set({ networkModelSlug: slug });
  },
  };
});

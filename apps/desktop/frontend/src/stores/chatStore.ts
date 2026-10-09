import { getConnectedPeers } from '@/lib/meshDiagnostics';
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
 *   2. Awaits the conversation create (optimistic UI still returns immediately
 *      from createConversation; this gate prevents FK races on append).
 *   3. Appends user message + persists immediately.
 *   4. Creates an empty assistant placeholder (local-only until done).
 *   5. Opens the SSE stream via localApi.streamChat or streamNetworkJob.
 *   6. Appends tokens as they arrive (memory-only during stream).
 *   7. On stream completion: persists the assistant message in one shot
 *      with all meta (served_by / output_tokens / duration_ms).
 *   8. Marks partial on abort, error on failure (each persisted too).
 */

import { create } from 'zustand';
import type { ChatMessage, Conversation } from '@/types/chat';
import { localApi } from '@/lib/localApi';
import type { UsageScope } from '@/lib/localApi';
import type { PersistedMessage } from '@/lib/localApi';
import { streamTokens } from '@/lib/streaming';
import { streamHouseholdInference } from '@/lib/householdInference';
import type { RoutingMode } from '@/lib/householdInference';
import { useMeshStore } from '@/stores/meshStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { persistUiPref } from '@/lib/sidecarPersist';
import { networkRefusalCopy } from '@/lib/networkRefusal';

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
const ACTIVE_CIRCLE_KEY = 'potluck.active_circle_id';

function readBootstrapActiveCircle(): string | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage.getItem(ACTIVE_CIRCLE_KEY) || null;
}

/** The circle a remembered choice resolves to once the member's circles are
 *  known: the remembered one if it still exists, else the only circle, else
 *  none (the picker asks). */
export function reconcileCircleSelection(
  activeId: string | null,
  circles: readonly { id: string }[],
): string | null {
  if (activeId !== null && circles.some((c) => c.id === activeId)) return activeId;
  const only = circles.length === 1 ? circles[0] : undefined;
  return only ? only.id : null;
}

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
   *  machines, 'circle' = a trusted-circle member, 'pool' = an open-pool peer.
   *  Session-only (not persisted). */
  served_scope?: 'mine' | 'circle' | 'pool';
  /** How a circle request reached the answering machine: 'direct' = a link from
   *  this machine's engine, 'relay' = through the coordinator. Session-only. */
  path?: 'direct' | 'relay';
  /** Carrier of a 'direct' link: 'udp' = peer to peer, 'derp' = through the
   *  Potluck relay as encrypted packets. Session-only. */
  transport?: 'udp' | 'derp';
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
  /** Session-only request snapshot. Never written to chat history. */
  recovery?: HouseholdRecovery;
  restartOf?: string;
}

interface HouseholdRecovery {
  messages: ChatMessage[];
  modelSlug: string;
  excludedPeerIds: string[];
  routingMode: RoutingMode;
  historyAllowed: boolean;
}

export interface AppConversation extends Omit<Conversation, 'messages'> {
  modelSlug: string | null;
  messages: AppMessage[];
  /** False when this conversation exists only in memory because chat history
   *  was off when it was created. Hydrated and normally-created conversations
   *  are true. The Sidebar surfaces the false case as a "Not saved" marker. */
  persisted?: boolean;
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
  sendMessage: (content: string, restartMessageId?: string) => Promise<void>;
  restartMessage: (messageId: string) => Promise<void>;
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

/** What the coordinator reported when a contributor's turn failed. Recorded
 *  from the SSE `error` envelope so the terminal decision below can tell a
 *  failed reply from a finished one. */
interface StreamFailure {
  message: string;
  /** Only the coordinator sends one, and only for its own closed set. Never
   *  used to decide whether the text is trustworthy: the producer controls
   *  whether it is present at all. */
  category?: string;
}

/** Fixed copy per error category, from the coordinator or from the local
 *  engine's /chat/circle route (the model_mismatch to coordinator_unreachable
 *  rows). inference_error has no row on purpose: the generic line is right.
 *
 *  A Map, not an object literal: the category is attacker-influenced, and an
 *  object lookup for "__proto__", "constructor" or "toString" returns an
 *  inherited value instead of undefined, so `?? fallback` never fires and a
 *  non-string lands in errorText. */
const CONTRIBUTOR_ERROR_COPY = new Map<string, string>([
  ['timeout', 'The contributor timed out before finishing.'],
  ['peer_disconnected', 'The contributor disconnected before finishing.'],
  ['oom', 'The contributor ran out of memory.'],
  ['model_not_loaded', 'The contributor did not have this model loaded.'],
  ['declined', 'The contributor declined this request.'],
  ['internal', 'The contributor hit an internal error.'],
  ['model_mismatch', 'The contributor no longer has this model loaded.'],
  ['link_ended', 'The link to the contributor ended before the reply finished.'],
  ['stream_protocol_error', 'The contributor sent a reply this app could not read.'],
  ['stream_interrupted', 'The connection to the contributor ended before the reply finished.'],
  ['stream_timeout', 'The contributor stopped answering.'],
  ['peer_unreachable', 'The connection to the contributor failed.'],
  ['coordinator_unreachable', 'Could not reach the Potluck coordinator.'],
]);

const UNKNOWN_CONTRIBUTOR_ERROR = 'The contributor could not finish this reply.';
const LOCAL_STREAM_ERROR_PREFIX = 'The reply stopped before it finished.';

/**
 * Copy for a message that died mid-stream.
 *
 * Whether the producer's text can be shown is decided by the routing scope the
 * client chose for this turn, not by anything on the wire. A remote producer
 * controls its own envelope: whether a `category` is there, what it says, and
 * whether the error is a bare string. Keying trust on any of that lets a
 * contributor put its own words on the user's screen just by omitting a field.
 *
 *   local / household  → the user's own machines; show the real message.
 *   circle/cooperative → someone else's machine; show only copy we wrote,
 *                        selected by category, whatever shape the error had.
 */
function streamFailureText(failure: StreamFailure, scope: RoutingScope): string {
  const ownMachines = scope === 'local' || scope === 'household';
  if (!ownMachines) {
    const copy =
      failure.category === undefined ? undefined : CONTRIBUTOR_ERROR_COPY.get(failure.category);
    return typeof copy === 'string' ? copy : UNKNOWN_CONTRIBUTOR_ERROR;
  }
  const detail = failure.message.trim();
  return detail === '' ? LOCAL_STREAM_ERROR_PREFIX : `${LOCAL_STREAM_ERROR_PREFIX} ${detail}`;
}

/** A stream that ended without the [DONE] sentinel was cut off, not finished. */
const TRUNCATED_STREAM_TEXT = 'The connection closed before the reply finished.';

/** Best-effort fire-and-forget — wraps a Promise so a sidecar outage
 *  doesn't kill the chat UX. We log the error and move on; in-memory
 *  state remains the user's source of truth for the session. */
function persist<T>(p: Promise<T>, label: string): void {
  void p.catch((err) => {
    console.warn(`[chatStore] persist:${label} failed`, err);
  });
}

/** Is the user's chat-history setting on right now?
 *
 *  Fails closed: a null settings object (sidecar unreachable, or the store
 *  has not hydrated yet) counts as off, so no prompt or reply is ever written
 *  to disk on a guess. settingsStore imports only localApi, so reading it
 *  from here introduces no import cycle. */
function historyEnabled(): boolean {
  return useSettingsStore.getState().settings?.privacy.chat_history_enabled ?? false;
}

/** Gate for every write that carries conversation *content* (prompts, replies,
 *  titles). `allowed` is the caller's turn-level snapshot; the fresh
 *  historyEnabled() read catches a flag flipped mid-turn. `run` is a thunk on
 *  purpose: the request must not even be constructed when the gate is closed.
 *
 *  Metadata-only writes (usage counters) and reads/deletes are NOT gated —
 *  turning history off hides nothing that is already on disk and must not
 *  destroy it. */
function persistContent<T>(allowed: boolean, run: () => Promise<T>, label: string): void {
  if (!allowed || !historyEnabled()) return;
  persist(run(), label);
}

/**
 * In-flight conversation creates keyed by id.
 *
 * createConversation() returns the optimistic id immediately so the UI can
 * switch chats without waiting on the network, but message/title persists
 * MUST wait for the matching create — otherwise SQLite rejects the insert
 * with FOREIGN KEY constraint failed (messages.conversation_id → conversations).
 *
 * The promise resolves to true on success, false on failure (so callers can
 * retry create before appending).
 */
const pendingCreates = new Map<string, Promise<boolean>>();

/** Conversation ids known to exist on disk (hydrated or successfully created).
 *  ensureConversationPersisted must NOT re-POST create for these — that
 *  produces UNIQUE constraint failed on every subsequent send. */
const knownPersistedIds = new Set<string>();

/** The chat-history snapshot for the turn currently streaming.
 *
 *  abort() runs outside sendMessage (the UI calls it directly), so it can't
 *  see that turn's local snapshot. Parking it here lets stop honour the same
 *  rule as every other write site: a turn that began with history off never
 *  persists anything, not even the partial the user chose to keep on screen.
 *  Set at the top of sendMessage, cleared when the stream settles. */
let currentTurnHistoryAllowed = false;

async function ensureConversationPersisted(
  conversationId: string,
  getConvo: () =>
    | {
        title: string;
      }
    | undefined,
  networkMode: boolean,
  networkModelSlug: string,
  allowed: boolean,
): Promise<void> {
  // History off: stay memory-only. No row is created and the id is
  // deliberately kept out of knownPersistedIds so a later turn re-evaluates.
  if (!allowed || !historyEnabled()) return;
  if (knownPersistedIds.has(conversationId)) return;

  const pending = pendingCreates.get(conversationId);
  if (pending) {
    const ok = await pending;
    if (ok || knownPersistedIds.has(conversationId)) return;
  }

  // Optimistic id whose create failed (or never started). Retry once.
  const convo = getConvo();
  if (!convo) return;

  try {
    await localApi.history.create({
      id: conversationId,
      title: convo.title,
      network_mode: networkMode,
      network_model_slug: networkMode ? networkModelSlug : null,
    });
    knownPersistedIds.add(conversationId);
  } catch (err) {
    // Idempotent create on the sidecar returns success for an existing id;
    // a hard failure still leaves us memory-only for this session.
    console.warn('[chatStore] ensureConversationPersisted:create failed', err);
  }
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
  activeCircleId: readBootstrapActiveCircle(),
  networkMode: _initNetworkMode,
  // Default slug until the user picks from the models the network advertises
  // (the selectable chip rack in /network). qwen2.5:3b-instruct-q4_K_M is what
  // both the Linux media server and the Windows gaming PC serve via Ollama.
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
          persisted: true,
        };
        knownPersistedIds.add(p.id);
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
    const allowed = historyEnabled();
    const optimistic: AppConversation = {
      id,
      title: 'New chat',
      modelSlug: modelSlug ?? null,
      messages: [],
      createdAt: now,
      updatedAt: now,
      persisted: allowed,
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

    // Persist create, but keep the promise so sendMessage can await it
    // before appending messages (FK: messages → conversations). Skipped
    // entirely when chat history is off: the title is derived from the user's
    // first prompt, so the conversation row is itself content. The chat stays
    // fully usable, it just never reaches disk.
    if (allowed) {
      const createPromise = localApi.history
        .create({
          id,
          title: 'New chat',
          network_mode: get().networkMode,
          network_model_slug: get().networkMode ? get().networkModelSlug : null,
        })
        .then(() => {
          knownPersistedIds.add(id);
          return true;
        })
        .catch((err) => {
          console.warn('[chatStore] persist:create failed', err);
          return false;
        })
        .finally(() => {
          pendingCreates.delete(id);
        });
      pendingCreates.set(id, createPromise);
    }

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

    knownPersistedIds.delete(id);

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
    persistContent(true, () => localApi.history.update(id, { title: trimmed }), 'rename');
  },

  async restartMessage(messageId) {
    await get().sendMessage('', messageId);
  },

  async sendMessage(content, restartMessageId) {
    if (get().isStreaming) return;

    const state = get();
    const previous = restartMessageId && state.activeConversationId
      ? state.conversations[state.activeConversationId]?.messages.find((m) => m.id === restartMessageId)
      : undefined;
    const recovery = previous?.status === 'error' ? previous.recovery : undefined;
    if (restartMessageId && !recovery) return;
    // Keep the user's routing choice for this request across persistence awaits.
    const { networkModelSlug, activeCircleId } = state;
    const routingScope = recovery ? 'household' : state.routingScope;
    const routingMode = recovery?.routingMode ?? state.routingMode;
    let servedModelSlug: string | null = recovery?.modelSlug ?? (routingScope === 'household' ? null : networkModelSlug);
    // Same idea for the chat-history privacy setting: snapshot it for this
    // turn so a flag flipped on mid-stream never retroactively saves a
    // conversation the user started while history was off. Every write also
    // re-reads the live value, so flipping it off mid-stream stops writes.
    const historyAllowed = historyEnabled() && (recovery?.historyAllowed ?? true);
    // Claim this turn before persistence awaits so double-clicking Restart
    // cannot dispatch two replacements for the same interrupted answer.
    const controller = new AbortController();
    set({ isStreaming: true, streamingMessageId: null, abortController: controller });
    currentTurnHistoryAllowed = historyAllowed;
    const maybeId = state.activeConversationId;

    // If for any reason there's no active conversation, create one
    const conversationId: string =
      maybeId && maybeId in state.conversations
        ? maybeId
        : await get().createConversation();
    if (controller.signal.aborted || get().abortController !== controller) return;

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
        messages: recovery ? target.messages : [...target.messages, userMessage],
        updatedAt: Date.now(),
      };
      return { conversations: { ...s.conversations, [conversationId]: updated } };
    });

    // Ensure the conversation row exists before message/title writes.
    // createConversation is optimistic; without this gate, append races
    // ahead and SQLite returns FOREIGN KEY constraint failed.
    await ensureConversationPersisted(
      conversationId,
      () => get().conversations[conversationId],
      get().networkMode,
      get().networkModelSlug,
      historyAllowed,
    );
    if (controller.signal.aborted || get().abortController !== controller) return;

    // The row may have been created just now (history was already on) — keep
    // the sidebar's "Not saved" marker honest either way.
    const rowExists = knownPersistedIds.has(conversationId);
    if (get().conversations[conversationId]?.persisted !== rowExists) {
      set((s) => {
        const target = s.conversations[conversationId];
        if (!target) return s;
        return {
          conversations: {
            ...s.conversations,
            [conversationId]: { ...target, persisted: rowExists },
          },
        };
      });
    }

    // Persist user message immediately. The first-message title rename
    // piggybacks here as a separate update — small extra round-trip but
    // keeps the API surface minimal (no compound endpoint).
    if (!recovery) persistContent(
      historyAllowed,
      () =>
        localApi.history.appendMessage(conversationId, {
          id: userMessage.id,
          role: 'user',
          content,
          status: 'complete',
        }),
      'append:user',
    );
    if (newTitle !== null) {
      persistContent(
        historyAllowed,
        () => localApi.history.update(conversationId, { title: newTitle }),
        'rename:auto',
      );
    }

    const assistantId = makeId();
    const assistantPlaceholder: AppMessage = {
      id: assistantId,
      role: 'assistant',
      content: '',
      createdAt: Date.now(),
      status: 'streaming',
      ...(restartMessageId ? { restartOf: restartMessageId } : {}),
      ...(recovery ? { recovery } : {}),
    };

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
            messages: [
              ...target.messages.map((m) => {
                if (m.id !== restartMessageId) return m;
                const { recovery: _used, ...rest } = m;
                return rest;
              }),
              assistantPlaceholder,
            ],
            updatedAt: Date.now(),
          },
        },
      };
    });

    // Build the message history to send (all messages including the new user msg,
    // excluding the empty assistant placeholder)
    const currentConvo = get().conversations[conversationId];
    const apiMessages: ChatMessage[] = recovery?.messages.map((m) => ({ ...m })) ?? (currentConvo?.messages ?? [])
      .filter((m) => m.id !== assistantId && (m.role !== 'assistant' || m.status === 'complete'))
      .map((m): ChatMessage => ({ id: m.id, role: m.role, content: m.content, createdAt: m.createdAt }));

    // Usage dashboard (metadata only, never content): the NETWORK scopes stream
    // renderer -> coordinator/peer and never touch this machine's sidecar, so
    // the renderer is the only place that can record them. Local turns are
    // recorded server-side in chat.py, so they stay null here to avoid double
    // counting. Computed up front because the failure paths report too.
    const usageScope: UsageScope | null =
      routingScope === 'household'
        ? 'my_machines'
        : routingScope === 'circle' && activeCircleId !== null
          ? 'circle'
          : routingScope === 'cooperative'
            ? 'pool'
            : null;

    try {
      // Branch on routing scope. Four tiers, three active dispatch paths:
      //   household   — direct to a household peer over the mesh
      //                 (full history forwarded; trusted machines we own)
      //   cooperative — through the coordinator to an open-pool peer
      //                 (this desktop path sends only the latest prompt)
      //   circle      — dispatch restricted to the selected circle, through the
      //                 local engine (a direct link, else the coordinator);
      //                 fail if no circle was selected when the user sent.
      //   local       — this machine's Python sidecar (default)
      let response: Response;
      // Name of the household machine that served this turn (My Machines path),
      // captured so MessageBubble can render "Ran on <your machine>".
      let householdPeerName: string | null = null;
      if (routingScope === 'household') {
        const peers = getConnectedPeers(useMeshStore.getState());
        let excludedPeerIds = [...(recovery?.excludedPeerIds ?? [])];
        const { response: r, peer } = await streamHouseholdInference(apiMessages, peers, {
          abortSignal: controller.signal,
          routingMode,
          ...(recovery ? { modelSlug: recovery.modelSlug } : {}),
          excludedPeerIds,
          getPeers: () => getConnectedPeers(useMeshStore.getState()),
          onAttempt: (candidate, modelSlug) => {
            servedModelSlug = modelSlug;
            excludedPeerIds = [...excludedPeerIds, candidate.node_id];
            const snapshot: HouseholdRecovery = {
              messages: apiMessages.map((m) => ({ ...m })), modelSlug,
              excludedPeerIds, routingMode, historyAllowed,
            };
            set((s) => {
              const target = s.conversations[conversationId];
              if (!target || s.abortController !== controller) return s;
              return { conversations: { ...s.conversations, [conversationId]: {
                ...target, messages: target.messages.map((m) => m.id === assistantId
                  ? { ...m, recovery: snapshot } : m),
              } } };
            });
          },
        });
        response = r;
        householdPeerName = peer.node_name;
      } else if (routingScope === 'circle') {
        if (!activeCircleId) {
          throw new Error('Choose a trusted circle before sending.');
        }
        // Restrict to the chosen circle. localApi sends this through the local
        // engine and falls back to the coordinator; it never uses the pool.
        response = await localApi.streamNetworkJob(content, {
          modelSlug: networkModelSlug,
          abortSignal: controller.signal,
          circleId: activeCircleId,
          messages: apiMessages.map((m) => ({ role: m.role, content: m.content })),
        });
      } else if (routingScope === 'cooperative') {
        response = await localApi.streamNetworkJob(content, {
          modelSlug: networkModelSlug,
          abortSignal: controller.signal,
        });
      } else {
        // Local scope uses this machine's sidecar.
        response = await localApi.streamChat(apiMessages, {
          abortSignal: controller.signal,
        });
      }
      if (controller.signal.aborted || get().abortController !== controller) {
        await response.body?.cancel();
        return;
      }

      if (!response.ok) {
        if (routingScope === 'circle' || routingScope === 'cooperative') {
          const copy = networkRefusalCopy(routingScope, response.status, networkModelSlug);
          if (copy !== null) {
            await response.body?.cancel();
            throw new Error(copy);
          }
        }
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

      // How the stream ended, not just that it ended. A normal loop exit means
      // the body ran out, which happens on success, on a dropped connection,
      // and on a producer crash alike. Only the [DONE] sentinel proves the
      // reply is whole.
      let sawDone = false;
      let streamError: StreamFailure | null = null;

      for await (const event of streamTokens(response)) {
        // Check abort between events
        if (controller.signal.aborted || get().abortController !== controller) break;

        if (event.type === 'done') {
          sawDone = true;
          break;
        }

        if (event.type === 'error') {
          // The error is authoritative. The coordinator's timeout branch still
          // sends a meta envelope and [DONE] after this (a producer bug), so we
          // stop reading here rather than let the trailing frames overwrite it.
          streamError =
            event.category === undefined
              ? { message: event.message }
              : { message: event.message, category: event.category };
          break;
        }

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
                    if (event.path !== undefined) meta.path = event.path;
                    if (event.transport !== undefined) meta.transport = event.transport;
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

      // Terminal decision, unless the user stopped it (abort() owns that case).
      //
      //   [DONE], no error  → complete
      //   error envelope    → error, with the streamed tokens kept as-is
      //   neither           → error, the connection was cut before the end
      if (!controller.signal.aborted) {
        const terminalErrorText: string | null =
          streamError !== null
            ? streamFailureText(streamError, routingScope)
            : sawDone
              ? null
              : TRUNCATED_STREAM_TEXT;
        const terminalStatus: MessageStatus = terminalErrorText === null ? 'complete' : 'error';

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
                messages: target.messages.map((m) => {
                  if (m.id !== assistantId) return m;
                  // Content is never rewritten: whatever streamed in stays on
                  // screen, and the error line sits alongside it.
                  if (terminalErrorText === null) {
                    const { recovery: _finished, ...rest } = m;
                    return { ...rest, status: terminalStatus };
                  }
                  return { ...m, status: terminalStatus, errorText: terminalErrorText };
                }),
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
          // A failed turn is persisted as what it was: the partial text plus
          // an 'error' status, which localApi, the sidecar and the schema all
          // accept. The content is left exactly as it streamed.
          const persistedFinishReason: string | null =
            terminalErrorText === null
              ? (finalMsg.meta?.finish_reason ?? null)
              : streamError !== null
                ? 'error'
                : 'truncated';

          persistContent(
            historyAllowed,
            () =>
              localApi.history.appendMessage(conversationId, {
                id: assistantId,
                role: 'assistant',
                content: finalMsg.content,
                status: terminalStatus,
                // 'household' is session-only — persist only the coordinator-known
                // values so the history column stays 'contributor' | 'local' | null.
                served_by:
                  finalMsg.meta?.served_by === 'contributor' || finalMsg.meta?.served_by === 'local'
                    ? finalMsg.meta.served_by
                    : null,
                output_tokens: finalMsg.meta?.output_tokens ?? null,
                duration_ms: finalMsg.meta?.duration_ms ?? null,
                finish_reason: persistedFinishReason,
              }),
            'append:assistant',
          );

          // Fire-and-forget: a telemetry write must never break chat. A failed
          // turn is recorded too, otherwise the dashboard reports a success
          // rate the network did not earn.
          if (usageScope) {
            void localApi.usage
              .record({
                direction: 'consumed',
                scope: usageScope,
                model_slug: servedModelSlug,
                ...(finalMsg.meta?.output_tokens === undefined
                  ? {}
                  : { output_tokens: finalMsg.meta.output_tokens }),
                ...(finalMsg.meta?.duration_ms === undefined
                  ? {}
                  : { duration_ms: finalMsg.meta.duration_ms }),
                ok: terminalErrorText === null,
                ...(terminalErrorText === null
                  ? {}
                  : { error_class: streamError !== null ? 'stream_error' : 'truncated' }),
              })
              .catch(() => {
                /* telemetry is best-effort; never surface to the user */
              });
          }
        }
      }
    } catch (err) {
      if (controller.signal.aborted || get().abortController !== controller ||
          (err instanceof Error && err.name === 'AbortError')) {
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

      // Persist the failed assistant message so history reflects what actually
      // happened. 'error' is a real persisted status all the way down (localApi,
      // the sidecar's append endpoint, the schema), so the row says it failed
      // instead of hiding an error string inside the content.
      const errConvo = get().conversations[conversationId];
      const errMsg = errConvo?.messages.find((m) => m.id === assistantId);
      if (errMsg) {
        persistContent(
          historyAllowed,
          () =>
            localApi.history.appendMessage(conversationId, {
              id: assistantId,
              role: 'assistant',
              content: errMsg.content,
              status: 'error',
              finish_reason: 'error',
            }),
          'append:assistant-error',
        );
      }

      // A dispatch that never produced a stream is still a failed network turn.
      if (usageScope) {
        void localApi.usage
          .record({
            direction: 'consumed',
            scope: usageScope,
            model_slug: servedModelSlug,
            ok: false,
            error_class: 'dispatch_error',
          })
          .catch(() => {
            /* telemetry is best-effort; never surface to the user */
          });
      }
    } finally {
      if (!get().abortController || get().abortController === controller) currentTurnHistoryAllowed = false;
    }
  },

  abort() {
    const { abortController, streamingMessageId, conversations } = get();
    // The user may have switched conversations while the request was running.
    const activeConversationId = Object.values(conversations)
      .find((c) => c.messages.some((m) => m.id === streamingMessageId))?.id ?? null;
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
        persistContent(
          currentTurnHistoryAllowed,
          () =>
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
    get().abort();
    // State-only reset — does NOT delete persisted conversations from disk.
    // Use deleteConversation() per-id for that. This action exists for
    // tests and future "panic clear" UI; not currently bound to any view.
    knownPersistedIds.clear();
    pendingCreates.clear();
    currentTurnHistoryAllowed = false;
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
    // Remembered like the scope, so a restart with the circle scope still
    // selected has a circle to send to.
    if (typeof window !== 'undefined') {
      if (id === null) window.localStorage.removeItem(ACTIVE_CIRCLE_KEY);
      else window.localStorage.setItem(ACTIVE_CIRCLE_KEY, id);
    }
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

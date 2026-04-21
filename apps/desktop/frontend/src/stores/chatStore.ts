/**
 * Chat store — manages conversations and streaming state.
 *
 * Zustand store typed with generics. No persistence in v0 (chat history
 * is opt-in via the Python sidecar's SQLite backend, not the frontend store).
 *
 * Actions:
 *   addMessage      — append a complete message to the active conversation
 *   startStreaming  — mark the store as mid-stream; creates a placeholder message
 *   appendTokens    — extend the streaming placeholder with new content
 *   finishStreaming — seal the in-progress message
 *   abort           — signal the active AbortController to stop generation
 *   setActiveConversation — switch the visible conversation
 *   newConversation — create and activate a fresh conversation
 */

import { create } from 'zustand';
import type { ChatMessage, Conversation } from '@/types/chat';

interface ChatState {
  conversations: Conversation[];
  activeConversationId: string | null;
  isStreaming: boolean;
  abortController: AbortController | null;

  // Derived helpers
  activeConversation: () => Conversation | null;

  // Actions
  newConversation: () => string;
  setActiveConversation: (id: string) => void;
  addMessage: (conversationId: string, message: ChatMessage) => void;
  startStreaming: (conversationId: string, placeholderMessage: ChatMessage) => void;
  appendTokens: (conversationId: string, messageId: string, tokens: string) => void;
  finishStreaming: () => void;
  abort: () => void;
}

function makeConversation(): Conversation {
  const now = Date.now();
  return {
    id: crypto.randomUUID(),
    title: 'New conversation',
    messages: [],
    createdAt: now,
    updatedAt: now,
  };
}

export const useChatStore = create<ChatState>()((set, get) => ({
  conversations: [],
  activeConversationId: null,
  isStreaming: false,
  abortController: null,

  activeConversation() {
    const { conversations, activeConversationId } = get();
    return conversations.find((c) => c.id === activeConversationId) ?? null;
  },

  newConversation() {
    const conversation = makeConversation();
    set((s) => ({
      conversations: [conversation, ...s.conversations],
      activeConversationId: conversation.id,
    }));
    return conversation.id;
  },

  setActiveConversation(id) {
    set({ activeConversationId: id });
  },

  addMessage(conversationId, message) {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? { ...c, messages: [...c.messages, message], updatedAt: Date.now() }
          : c,
      ),
    }));
  },

  startStreaming(conversationId, placeholderMessage) {
    const controller = new AbortController();
    set((s) => ({
      isStreaming: true,
      abortController: controller,
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? { ...c, messages: [...c.messages, placeholderMessage], updatedAt: Date.now() }
          : c,
      ),
    }));
  },

  appendTokens(conversationId, messageId, tokens) {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === messageId ? { ...m, content: m.content + tokens } : m,
              ),
            }
          : c,
      ),
    }));
  },

  finishStreaming() {
    set({ isStreaming: false, abortController: null });
  },

  abort() {
    const { abortController } = get();
    abortController?.abort();
    set({ isStreaming: false, abortController: null });
  },
}));

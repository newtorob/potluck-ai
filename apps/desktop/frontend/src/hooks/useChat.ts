/**
 * useChat — thin facade over chatStore.
 *
 * Keeps components clean by selecting only what they need and exposing
 * a stable action API. Components should import this hook rather than
 * reaching into useChatStore directly.
 */

import { useChatStore } from '@/stores/chatStore';
import type { AppConversation, AppMessage } from '@/stores/chatStore';

export interface UseChatReturn {
  conversations: AppConversation[];
  activeConversation: AppConversation | null;
  activeConversationId: string | null;
  messages: AppMessage[];
  isStreaming: boolean;
  streamingMessageId: string | null;

  // Actions
  send: (content: string) => Promise<void>;
  abort: () => void;
  createConversation: (modelSlug?: string) => Promise<string>;
  deleteConversation: (id: string) => Promise<void>;
  switchConversation: (id: string) => Promise<void>;
  renameConversation: (id: string, title: string) => Promise<void>;
  clearConversations: () => void;
}

export function useChat(): UseChatReturn {
  const conversationsMap = useChatStore((s) => s.conversations);
  const activeConversationId = useChatStore((s) => s.activeConversationId);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const streamingMessageId = useChatStore((s) => s.streamingMessageId);

  const sendMessage = useChatStore((s) => s.sendMessage);
  const abort = useChatStore((s) => s.abort);
  const createConversation = useChatStore((s) => s.createConversation);
  const deleteConversation = useChatStore((s) => s.deleteConversation);
  const switchConversation = useChatStore((s) => s.switchConversation);
  const renameConversation = useChatStore((s) => s.renameConversation);
  const clearConversations = useChatStore((s) => s.clearConversations);

  const conversations = Object.values(conversationsMap).sort(
    (a, b) => b.updatedAt - a.updatedAt,
  );

  const activeConversation =
    activeConversationId ? (conversationsMap[activeConversationId] ?? null) : null;

  const messages = activeConversation?.messages ?? [];

  return {
    conversations,
    activeConversation,
    activeConversationId,
    messages,
    isStreaming,
    streamingMessageId,
    send: sendMessage,
    abort,
    createConversation,
    deleteConversation,
    switchConversation,
    renameConversation,
    clearConversations,
  };
}

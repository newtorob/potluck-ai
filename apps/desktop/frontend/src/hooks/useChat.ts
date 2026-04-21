/**
 * useChat — primary hook for sending messages and consuming the stream.
 *
 * Orchestrates: chatStore state, routeInference, streamTokens parser.
 * Components should use this hook rather than calling the store or API directly.
 */

import { useCallback } from 'react';
import { useChatStore } from '@/stores/chatStore';
import { routeInference } from '@/lib/inference';
import { streamTokens } from '@/lib/streaming';
import { useModelStore } from '@/stores/modelStore';
import type { ChatMessage } from '@/types/chat';

interface UseChatReturn {
  conversations: ReturnType<typeof useChatStore.getState>['conversations'];
  activeConversationId: string | null;
  isStreaming: boolean;
  send: (text: string) => Promise<void>;
  abort: () => void;
  newConversation: () => string;
}

export function useChat(): UseChatReturn {
  const conversations = useChatStore((s) => s.conversations);
  const activeConversationId = useChatStore((s) => s.activeConversationId);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const addMessage = useChatStore((s) => s.addMessage);
  const startStreaming = useChatStore((s) => s.startStreaming);
  const appendTokens = useChatStore((s) => s.appendTokens);
  const finishStreaming = useChatStore((s) => s.finishStreaming);
  const abort = useChatStore((s) => s.abort);
  const newConversation = useChatStore((s) => s.newConversation);
  const activeModelId = useModelStore((s) => s.activeModelId);

  const send = useCallback(
    async (text: string) => {
      if (isStreaming) return;

      // Snapshot current state — do not close over reactive values
      const state = useChatStore.getState();

      // Ensure we have an active conversation
      let conversationId = state.activeConversationId;
      if (!conversationId) {
        conversationId = newConversation();
      }

      // Add the user message
      const userMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'user',
        content: text,
        createdAt: Date.now(),
      };
      addMessage(conversationId, userMessage);

      // Create an assistant placeholder for streaming
      const assistantMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'assistant',
        content: '',
        createdAt: Date.now(),
      };

      // Build message history from the snapshot (before the placeholder is added)
      const conversation = state.conversations.find((c) => c.id === conversationId);
      const messages: ChatMessage[] = conversation
        ? [...conversation.messages, userMessage]
        : [userMessage];

      startStreaming(conversationId, assistantMessage);

      try {
        const { response } = await routeInference(
          messages,
          { abortSignal: useChatStore.getState().abortController?.signal },
          activeModelId ?? '',
        );

        for await (const token of streamTokens(response)) {
          appendTokens(conversationId, assistantMessage.id, token.content);
        }
      } catch (err) {
        if (err instanceof Error && err.name === 'AbortError') {
          // User-initiated abort — not an error
          return;
        }
        // TODO: surface to user via toast / inline error state
        console.error('[useChat] inference error:', err);
      } finally {
        finishStreaming();
      }
    },
    // Actions from Zustand are stable references — only re-create if isStreaming changes
    [isStreaming, addMessage, startStreaming, appendTokens, finishStreaming, newConversation, activeModelId],
  );

  return {
    conversations,
    activeConversationId,
    isStreaming,
    send,
    abort,
    newConversation,
  };
}

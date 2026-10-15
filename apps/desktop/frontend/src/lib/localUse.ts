import { useChatStore } from '@/stores/chatStore';
import { useUiPrefsStore } from '@/stores/uiPrefsStore';

/** Enter local use without touching history, installed models, or account credentials. */
export function continueLocally(): void {
  useChatStore.getState().setRoutingScope('local');
  useUiPrefsStore.getState().setLocalUseEnabled(true);
}

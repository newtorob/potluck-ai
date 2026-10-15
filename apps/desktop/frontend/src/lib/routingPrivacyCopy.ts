import type { RoutingScope } from '@/stores/chatStore';

/** Keep the scope picker and composer consistent about who can read a request. */
export const ROUTING_PRIVACY_COPY: Record<RoutingScope, string> = {
  local:
    'Uses the model loaded on this machine. Requests are not sent to a circle or the contributor network.',
  household:
    'Runs on one of your paired machines over an encrypted connection. The machine answering processes your conversation.',
  circle:
    'The member operating the answering machine can read your conversation. Potluck’s coordinator can too when relaying it. Its contents may identify you.',
  cooperative:
    'The contributor and Potluck’s coordinator can read the prompt you send. The contributor job omits your account identity, but your prompt may identify you.',
};

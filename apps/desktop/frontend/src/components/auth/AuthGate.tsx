import type { ReactNode } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { useDeepLinkAuth } from '@/hooks/useDeepLinkAuth';

interface AuthGateProps {
  children: ReactNode;
}

/**
 * AuthGate — resolves auth state and handles magic-link deep links, without
 * gating the app.
 *
 * Potluck is local-first: local chat, memory, and MCP work with no account, so
 * this never blocks rendering. It kicks off a one-time silent session
 * rehydration (useAuth) and mounts the global potluck:// deep-link listener
 * (useDeepLinkAuth). Sign-in lives on the Membership view and in
 * Settings → Account.
 */
export function AuthGate({ children }: AuthGateProps) {
  useAuth();
  useDeepLinkAuth();
  return <>{children}</>;
}

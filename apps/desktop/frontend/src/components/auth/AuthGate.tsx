import type { ReactNode } from 'react';

interface AuthGateProps {
  children: ReactNode;
}

/**
 * AuthGate — wraps the application and enforces authentication.
 *
 * v0 scaffold: pass-through. Real implementation will:
 *   - Read authStore.session
 *   - Redirect to MagicLinkForm if session is null
 *   - Validate session expiry; call cloudApi.getMe() on mount
 *   - Show a full-screen loading state while session is resolving
 *
 * TODO (guide §4.7 / cloudApi): Replace pass-through with real guard.
 */
export function AuthGate({ children }: AuthGateProps) {
  // Pass-through for scaffold phase — auth enforcement comes later
  return <>{children}</>;
}

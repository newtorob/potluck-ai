import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { EmptyState } from '@/components/layout/EmptyState';
import { hasAccountAccess, useAuthStore } from '@/stores/authStore';

/** Do not mount account-backed controls or their effects before sign-in. */
export function AccountRequired({ children, feature }: { children: ReactNode; feature: string }) {
  const available = useAuthStore(hasAccountAccess);
  if (available) return <>{children}</>;
  return <EmptyState
    title={`Sign in to ${feature}`}
    description="Local chat and model management are available without an account. Signing in keeps your local chats and settings."
    primaryAction={<Link to="/membership" className="rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Sign in</Link>}
    secondaryAction={<Link to="/chat" className="rounded-sm text-sm text-muted-foreground underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Back to local chat</Link>}
  />;
}

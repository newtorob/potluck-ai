/**
 * useMembership - fetches membership details and exposes billing portal action.
 *
 * Only called when the user is signed in (check useAuth().isSignedIn first).
 */

import { useState, useEffect, useCallback } from 'react';
import { cloudApi } from '@/lib/cloudApi';
import type { MembershipPlan } from '@/types/api';

export interface MembershipDetails {
  plan: MembershipPlan | null;
  status: string;
  isFoundingMember: boolean;
  currentPeriodEnd: string | null;
}

interface UseMembershipReturn {
  membership: MembershipDetails | null;
  isLoading: boolean;
  error: string | null;
  openBillingPortal: () => Promise<void>;
  startCheckout: (plan: MembershipPlan) => Promise<void>;
  refresh: () => void;
}

export function useMembership(): UseMembershipReturn {
  const [membership, setMembership] = useState<MembershipDetails | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);

    cloudApi
      .getMembership()
      .then((data) => {
        if (cancelled) return;
        setMembership(data);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load membership');
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [tick]);

  const openBillingPortal = useCallback(async () => {
    const { url } = await cloudApi.getBillingPortal();
    // Open in the user's default browser via Electron shell or window.open
    window.open(url, '_blank', 'noopener,noreferrer');
  }, []);

  const startCheckout = useCallback(async (plan: MembershipPlan) => {
    const { url } = await cloudApi.createCheckout(plan);
    window.open(url, '_blank', 'noopener,noreferrer');
  }, []);

  return {
    membership,
    isLoading,
    error,
    openBillingPortal,
    startCheckout,
    refresh: () => setTick((t) => t + 1),
  };
}

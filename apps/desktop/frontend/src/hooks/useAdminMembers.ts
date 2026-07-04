/**
 * useAdminMembers — fetches the admin member roster and exposes the
 * grant/revoke-founding mutations. Admin-only; the underlying API 403s a
 * non-admin session (the caller should gate on memberProfile.isAdmin).
 *
 * Mirrors useMembership: fetch on mount, cancelled-guard, tick-based refresh.
 * Both mutations refresh the list after they resolve so the row reflects the
 * new server state.
 */

import { useState, useEffect, useCallback } from 'react';
import { cloudApi } from '@/lib/cloudApi';
import type { AdminMembersPage } from '@/types/api';

interface UseAdminMembersReturn {
  page: AdminMembersPage | null;
  isLoading: boolean;
  error: string | null;
  refresh: () => void;
  grantFounding: (memberId: string) => Promise<void>;
  revokeFounding: (memberId: string) => Promise<void>;
}

export function useAdminMembers(): UseAdminMembersReturn {
  const [page, setPage] = useState<AdminMembersPage | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);

    cloudApi
      .adminListMembers()
      .then((data) => {
        if (!cancelled) setPage(data);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load members');
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [tick]);

  const grantFounding = useCallback(
    async (memberId: string) => {
      await cloudApi.adminGrantFounding(memberId);
      refresh();
    },
    [refresh],
  );

  const revokeFounding = useCallback(
    async (memberId: string) => {
      await cloudApi.adminRevokeFounding(memberId);
      refresh();
    },
    [refresh],
  );

  return { page, isLoading, error, refresh, grantFounding, revokeFounding };
}

/**
 * useWeeklyReceiptToast - surfaces the "what your machines did last week"
 * toast once per completed week, on app start.
 *
 * Rules:
 *   - Only fires when contributionReceiptsEnabled() is true.
 *   - Uses the last complete week: weeks[weeks.length - 2] (the bucket
 *     immediately before the current/partial final bucket).
 *   - Skips silently if the sidecar is unreachable, there are fewer than 2
 *     week buckets, or the prior week had zero activity.
 *   - Shows at most once per week_start (tracked in localStorage).
 *   - Returns toast state + a dismiss handler for the rendering layer.
 */

import { useEffect, useState } from 'react';
import { localApi } from '@/lib/localApi';
import {
  contributionReceiptsEnabled,
  weeklyReceiptToastShownFor,
  setWeeklyReceiptToastShownFor,
} from '@/lib/appPrefs';

export interface WeeklyReceiptToastData {
  weekStart: string;
  requestsServed: number;
  creditsEarned: string;
}

interface ToastState {
  visible: boolean;
  data: WeeklyReceiptToastData | null;
}

export function useWeeklyReceiptToast(): {
  toast: ToastState;
  dismiss: () => void;
} {
  const [toast, setToast] = useState<ToastState>({ visible: false, data: null });

  useEffect(() => {
    // Gate on user preference.
    if (!contributionReceiptsEnabled()) return;

    let cancelled = false;

    void (async () => {
      try {
        const weekly = await localApi.network.myContributionsWeekly(8);
        if (cancelled) return;

        const { weeks } = weekly;

        // Need at least 2 buckets: one complete prior week + the current partial.
        if (weeks.length < 2) return;

        // The second-to-last bucket is the last complete week.
        const priorWeek = weeks[weeks.length - 2];
        if (!priorWeek) return;

        // Skip if no activity that week.
        if (priorWeek.requests_served === 0) return;

        // Skip if we already showed (or dismissed) for this week_start.
        if (weeklyReceiptToastShownFor() === priorWeek.week_start) return;

        // Mark shown immediately so a fast re-mount won't double-fire.
        setWeeklyReceiptToastShownFor(priorWeek.week_start);

        setToast({
          visible: true,
          data: {
            weekStart: priorWeek.week_start,
            requestsServed: priorWeek.requests_served,
            creditsEarned: priorWeek.credits_earned,
          },
        });
      } catch {
        // Sidecar / coordinator unreachable - skip silently.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  const dismiss = () => {
    setToast((prev) => ({ ...prev, visible: false }));
    // The shown key is already written on display; dismiss is just a visual hide.
  };

  return { toast, dismiss };
}

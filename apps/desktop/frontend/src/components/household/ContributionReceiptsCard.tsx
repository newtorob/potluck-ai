/**
 * ContributionReceiptsCard — contributor side of the mesh, made visible.
 *
 * Shows:
 *   1. A plain headline summarising this week's totals.
 *   2. A per-machine breakdown with hostname, requests served, credits earned,
 *      and a quiet reputation dot.
 *   3. A last-8-weeks bar chart (requests served per week).
 *
 * Privacy rule: aggregate counts only — no per-request detail, no indication
 * of who asked what. The coordinator already enforces this at the API layer;
 * this component simply never attempts to display finer grain.
 *
 * Error/empty contract: coordinator-unreachable or zero-activity both render
 * as the calm empty state, not an error wall.
 */

import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import type {
  ContributionsResponse,
  ContributionsWeeklyResponse,
  ContributionWeek,
} from '@/lib/localApi';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Format a decimal-as-string credit value with thousands separators. */
function formatCredits(raw: string): string {
  const n = parseFloat(raw);
  if (isNaN(n)) return '0';
  // Two decimal places, comma thousands separator.
  return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Format a request count with thousands separators. */
function formatCount(n: number): string {
  return n.toLocaleString('en-US');
}

/**
 * "Jun 16" from "2026-06-16" — short and timezone-stable (treats the date
 * as local midnight so the label matches what the user sees on a calendar).
 */
function shortDate(isoDate: string): string {
  // Split on '-' to avoid any UTC shift from new Date(string).
  const parts = isoDate.split('-');
  if (parts.length !== 3) return isoDate;
  const [y, m, d] = parts as [string, string, string];
  const dt = new Date(parseInt(y), parseInt(m) - 1, parseInt(d));
  return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

/**
 * Map a reputation score [0,1] to a small coloured dot.
 * null = no data yet (omit).
 * ~0.5 = neutral (muted).
 * > 0.7 = trusted (sage).
 * < 0.3 = flagged (amber).
 */
function ReputationDot({ score }: { score: number | null }) {
  if (score === null) return null;

  const cls =
    score >= 0.7
      ? 'bg-sage'
      : score < 0.3
        ? 'bg-amber-400'
        : 'bg-muted-foreground/40';

  const label =
    score >= 0.7
      ? `Reputation: trusted (${(score * 100).toFixed(0)}%)`
      : score < 0.3
        ? `Reputation: flagged (${(score * 100).toFixed(0)}%)`
        : `Reputation: neutral (${(score * 100).toFixed(0)}%)`;

  return (
    <span
      className={cn('inline-block h-2 w-2 shrink-0 rounded-full', cls)}
      title={label}
      aria-label={label}
    />
  );
}

// ---------------------------------------------------------------------------
// Weekly bar chart (hand-laid SVG)
// ---------------------------------------------------------------------------

/** 8-week bar chart — requests served per week, x-axis labelled by week_start. */
function WeeklyChart({ weeks }: { weeks: ContributionWeek[] }) {
  if (weeks.length === 0) return null;

  const maxReq = Math.max(...weeks.map((w) => w.requests_served), 1);

  // Viewport: fixed width, fixed height. Bars fill the full column width with
  // a 2-unit gap between. X-labels sit below the chart.
  const W = 420;
  const H = 64;         // chart area height
  const LABEL_H = 20;   // label area below
  const TOTAL_H = H + LABEL_H;
  const n = weeks.length;
  const barW = Math.floor((W - (n - 1) * 2) / n);
  const gap = Math.floor((W - barW * n) / (n - 1) || 0);

  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`0 0 ${W} ${TOTAL_H}`}
        className="w-full max-w-[420px]"
        aria-label="Weekly requests served"
        role="img"
      >
        {weeks.map((week, i) => {
          const x = i * (barW + gap);
          const barH = Math.max(2, Math.round((week.requests_served / maxReq) * H));
          const y = H - barH;
          const isEmpty = week.requests_served === 0;

          const tooltipParts = [
            `Week of ${shortDate(week.week_start)}`,
            `${formatCount(week.requests_served)} request${week.requests_served === 1 ? '' : 's'}`,
            `${formatCredits(week.credits_earned)} credits`,
          ];

          return (
            <g key={week.week_start}>
              <title>{tooltipParts.join(' · ')}</title>
              {/* Bar */}
              <rect
                x={x}
                y={y}
                width={barW}
                height={barH}
                rx={2}
                className={cn(
                  isEmpty
                    ? 'fill-muted-foreground/15'
                    : 'fill-primary/70',
                )}
              />
              {/* x-axis label */}
              <text
                x={x + barW / 2}
                y={H + LABEL_H - 2}
                textAnchor="middle"
                fontSize={9}
                className="fill-muted-foreground/60"
              >
                {shortDate(week.week_start)}
              </text>
            </g>
          );
        })}
      </svg>
      <p className="mt-1 text-[10px] text-muted-foreground/50">Requests served per week</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

type FetchState =
  | { status: 'loading' }
  | { status: 'empty' }
  | { status: 'error' }
  | {
      status: 'ok';
      summary: ContributionsResponse;
      weekly: ContributionsWeeklyResponse;
    };

export function ContributionReceiptsCard() {
  const [state, setState] = useState<FetchState>({ status: 'loading' });

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const [summary, weekly] = await Promise.all([
          localApi.network.myContributions(7),
          localApi.network.myContributionsWeekly(8),
        ]);
        if (cancelled) return;

        const hasActivity =
          summary.total_requests_served > 0 || summary.machines.length > 0;

        if (!hasActivity) {
          setState({ status: 'empty' });
        } else {
          setState({ status: 'ok', summary, weekly });
        }
      } catch {
        if (!cancelled) setState({ status: 'error' });
      }
    };

    void load();

    // Light periodic refresh — same cadence neighbourhood as the health poll
    // (every 2 minutes). Not real-time; these numbers don't need to be live.
    const id = setInterval(() => void load(), 120_000);

    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Loading — show nothing rather than a skeleton for a non-critical card.
  // The card will appear once data arrives.
  if (state.status === 'loading') {
    return (
      <div className="rounded-2xl border border-border/60 bg-card p-5 shadow-hearth-sm">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          Loading contributions…
        </div>
      </div>
    );
  }

  // Error or empty — calm, never a red wall.
  if (state.status === 'error' || state.status === 'empty') {
    return (
      <div className="rounded-2xl border border-border/60 bg-card p-5 shadow-hearth-sm">
        <p className="text-sm text-muted-foreground leading-snug">
          Your machines haven't served any requests yet. Turn on contributing
          in Settings to share spare compute.
        </p>
      </div>
    );
  }

  const { summary, weekly } = state;
  const { total_requests_served, total_credits_earned, machines } = summary;

  return (
    <div className="rounded-2xl border border-border/60 bg-card p-5 space-y-5 shadow-hearth-sm">
      {/* Headline */}
      <div>
        <p className="font-serif text-base leading-tight tracking-display text-foreground">
          {total_requests_served === 0 ? (
            'Your machines served 0 requests this week.'
          ) : (
            <>
              Your machines served{' '}
              <span className="text-foreground font-medium">
                {formatCount(total_requests_served)}
              </span>{' '}
              {total_requests_served === 1 ? 'request' : 'requests'} this week
              and earned{' '}
              <span className="text-foreground font-medium">
                {formatCredits(total_credits_earned)}
              </span>{' '}
              credits.
            </>
          )}
        </p>
      </div>

      {/* Per-machine breakdown */}
      {machines.length > 0 && (
        <div>
          <p className="mb-2 font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
            By machine
          </p>
          <ul className="space-y-1.5" aria-label="Per-machine contributions">
            {machines.map((m) => {
              const label = m.hostname ?? m.peer_id.slice(0, 8);
              return (
                <li
                  key={m.peer_id}
                  className="flex items-center gap-3 rounded-lg border border-border/40 bg-background/40 px-3 py-2"
                >
                  <ReputationDot score={m.reputation} />
                  <span
                    className="min-w-0 flex-1 truncate text-sm font-medium text-foreground"
                    title={m.peer_id}
                  >
                    {label}
                  </span>
                  <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                    {formatCount(m.requests_served)}{' '}
                    {m.requests_served === 1 ? 'req' : 'reqs'}
                  </span>
                  <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                    {formatCredits(m.credits_earned)} cr
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {/* Weekly bar chart */}
      {weekly.weeks.length > 0 && (
        <div>
          <p className="mb-2 font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
            Last {weekly.window_weeks} weeks
          </p>
          <WeeklyChart weeks={weekly.weeks} />
        </div>
      )}
    </div>
  );
}

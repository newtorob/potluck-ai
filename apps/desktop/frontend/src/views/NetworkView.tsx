/**
 * NetworkView — /network route.
 *
 * The "see the co-op work" surface. Four sections:
 *
 *   1. Aggregate fleet card — peers online, models served, jobs/tokens
 *      in the last 24h.
 *   2. Available models chip rack — one chip per slug the network serves.
 *   3. Your usage card — requester-side stats for the last 24h.
 *   4. Credit balance — earned / spent / net from the Phase G ledger,
 *      polled every 30s.
 *   5. Credit history — the 10 most-recent ledger entries.
 *
 * Privacy invariant (memory 42a7e671 / b3cbfba9):
 *   NO peer_id, hostname, hardware specs, or per-request peer attribution
 *   surface here. Aggregate-only.
 *
 * Credit polling: balance refreshes every 30s; history refreshes alongside
 * it. Both pause on tab blur (same pattern as fleet polling).
 */

import { useEffect, useRef, useState } from 'react';
import { Globe2, Layers, Activity, Cpu, BarChart3, TrendingUp, TrendingDown, Minus, type LucideIcon } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import type { FleetSummary, MyJobStats, CreditBalance, CreditHistory, CreditHistoryEntry } from '@/lib/localApi';
import { relativeTime } from '@/lib/timeFormat';
import { formatModelName } from '@/lib/modelName';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';

const FLEET_POLL_MS = 5_000;
const CREDIT_POLL_MS = 30_000;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Format a Decimal-string like "13.50" for display. Preserves precision. */
function fmtCredits(raw: string): string {
  const n = Number(raw);
  if (!Number.isFinite(n)) return raw;
  return n.toFixed(2);
}

// ---------------------------------------------------------------------------
// NetworkView
// ---------------------------------------------------------------------------

export function NetworkView() {
  const [fleet, setFleet] = useState<FleetSummary | null>(null);
  const [myStats, setMyStats] = useState<MyJobStats | null>(null);
  const [fleetError, setFleetError] = useState<string | null>(null);
  const [isFetchingFleet, setIsFetchingFleet] = useState(true);

  const [balance, setBalance] = useState<CreditBalance | null>(null);
  const [history, setHistory] = useState<CreditHistory | null>(null);
  const [creditError, setCreditError] = useState<string | null>(null);
  const [isFetchingCredits, setIsFetchingCredits] = useState(true);

  // Stable refs so polling closures always capture the latest fetch fns.
  const fetchFleetRef = useRef<() => Promise<void>>(async () => {});
  fetchFleetRef.current = async () => {
    try {
      const [f, s] = await Promise.all([
        localApi.network.fleet(),
        localApi.network.myStats().catch(() => null),
      ]);
      setFleet(f);
      setMyStats(s);
      setFleetError(null);
    } catch (err) {
      setFleetError(err instanceof Error ? err.message : 'Network fetch failed');
    } finally {
      setIsFetchingFleet(false);
    }
  };

  const fetchCreditsRef = useRef<() => Promise<void>>(async () => {});
  fetchCreditsRef.current = async () => {
    try {
      const [b, h] = await Promise.all([
        localApi.credits.balance(),
        localApi.credits.history({ limit: 10 }),
      ]);
      setBalance(b);
      setHistory(h);
      setCreditError(null);
    } catch (err) {
      setCreditError(
        err instanceof Error ? err.message : 'Credit fetch failed',
      );
    } finally {
      setIsFetchingCredits(false);
    }
  };

  // Fleet polling lifecycle.
  useEffect(() => {
    void fetchFleetRef.current();
    let fleetTimer: ReturnType<typeof setTimeout> | null = null;
    function scheduleFleet() {
      fleetTimer = setTimeout(() => {
        if (!document.hidden) void fetchFleetRef.current();
        scheduleFleet();
      }, FLEET_POLL_MS);
    }
    scheduleFleet();

    function onVisibility() {
      if (!document.hidden) {
        void fetchFleetRef.current();
        void fetchCreditsRef.current();
      }
    }
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      if (fleetTimer !== null) clearTimeout(fleetTimer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  // Credit polling lifecycle (separate timer — 30s cadence).
  useEffect(() => {
    void fetchCreditsRef.current();
    let creditTimer: ReturnType<typeof setTimeout> | null = null;
    function scheduleCredits() {
      creditTimer = setTimeout(() => {
        if (!document.hidden) void fetchCreditsRef.current();
        scheduleCredits();
      }, CREDIT_POLL_MS);
    }
    scheduleCredits();
    return () => {
      if (creditTimer !== null) clearTimeout(creditTimer);
    };
  }, []);

  const onlineNow = fleet?.peers.online_now ?? 0;
  const hasPeers = onlineNow > 0;

  // ---- empty state -------------------------------------------------------

  if (!isFetchingFleet && fleet !== null && !hasPeers) {
    return (
      <div className="flex h-full flex-col">
        <NetworkHeader fleet={null} />
        <EmptyState
          icon={Globe2}
          title="No peers online right now"
          description={
            <>
              When members run the contributor sidecar, they show up here.
              You can also <span className="font-medium text-foreground">be the first peer</span> —
              flip on inference in <span className="font-medium text-foreground">Settings</span> and your
              machine joins the network.
            </>
          }
        />
      </div>
    );
  }

  // ---- error state -------------------------------------------------------

  if (fleetError !== null && fleet === null) {
    return (
      <div className="flex h-full flex-col">
        <NetworkHeader fleet={null} />
        <EmptyState
          icon={Globe2}
          tone="warning"
          title="Couldn't reach the coordinator"
          description={
            <>
              Make sure the coordinator is running (<code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">make up</code>)
              and reachable at the configured URL. Network mode in chat won't work until this is fixed.
            </>
          }
        />
      </div>
    );
  }

  // ---- normal state ------------------------------------------------------

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <NetworkHeader fleet={fleet} balance={balance} />

      <div className="flex-1 overflow-y-auto px-6 py-6 space-y-8">
        {/* Aggregate fleet card */}
        <section aria-label="Aggregate fleet stats">
          <SectionHeader>The network right now</SectionHeader>
          <FleetCard fleet={fleet} />
        </section>

        {/* Available models */}
        <section aria-label="Available models">
          <SectionHeader>Models the network can serve</SectionHeader>
          <ModelChips models={fleet?.models ?? []} />
        </section>

        {/* Your usage */}
        <section aria-label="Your usage">
          <SectionHeader>Your usage in the last 24 hours</SectionHeader>
          <MyUsageCard stats={myStats} />
        </section>

        {/* Credit balance */}
        <section aria-label="Your credit balance">
          <SectionHeader>Your credit balance</SectionHeader>
          <CreditBalanceCard
            balance={balance}
            isLoading={isFetchingCredits}
            error={creditError}
          />
        </section>

        {/* Credit history */}
        <section aria-label="Recent credit activity">
          <SectionHeader>Recent credit activity</SectionHeader>
          <CreditHistoryList
            history={history}
            isLoading={isFetchingCredits}
            error={creditError}
          />
        </section>

        {/* Privacy footer */}
        <p
          className="pt-2 text-[11px] text-muted-foreground/70 max-w-2xl"
          aria-label="Privacy note"
        >
          The network numbers above are aggregate. We never show which specific peer
          served which prompt, by design — that's the manifesto's blinded-routing
          rule. Credit rows reflect your own ledger only; hash-chain verification
          is coming in a future update.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

function NetworkHeader({
  fleet,
  balance,
}: {
  fleet: FleetSummary | null;
  balance?: CreditBalance | null;
}) {
  const onlineNow = fleet?.peers.online_now ?? 0;
  const isAlive = onlineNow > 0;

  // Net balance pill — only show when we have data.
  const netPill = balance !== null && balance !== undefined ? (
    <NetBalancePill net={balance.net} />
  ) : null;

  return (
    <ViewHeader
      eyebrow="Member-operated"
      title="Network"
      description={
        <span className="flex items-center gap-3">
          <span className="flex items-center gap-1.5">
            <span
              className={cn(
                'inline-block h-2 w-2 rounded-full',
                isAlive ? 'bg-sage animate-hearth-pulse' : 'bg-muted-foreground/40',
              )}
              aria-hidden="true"
            />
            {isAlive
              ? `${onlineNow} peer${onlineNow === 1 ? '' : 's'} online`
              : 'Quiet — no peers online right now'}
          </span>
          {netPill}
        </span>
      }
    />
  );
}

// ---------------------------------------------------------------------------
// Net balance pill
// ---------------------------------------------------------------------------

function NetBalancePill({ net }: { net: string }) {
  const n = Number(net);
  const isPositive = n > 0;
  const isNegative = n < 0;

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium tabular-nums',
        isPositive
          ? 'border-sage/30 bg-sage/10 text-sage dark:text-sage'
          : isNegative
            ? 'border-border/60 bg-muted/60 text-muted-foreground'
            : 'border-border/60 bg-muted/60 text-muted-foreground',
      )}
      title="Your net credit balance (earned minus spent)"
    >
      {isPositive ? (
        <TrendingUp className="h-3 w-3" aria-hidden="true" />
      ) : isNegative ? (
        <TrendingDown className="h-3 w-3" aria-hidden="true" />
      ) : (
        <Minus className="h-3 w-3" aria-hidden="true" />
      )}
      {isPositive ? '+' : ''}{fmtCredits(net)} credits
    </span>
  );
}

// ---------------------------------------------------------------------------
// Aggregate fleet card
// ---------------------------------------------------------------------------

function FleetCard({ fleet }: { fleet: FleetSummary | null }) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard
        icon={Cpu}
        label="Peers online"
        value={fleet?.peers.online_now}
        sub={fleet ? `of ${fleet.peers.active_total} ever active` : undefined}
        emphasize
      />
      <StatCard
        icon={Layers}
        label="Models served"
        value={fleet?.models.length}
        sub={
          fleet && fleet.models.length > 0 && fleet.models[0]
            ? `top: ${formatModelName(fleet.models[0].slug)}`
            : undefined
        }
      />
      <StatCard
        icon={Activity}
        label="Jobs · 24h"
        value={fleet?.activity.jobs_completed_24h}
      />
      <StatCard
        icon={BarChart3}
        label="Tokens served · 24h"
        value={fleet?.activity.tokens_served_24h}
        formatLarge
      />
    </div>
  );
}

interface StatCardProps {
  icon: LucideIcon;
  label: string;
  value: number | undefined;
  sub?: string | undefined;
  emphasize?: boolean;
  formatLarge?: boolean;
}

function StatCard({ icon: Icon, label, value, sub, emphasize, formatLarge }: StatCardProps) {
  const display =
    value === undefined
      ? '—'
      : formatLarge && value >= 1000
        ? `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k`
        : value.toLocaleString();
  // An idle/zero metric should read quiet, not broken — mute it so a fresh
  // network doesn't greet the user with bleak-loud zeros.
  const isIdle = value === 0 || value === undefined;
  return (
    <div
      className={cn(
        'rounded-xl border bg-card p-4',
        emphasize && !isIdle ? 'border-primary/30' : 'border-border/60',
      )}
    >
      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <Icon className={cn('h-3 w-3', emphasize && !isIdle ? 'text-primary' : 'text-muted-foreground')} aria-hidden="true" />
        <span className="font-serif italic tracking-hearth-label">
          {label}
        </span>
      </div>
      <p
        className={cn(
          'mt-1 font-serif tabular-nums leading-tight tracking-display text-3xl',
          isIdle ? 'text-muted-foreground/40' : emphasize ? 'text-primary' : 'text-foreground',
        )}
      >
        {display}
      </p>
      {sub && <p className="mt-0.5 text-xs text-muted-foreground">{sub}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Available models
// ---------------------------------------------------------------------------

function ModelChips({ models }: { models: { slug: string; online_peer_count: number }[] }) {
  if (models.length === 0) {
    return (
      <p className="text-sm text-muted-foreground/70">
        No models currently advertised. Peers report what they serve when they
        connect; this populates as contributors come online.
      </p>
    );
  }
  return (
    <div className="flex flex-wrap gap-2">
      {models.map((m) => (
        <span
          key={m.slug}
          className={cn(
            'inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-card px-2.5 py-1',
            'text-xs',
          )}
          title={`${m.online_peer_count} peer${m.online_peer_count === 1 ? '' : 's'} currently serve ${m.slug}`}
        >
          <span className="text-foreground">{formatModelName(m.slug)}</span>
          <span
            className={cn(
              'rounded-full px-1.5 text-[10px] font-medium tabular-nums',
              m.online_peer_count > 0
                ? 'bg-sage/20 text-sage-foreground dark:text-sage'
                : 'bg-muted text-muted-foreground',
            )}
            aria-label={`${m.online_peer_count} peer${m.online_peer_count === 1 ? '' : 's'}`}
          >
            {m.online_peer_count}
          </span>
        </span>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Your usage
// ---------------------------------------------------------------------------

function MyUsageCard({ stats }: { stats: MyJobStats | null }) {
  if (stats === null) {
    return (
      <p className="text-sm text-muted-foreground/70">
        No requester stats available yet.
      </p>
    );
  }
  const noActivity = stats.jobs_24h === 0;
  return (
    <div className="rounded-xl border border-border/60 bg-card p-4">
      {noActivity ? (
        <p className="text-sm text-muted-foreground">
          You haven't routed anything through the network in the last 24
          hours. Flip on the <span className="font-medium text-foreground">Network</span> toggle in the chat
          composer to send a prompt to a peer.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <SmallStat label="Prompts routed" value={stats.jobs_24h.toLocaleString()} />
          <SmallStat
            label="Tokens received"
            value={
              stats.tokens_received_24h >= 1000
                ? `${(stats.tokens_received_24h / 1000).toFixed(1)}k`
                : stats.tokens_received_24h.toLocaleString()
            }
          />
          <SmallStat
            label="Avg latency"
            value={
              stats.avg_duration_ms >= 1000
                ? `${(stats.avg_duration_ms / 1000).toFixed(1)}s`
                : `${stats.avg_duration_ms}ms`
            }
          />
        </div>
      )}
    </div>
  );
}

function SmallStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p
        className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label"
      >
        {label}
      </p>
      <p className="mt-0.5 font-serif text-xl tabular-nums tracking-display text-foreground">
        {value}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Credit balance card
// ---------------------------------------------------------------------------

interface CreditBalanceCardProps {
  balance: CreditBalance | null;
  isLoading: boolean;
  error: string | null;
}

function CreditBalanceCard({ balance, isLoading, error }: CreditBalanceCardProps) {
  if (isLoading && balance === null) {
    return <CreditSkeleton />;
  }

  if (error !== null && balance === null) {
    return (
      <p className="text-sm text-muted-foreground/70">
        Coordinator unreachable — check connection.
      </p>
    );
  }

  if (balance === null) return null;

  const hasActivity =
    balance.earned !== '0' ||
    balance.spent !== '0' ||
    Number(balance.earned) !== 0 ||
    Number(balance.spent) !== 0;

  if (!hasActivity) {
    return (
      <div className="rounded-xl border border-border/60 bg-card p-4">
        <p className="text-sm text-muted-foreground">
          No activity yet — toggle network mode in chat to start contributing
          or consuming from the network.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-border/60 bg-card p-4 space-y-4">
      {/* Totals row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <SmallStat label="Earned (contributor)" value={`${fmtCredits(balance.earned)} cr`} />
        <SmallStat label="Spent (requester)" value={`${fmtCredits(balance.spent)} cr`} />
        <div>
          <p className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
            Net balance
          </p>
          <p
            className={cn(
              'mt-0.5 font-serif text-xl tabular-nums tracking-display',
              Number(balance.net) > 0
                ? 'text-sage dark:text-sage'
                : Number(balance.net) < 0
                  ? 'text-muted-foreground'
                  : 'text-foreground',
            )}
          >
            {Number(balance.net) > 0 ? '+' : ''}{fmtCredits(balance.net)} cr
          </p>
        </div>
      </div>

      {/* Per-model breakdown */}
      {balance.by_model.length > 0 && (
        <div>
          <p className="mb-2 font-serif text-[11px] italic text-muted-foreground/70 tracking-hearth-label">
            By model
          </p>
          <div className="space-y-1.5">
            {balance.by_model.map((m) => (
              <div key={m.model_slug} className="flex items-center gap-3 text-xs">
                <span className="text-foreground/80 truncate max-w-[14rem]" title={m.model_slug}>
                  {formatModelName(m.model_slug)}
                </span>
                <span className="ml-auto flex items-center gap-3 shrink-0 tabular-nums text-muted-foreground">
                  {Number(m.earned) > 0 && (
                    <span className="text-sage dark:text-sage">
                      +{fmtCredits(m.earned)}
                    </span>
                  )}
                  {Number(m.spent) > 0 && (
                    <span>−{fmtCredits(m.spent)}</span>
                  )}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Credit history list
// ---------------------------------------------------------------------------

interface CreditHistoryListProps {
  history: CreditHistory | null;
  isLoading: boolean;
  error: string | null;
}

function CreditHistoryList({ history, isLoading, error }: CreditHistoryListProps) {
  if (isLoading && history === null) {
    return <CreditSkeleton rows={4} />;
  }

  if (error !== null && history === null) {
    return (
      <p className="text-sm text-muted-foreground/70">
        Coordinator unreachable — check connection.
      </p>
    );
  }

  if (history === null || history.entries.length === 0) {
    return (
      <p className="text-sm text-muted-foreground/70">
        No credit history yet — your ledger entries will appear here once
        you contribute to or consume from the network.
      </p>
    );
  }

  return (
    <div className="rounded-xl border border-border/60 bg-card overflow-hidden">
      <table className="w-full text-xs" role="table">
        <thead>
          <tr className="border-b border-border/60 bg-muted/30">
            <th
              className="px-3 py-2 text-left font-serif italic text-[11px] text-muted-foreground font-normal tracking-hearth-label"
              scope="col"
            >
              Direction
            </th>
            <th
              className="px-3 py-2 text-left font-serif italic text-[11px] text-muted-foreground font-normal tracking-hearth-label"
              scope="col"
            >
              Model
            </th>
            <th
              className="px-3 py-2 text-right font-serif italic text-[11px] text-muted-foreground font-normal tracking-hearth-label"
              scope="col"
            >
              Tokens
            </th>
            <th
              className="px-3 py-2 text-right font-serif italic text-[11px] text-muted-foreground font-normal tracking-hearth-label"
              scope="col"
            >
              Credits
            </th>
            <th
              className="px-3 py-2 text-right font-serif italic text-[11px] text-muted-foreground font-normal tracking-hearth-label"
              scope="col"
            >
              When
            </th>
          </tr>
        </thead>
        <tbody>
          {history.entries.map((entry, idx) => (
            <CreditHistoryRow
              key={entry.id}
              entry={entry}
              isLast={idx === history.entries.length - 1}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CreditHistoryRow({
  entry,
  isLast,
}: {
  entry: CreditHistoryEntry;
  isLast: boolean;
}) {
  const isEarned = entry.direction === 'earned';
  return (
    <tr
      className={cn(
        'transition-colors hover:bg-accent/40',
        !isLast && 'border-b border-border/40',
        isEarned ? 'bg-sage/5' : '',
      )}
    >
      <td className="px-3 py-2">
        <span
          className={cn(
            'inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] font-medium',
            isEarned
              ? 'bg-sage/15 text-sage dark:text-sage'
              : 'bg-muted/60 text-muted-foreground',
          )}
        >
          {isEarned ? (
            <TrendingUp className="h-2.5 w-2.5" aria-hidden="true" />
          ) : (
            <TrendingDown className="h-2.5 w-2.5" aria-hidden="true" />
          )}
          {entry.direction}
        </span>
      </td>
      <td className="px-3 py-2">
        <span
          className="text-foreground/80 truncate block max-w-[12rem]"
          title={entry.model_slug}
        >
          {formatModelName(entry.model_slug)}
        </span>
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
        {entry.output_tokens.toLocaleString()}
      </td>
      <td
        className={cn(
          'px-3 py-2 text-right tabular-nums font-medium',
          isEarned ? 'text-sage dark:text-sage' : 'text-foreground',
        )}
      >
        {isEarned ? '+' : '−'}{fmtCredits(entry.credits)}
      </td>
      <td className="px-3 py-2 text-right text-muted-foreground whitespace-nowrap">
        {relativeTime(entry.created_at)}
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Skeleton / loading placeholder (credit sections only)
// ---------------------------------------------------------------------------

function CreditSkeleton({ rows = 2 }: { rows?: number }) {
  return (
    <div className="rounded-xl border border-border/60 bg-card p-4 space-y-3 animate-pulse" aria-busy="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex gap-3">
          <div className="h-4 w-24 rounded bg-muted" />
          <div className="h-4 w-16 rounded bg-muted" />
          <div className="ml-auto h-4 w-12 rounded bg-muted" />
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section header (consistent italic-serif treatment)
// ---------------------------------------------------------------------------

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="mb-3 font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
      {children}
    </h2>
  );
}

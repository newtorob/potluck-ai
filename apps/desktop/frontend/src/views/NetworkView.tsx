/**
 * NetworkView - /network route.
 *
 * The "see the co-op work" surface. Four sections:
 *
 *   1. Aggregate fleet card - peers online, models served, jobs/tokens
 *      in the last 24h.
 *   2. Available models chip rack - one chip per slug the network serves.
 *   3. Your usage card - requester-side stats for the last 24h.
 *   4. Credit balance - earned / spent / net from the Phase G ledger,
 *      polled every 30s.
 *   5. Credit history - the 10 most-recent ledger entries.
 *
 * Privacy invariant:
 *   No peer_id, hostname, hardware specs, or per-request peer attribution
 *   surface here. Aggregate-only.
 *
 * Credit polling: balance refreshes every 30s; history refreshes alongside
 * it. Both pause on tab blur (same pattern as fleet polling).
 */

import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Globe2, Layers, Activity, Cpu, BarChart3, TrendingUp, TrendingDown, Minus, type LucideIcon } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import { useChatStore } from '@/stores/chatStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { registerContributorDeviceKey } from '@/lib/deviceKey';
import {
  contributorStatusLine,
  effectiveAudience,
  type ContributorAudience,
  type ContributorStatus,
} from '@/lib/contributorStatus';
import { FEATURES } from '@/lib/features';
import type { FleetSummary, MyJobStats, CreditBalance, CreditHistory, CreditHistoryEntry, ContributionsResponse } from '@/lib/localApi';
import { relativeTime } from '@/lib/timeFormat';
import { formatModelName } from '@/lib/modelName';
import { ViewHeader } from '@/components/layout/ViewHeader';
import {
  classifyCoordinatorError,
  defaultCoordinatorErrorCopy,
  type CoordinatorFailureState,
} from '@/lib/coordinatorError';

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
  const [contributions, setContributions] = useState<ContributionsResponse | null>(null);
  const [fleetError, setFleetError] = useState<string | null>(null);
  // Only the setter is read now (the fleet sections render null-safe, and an
  // empty pool shows the first-peer panel rather than a full-page empty state).
  const [, setIsFetchingFleet] = useState(true);

  const [balance, setBalance] = useState<CreditBalance | null>(null);
  const [history, setHistory] = useState<CreditHistory | null>(null);
  const [creditError, setCreditError] = useState<CoordinatorFailureState | null>(null);
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
      setCreditError(classifyCoordinatorError(err));
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

  // Credit polling lifecycle (separate timer - 30s cadence).
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

  // Per-machine contribution receipts (own machines only). Best-effort: a
  // coordinator that's down just leaves the card empty.
  useEffect(() => {
    let cancelled = false;
    const load = () => {
      localApi.network
        .myContributions(7)
        .then((c) => {
          if (!cancelled) setContributions(c);
        })
        .catch(() => {
          /* coordinator unreachable; card stays empty */
        });
    };
    load();
    const timer = setInterval(() => {
      if (!document.hidden) load();
    }, CREDIT_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const onlineNow = fleet?.peers.online_now ?? 0;
  const hasPeers = onlineNow > 0;

  // ---- error state -------------------------------------------------------
  // Note: an empty pool (no other peers) is not an empty state - we still show
  // the member's own usage, contributions, and credits below, plus a
  // "be the first peer" panel in place of the aggregate fleet card. Collapsing
  // the whole page to a single "no peers" message hid everything that is
  // actually about you.

  // ---- normal state ------------------------------------------------------

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <NetworkHeader fleet={fleet} balance={balance} unavailable={fleetError !== null} />

      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-6 space-y-8">
        {fleetError && (
          <div role="alert" className="rounded-lg border border-border bg-card px-4 py-3 text-sm text-muted-foreground">
            Network status is unavailable. Local chat and your contribution controls are still available.
            <button type="button" onClick={() => void fetchFleetRef.current()} className="ml-2 rounded underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Retry</button>
          </div>
        )}
        {/* Contribute toggle - the persistent home for contributor_mode.
            Onboarding sets it too, but this is where a member turns serving on
            or off after setup (household + open-pool requests both gate on it). */}
        <section aria-label="Contribute compute">
          <SectionHeader>Contribute compute</SectionHeader>
          <ContributorToggleCard />
        </section>

        {/* Pool state. When no other peers are online, show a useful
            "be the first peer" panel instead of a wall of zeros; the member's
            own sections below still render either way. */}
        {fleet !== null && !hasPeers ? (
          <FirstPeerPanel />
        ) : fleet !== null ? (
          <>
            {/* Aggregate fleet card */}
            <section aria-label="Aggregate fleet stats">
              <SectionHeader>The network right now</SectionHeader>
              <FleetCard fleet={fleet} />
            </section>

            {/* Available models - pick which one network requests use */}
            <section aria-label="Available models">
              <SectionHeader>Models the network can serve</SectionHeader>
              <ModelChips models={fleet?.models ?? []} />
            </section>
          </>
        ) : null}

        {/* Your usage */}
        <section aria-label="Your usage">
          <SectionHeader>Your usage in the last 24 hours</SectionHeader>
          <MyUsageCard stats={myStats} />
        </section>

        {/* Your contributions */}
        <section aria-label="Your contributions">
          <SectionHeader>What your machines served this week</SectionHeader>
          <ContributionsCard data={contributions} />
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
          className="pt-2 text-xs text-muted-foreground max-w-2xl"
          aria-label="Privacy note"
        >
          The network numbers above are aggregate. We never show which specific peer
          served which prompt, by design. That's the manifesto's blinded-routing rule.
          Credit rows reflect your own ledger only; hash-chain verification
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
  unavailable = false,
}: {
  unavailable?: boolean;
  fleet: FleetSummary | null;
  balance?: CreditBalance | null;
}) {
  const onlineNow = fleet?.peers.online_now ?? 0;
  const isAlive = onlineNow > 0;

  // Net balance pill - only show when we have data.
  const netPill = balance !== null && balance !== undefined ? (
    <NetBalancePill net={balance.net} />
  ) : null;

  return (
    <ViewHeader
      eyebrow="Member-operated"
      title="Network"
      actions={<Link to="/circles" className="rounded-md border border-border px-3 py-2 text-xs font-medium text-foreground hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Trusted circles</Link>}
      description={
        <span className="flex flex-col gap-1">
          <span>Compute other members are sharing right now, beyond your own machines.</span>
          <span className="flex items-center gap-3">
            <span className="flex items-center gap-1.5">
              <span
                className={cn(
                  'inline-block h-2 w-2 rounded-full',
                  isAlive ? 'bg-sage animate-hearth-pulse' : 'bg-muted-foreground/40',
                )}
                aria-hidden="true"
              />
              {unavailable ? 'Network status unavailable' : fleet === null ? 'Checking network…' : isAlive
                ? `${onlineNow} member${onlineNow === 1 ? '' : 's'} online`
                : 'Quiet, no members online right now'}
            </span>
            {netPill}
          </span>
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
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium tabular-nums',
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
// First-peer panel - shown when the pool has no other members online. This is
// the common state before launch, so it must be useful, not a dead end.
// ---------------------------------------------------------------------------

function FirstPeerPanel() {
  return (
    <section aria-label="Be the first peer">
      <div className="rounded-xl border border-border/60 bg-card p-6">
        <div className="flex items-start gap-4">
          <div
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-primary/30 bg-primary/10 text-primary"
            aria-hidden="true"
          >
            <Globe2 className="h-5 w-5" />
          </div>
          <div className="space-y-3">
            <div className="space-y-1">
              <h3 className="font-serif text-lg tracking-display text-foreground">
                The network is quiet right now
              </h3>
              <p className="max-w-xl text-sm text-muted-foreground">
                No other members are online yet. This page fills in as contributors
                join: how many are online, the available models, and the jobs and tokens
                flowing across the network.
              </p>
            </div>
            <p className="max-w-xl text-sm text-muted-foreground">
              To offer this machine's loaded model{FEATURES.openPoolContributor ? '' : ' to your own machines'}, enable{' '}
              <span className="font-medium text-foreground">Contribute compute</span> above.
              To ask the network for an answer, choose a network scope in Chat.
              Your own usage, contributions, and credits are below.
            </p>
          </div>
        </div>
      </div>
    </section>
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
        label="Members online"
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
      ? 'n/a'
      : formatLarge && value >= 1000
        ? `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k`
        : value.toLocaleString();
  // An idle/zero metric should read quiet, not broken - mute it so a fresh
  // network doesn't greet the user with bleak-loud zeros.
  const isIdle = value === 0 || value === undefined;
  return (
    <div
      className={cn(
        'rounded-xl border bg-card p-4',
        emphasize && !isIdle ? 'border-primary/30' : 'border-border/60',
      )}
    >
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Icon className={cn('h-3 w-3', emphasize && !isIdle ? 'text-primary' : 'text-muted-foreground')} aria-hidden="true" />
        <span className="font-serif italic tracking-hearth-label">
          {label}
        </span>
      </div>
      <p
        className={cn(
          'mt-1 font-serif tabular-nums leading-tight tracking-display text-3xl',
          isIdle ? 'text-muted-foreground' : emphasize ? 'text-primary' : 'text-foreground',
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
  const networkModelSlug = useChatStore((s) => s.networkModelSlug);
  const setNetworkModelSlug = useChatStore((s) => s.setNetworkModelSlug);

  if (models.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No models currently advertised. Peers report what they serve when they
        connect; this populates as contributors come online.
      </p>
    );
  }
  return (
    <div className="space-y-2">
      <p className="text-xs text-muted-foreground">
        Pick the model your network requests should use.
      </p>
      <div className="flex flex-wrap gap-2">
        {models.map((m) => {
          const active = m.slug === networkModelSlug;
          return (
            <button
              key={m.slug}
              type="button"
              aria-pressed={active}
              onClick={() => setNetworkModelSlug(m.slug)}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors',
                active
                  ? 'border-primary bg-primary/10 text-foreground'
                  : 'border-border/60 bg-card text-foreground hover:border-border',
              )}
              title={`${m.online_peer_count} peer${m.online_peer_count === 1 ? '' : 's'} currently serve ${m.slug}`}
            >
              <span>{formatModelName(m.slug)}</span>
              <span
                className={cn(
                  'rounded-full px-1.5 text-xs font-medium tabular-nums',
                  m.online_peer_count > 0
                    ? 'bg-sage/20 text-sage-foreground dark:text-sage'
                    : 'bg-muted text-muted-foreground',
                )}
                aria-label={`${m.online_peer_count} peer${m.online_peer_count === 1 ? '' : 's'}`}
              >
                {m.online_peer_count}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Your usage
// ---------------------------------------------------------------------------

function MyUsageCard({ stats }: { stats: MyJobStats | null }) {
  if (stats === null) {
    return (
      <p className="text-sm text-muted-foreground">
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

// ---------------------------------------------------------------------------
// Contribution receipts - per-machine "what my machines served this week"
// ---------------------------------------------------------------------------

/** Map a machine's canary reputation to an honest standing label. */
function standing(reputation: number | null): { label: string; cls: string } {
  if (reputation === null) return { label: 'New', cls: 'text-muted-foreground' };
  if (reputation <= 0) return { label: 'Flagged', cls: 'text-destructive' };
  if (reputation >= 0.95) return { label: 'Trusted', cls: 'text-sage' };
  return { label: 'Building', cls: 'text-muted-foreground' };
}

function ContributionsCard({ data }: { data: ContributionsResponse | null }) {
  if (data === null) {
    return (
      <p className="text-sm text-muted-foreground">Loading your machines' activity...</p>
    );
  }
  if (data.machines.length === 0) {
    return (
      <div className="rounded-xl border border-border/60 bg-card p-4 text-sm text-muted-foreground">
        None of your machines served a request in the last {data.window_days} days.
      </div>
    );
  }
  return (
    <div className="overflow-hidden rounded-xl border border-border/60 bg-card">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border/60 text-left text-xs uppercase tracking-hearth-label text-muted-foreground">
            <th className="px-4 py-2 font-medium">Machine</th>
            <th className="px-4 py-2 text-left font-medium">Standing</th>
            <th className="px-4 py-2 text-right font-medium">Requests</th>
            <th className="px-4 py-2 text-right font-medium">Tokens</th>
            <th className="px-4 py-2 text-right font-medium">Credits</th>
          </tr>
        </thead>
        <tbody>
          {data.machines.map((m) => (
            <tr key={m.peer_id} className="border-b border-border/40 last:border-0">
              <td className="px-4 py-2 text-foreground">
                {m.hostname ?? (
                  <span className="font-mono text-xs text-muted-foreground">{m.peer_id.slice(0, 8)}</span>
                )}
              </td>
              <td className={cn('px-4 py-2 text-xs font-medium', standing(m.reputation).cls)}>
                {standing(m.reputation).label}
              </td>
              <td className="px-4 py-2 text-right tabular-nums text-foreground">
                {m.requests_served.toLocaleString()}
              </td>
              <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">
                {m.output_tokens.toLocaleString()}
              </td>
              <td className="px-4 py-2 text-right tabular-nums text-sage">
                {fmtCredits(m.credits_earned)}
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t border-border/60 text-xs text-muted-foreground">
            <td className="px-4 py-2 font-medium">Total</td>
            <td className="px-4 py-2" />
            <td className="px-4 py-2 text-right tabular-nums">{data.total_requests_served.toLocaleString()}</td>
            <td className="px-4 py-2 text-right tabular-nums">{data.total_output_tokens.toLocaleString()}</td>
            <td className="px-4 py-2 text-right tabular-nums">{fmtCredits(data.total_credits_earned)}</td>
          </tr>
        </tfoot>
      </table>
      <p className="border-t border-border/40 px-4 py-2 text-xs text-muted-foreground">
        Credits are earned by serving other members. Requests you run on your own machines do not earn credits.
      </p>
    </div>
  );
}

function SmallStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p
        className="font-sans text-xs font-medium text-muted-foreground tracking-hearth-label"
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
// Credit error note - shared by the balance card and history list. Classified
// so a 401 (session expired) reads as "sign in," not "coordinator unreachable."
// ---------------------------------------------------------------------------

function CreditErrorNote({ error }: { error: CoordinatorFailureState }) {
  if (error.kind === 'unauthenticated') {
    return (
      <p className="text-sm text-muted-foreground">
        You're signed out.{' '}
        <Link to="/membership" className="underline underline-offset-2 hover:text-foreground">
          Sign in
        </Link>{' '}
        to see your credits.
      </p>
    );
  }
  const copy = defaultCoordinatorErrorCopy(error);
  return <p className="text-sm text-muted-foreground">{copy.title}. {copy.description}</p>;
}

// ---------------------------------------------------------------------------
// Credit balance card
// ---------------------------------------------------------------------------

interface CreditBalanceCardProps {
  balance: CreditBalance | null;
  isLoading: boolean;
  error: CoordinatorFailureState | null;
}

function CreditBalanceCard({ balance, isLoading, error }: CreditBalanceCardProps) {
  if (isLoading && balance === null) {
    return <CreditSkeleton />;
  }

  if (error !== null && balance === null) {
    return <CreditErrorNote error={error} />;
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
          No activity yet. Choose a network scope in Chat to request an answer
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
          <p className="font-sans text-xs font-medium text-muted-foreground tracking-hearth-label">
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
          <p className="mb-2 font-sans text-xs font-medium text-muted-foreground tracking-hearth-label">
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
  error: CoordinatorFailureState | null;
}

function CreditHistoryList({ history, isLoading, error }: CreditHistoryListProps) {
  if (isLoading && history === null) {
    return <CreditSkeleton rows={4} />;
  }

  if (error !== null && history === null) {
    return <CreditErrorNote error={error} />;
  }

  if (history === null || history.entries.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No credit history yet. Your ledger entries will appear here once
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
              className="px-3 py-2 text-left font-serif italic text-xs text-muted-foreground font-normal tracking-hearth-label"
              scope="col"
            >
              Direction
            </th>
            <th
              className="px-3 py-2 text-left font-serif italic text-xs text-muted-foreground font-normal tracking-hearth-label"
              scope="col"
            >
              Model
            </th>
            <th
              className="px-3 py-2 text-right font-serif italic text-xs text-muted-foreground font-normal tracking-hearth-label"
              scope="col"
            >
              Tokens
            </th>
            <th
              className="px-3 py-2 text-right font-serif italic text-xs text-muted-foreground font-normal tracking-hearth-label"
              scope="col"
            >
              Credits
            </th>
            <th
              className="px-3 py-2 text-right font-serif italic text-xs text-muted-foreground font-normal tracking-hearth-label"
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
            'inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-xs font-medium',
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
    <h2 className="mb-3 font-sans text-xs font-medium text-muted-foreground tracking-hearth-label">
      {children}
    </h2>
  );
}

/**
 * ContributorToggleCard - the persistent contributor_mode switch.
 *
 * Wired to the same NetworkSettings.contributor_mode boolean the onboarding
 * "Contribute" step writes. Until this existed, a member who declined during
 * setup had no in-app way to start serving (household "My machines" routing
 * and open-pool serving both gate on this flag). Enabling it also registers
 * this machine's contributor device key, mirroring ConnectStep.
 */
const CONTRIBUTOR_STATUS_POLL_MS = 10_000;

const AUDIENCE_CHOICES: { value: ContributorAudience; label: string; detail: string }[] = [
  {
    value: 'mine',
    label: 'Only my own machines',
    detail: 'Nobody else can send requests here.',
  },
  {
    value: 'circles',
    label: 'My trusted circles too',
    detail:
      'People in a trusted circle with you can send requests here. This machine sees their conversation in order to answer. It is never offered open pool work.',
  },
  {
    value: 'everyone',
    label: 'Everyone on the network',
    detail: 'Your trusted circles and the open pool.',
  },
];

function ContributorToggleCard() {
  const settings = useSettingsStore((s) => s.settings);
  const isSaving = useSettingsStore((s) => s.isSaving);
  const updateSettings = useSettingsStore((s) => s.update);
  const [status, setStatus] = useState<ContributorStatus | null>(null);

  const contributorMode = settings?.network.contributor_mode ?? false;
  const audience = effectiveAudience(settings?.network.contributor_audience, FEATURES.openPoolContributor);

  // While the toggle is on, the sidecar's in-process contributor is what does
  // the work; show what it is doing. Polled, not pushed: the status is a few
  // counters and a state word, and 10 s is well inside a heartbeat interval.
  useEffect(() => {
    if (!contributorMode) {
      setStatus(null);
      return;
    }
    let cancelled = false;
    const tick = async () => {
      try {
        const next = await localApi.getContributorStatus();
        if (!cancelled) setStatus(next);
      } catch {
        // Sidecar hiccup: keep the last line rather than flashing.
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), CONTRIBUTOR_STATUS_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [contributorMode]);

  async function handleToggle(next: boolean) {
    try {
      await updateSettings({ network: { contributor_mode: next } });
    } catch {
      // Rollback is handled by the store.
      return;
    }
    if (next) {
      // Pre-register the contributor device key so attestation isn't rejected
      // later. Best-effort, same as ConnectStep - never block the toggle.
      try {
        await registerContributorDeviceKey();
      } catch (err) {
        console.warn('[NetworkView] device key registration failed:', err);
      }
      // Refresh right away so the line moves from Starting within a second.
      try {
        setStatus(await localApi.getContributorStatus());
      } catch {
        /* the poll above catches up */
      }
    }
  }

  async function handleAudience(next: ContributorAudience) {
    try {
      await updateSettings({ network: { contributor_audience: next } });
    } catch {
      // Rollback is handled by the store.
      return;
    }
    if (next !== 'mine') {
      // The coordinator has to know this machine before it can share it.
      try {
        await registerContributorDeviceKey();
      } catch (err) {
        console.warn('[NetworkView] device key registration failed:', err);
      }
    }
    try {
      setStatus(await localApi.getContributorStatus());
    } catch {
      /* the poll above catches up */
    }
  }

  // The engine joins the coordinator only when the machine is shared beyond
  // its owner, so that is when there is a connection to report on.
  const line = audience !== 'mine' ? contributorStatusLine(status) : null;

  return (
    <div className="rounded-lg border border-border bg-card/60 p-4">
      {settings === null ? (
        <p className="text-sm text-muted-foreground">
          Settings are still loading. Try again in a moment.
        </p>
      ) : (
        <label className="flex items-start gap-3 cursor-pointer select-none">
          <Cpu className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <div className="flex-1 space-y-0.5">
            <p className="text-sm text-foreground">
              Share spare compute with your network
            </p>
            <p className="text-xs leading-relaxed text-muted-foreground">
              {FEATURES.openPoolContributor
                ? 'While enabled, this machine offers its loaded model to your own machines and eligible network requests.'
                : 'While enabled, this machine can answer requests from your own machines using its loaded model.'}{' '}
              Turn it off when you need the resources for other work. It does not
              automatically pause when you are active.
              Choosing a network scope in Chat is separate: it requests compute from others.
            </p>
            {line !== null ? (
              <p
                role="status"
                aria-live="polite"
                className={cn(
                  'text-xs',
                  line.tone === 'ok' && 'text-foreground',
                  line.tone === 'pending' && 'text-muted-foreground',
                  line.tone === 'warn' && 'text-destructive',
                )}
              >
                {line.text}
              </p>
            ) : null}
          </div>
          <input
            type="checkbox"
            role="switch"
            aria-label="Share spare compute with your network"
            checked={contributorMode}
            disabled={isSaving}
            onChange={(e) => void handleToggle(e.target.checked)}
            className="mt-0.5 h-4 w-4 shrink-0 accent-primary disabled:cursor-not-allowed disabled:opacity-60"
          />
        </label>
      )}
      {settings !== null && contributorMode ? (
        <fieldset disabled={isSaving} className="mt-3 space-y-2 border-t border-border/60 pt-3 pl-7">
          <legend className="sr-only">Who can use this machine</legend>
          <p className="text-xs font-medium text-foreground" aria-hidden="true">
            Who can use this machine
          </p>
          {AUDIENCE_CHOICES.filter((c) => c.value !== 'everyone' || FEATURES.openPoolContributor).map((choice) => (
            <label key={choice.value} className="flex cursor-pointer items-start gap-2 text-xs">
              <input
                type="radio"
                name="contributor-audience"
                checked={audience === choice.value}
                onChange={() => void handleAudience(choice.value)}
                className="mt-0.5 shrink-0 accent-primary disabled:cursor-not-allowed"
              />
              <span>
                <span className="text-foreground">{choice.label}</span>
                <span className="block leading-relaxed text-muted-foreground">{choice.detail}</span>
              </span>
            </label>
          ))}
        </fieldset>
      ) : null}
    </div>
  );
}

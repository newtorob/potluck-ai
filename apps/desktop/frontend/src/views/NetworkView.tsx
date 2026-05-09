/**
 * NetworkView — /network route.
 *
 * The "see the co-op work" surface. Three sections:
 *
 *   1. Aggregate fleet card — peers online, models served, jobs/tokens
 *      in the last 24h. Sage-tinted "alive" dot when peers >= 1.
 *   2. Available models chip rack — one chip per slug the network
 *      currently serves, with a small per-slug peer count.
 *   3. Your usage card — your requester-side stats for the last 24h.
 *
 * Privacy invariant (memory 42a7e671 / b3cbfba9):
 *   NO peer_id, hostname, hardware specs, or per-request peer
 *   attribution surface here. Aggregate-only. The "I'm serving" card
 *   from the original brief is deferred until we have a clean way to
 *   identify the user's own contributor without exposing identity to
 *   anyone else.
 *
 * Polls /transparency/fleet + /jobs/stats every 5s while the document
 * is visible. Pauses on tab blur (matches /agents pattern).
 */

import { useEffect, useRef, useState } from 'react';
import { Globe2, Layers, Activity, Cpu, BarChart3 } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import type { FleetSummary, MyJobStats } from '@/lib/localApi';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';

const POLL_INTERVAL_MS = 5_000;

export function NetworkView() {
  const [fleet, setFleet] = useState<FleetSummary | null>(null);
  const [myStats, setMyStats] = useState<MyJobStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isFetching, setIsFetching] = useState(true);

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Wrap in refs so the polling closure always reads the live fetcher.
  const fetchAllRef = useRef<() => Promise<void>>(async () => {});
  fetchAllRef.current = async () => {
    try {
      const [f, s] = await Promise.all([
        localApi.network.fleet(),
        localApi.network.myStats().catch(() => null),
      ]);
      setFleet(f);
      setMyStats(s);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network fetch failed');
    } finally {
      setIsFetching(false);
    }
  };

  // Initial fetch + polling lifecycle.
  useEffect(() => {
    void fetchAllRef.current();
    function schedule() {
      timerRef.current = setTimeout(() => {
        if (!document.hidden) void fetchAllRef.current();
        schedule();
      }, POLL_INTERVAL_MS);
    }
    schedule();

    function onVisibility() {
      if (!document.hidden) void fetchAllRef.current();
    }
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  const onlineNow = fleet?.peers.online_now ?? 0;
  const hasPeers = onlineNow > 0;

  // ---- empty state -------------------------------------------------------

  if (!isFetching && fleet !== null && !hasPeers) {
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
              machine joins the cooperative.
            </>
          }
        />
      </div>
    );
  }

  // ---- error state -------------------------------------------------------

  if (error !== null && fleet === null) {
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
      <NetworkHeader fleet={fleet} />

      <div className="flex-1 overflow-y-auto px-6 py-6 space-y-8">
        {/* Aggregate fleet card */}
        <section aria-label="Aggregate fleet stats">
          <SectionHeader>The cooperative right now</SectionHeader>
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

        {/* Privacy footer */}
        <p
          className="pt-2 text-[11px] text-muted-foreground/70 max-w-2xl"
          aria-label="Privacy note"
        >
          The numbers above are aggregate. We never show which specific peer
          served which prompt, by design — that's the manifesto's blinded-routing
          rule. To audit your own routing you can read the coordinator log
          directly; that's your machine reporting back to you.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

function NetworkHeader({ fleet }: { fleet: FleetSummary | null }) {
  const onlineNow = fleet?.peers.online_now ?? 0;
  const isAlive = onlineNow > 0;
  return (
    <ViewHeader
      eyebrow="Cooperative"
      title="Network"
      description={
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
      }
    />
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
          fleet && fleet.models.length > 0
            ? fleet.models[0]
              ? `top: ${fleet.models[0].slug.split(':')[0]}`
              : undefined
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
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: number | undefined;
  sub?: string;
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
  return (
    <div
      className={cn(
        'rounded-xl border bg-card p-4',
        emphasize ? 'border-primary/30' : 'border-border/60',
      )}
    >
      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <Icon className={cn('h-3 w-3', emphasize ? 'text-primary' : 'text-muted-foreground')} aria-hidden="true" />
        <span className="font-serif italic" style={{ letterSpacing: '0.04em' }}>
          {label}
        </span>
      </div>
      <p
        className={cn(
          'mt-1 font-serif tabular-nums leading-tight tracking-display',
          emphasize ? 'text-3xl text-primary' : 'text-3xl text-foreground',
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
          <span className="font-mono text-foreground">{m.slug}</span>
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
        className="font-serif text-[11px] italic text-muted-foreground"
        style={{ letterSpacing: '0.04em' }}
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
// Section header (consistent italic-serif treatment)
// ---------------------------------------------------------------------------

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <h2
      className="mb-3 font-serif text-[11px] italic text-muted-foreground"
      style={{ letterSpacing: '0.04em' }}
    >
      {children}
    </h2>
  );
}

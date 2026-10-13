/**
 * UsageView - /usage route.
 *
 * A local, privacy-preserving "your usage at a glance" view. Everything shown
 * is computed and stored on-device in the sidecar (services/usage_store.py);
 * nothing is ever uploaded, and only metadata is recorded (tokens, latency,
 * scope, model, ok/fail), never prompt or response content.
 *
 * Two jobs:
 *   1. Retention - the streak + activity heatmap pull people back.
 *   2. Measurement - the consumed-vs-served split by scope answers "do people
 *      leave local-only for the network scopes?"
 *
 * The UI leads with the two things no cloud or CLI competitor can match:
 * local-only storage, and the consumed-vs-served symmetry (the "served" half
 * only exists because every member can be both supply and demand).
 */

import { useEffect, useRef, useState } from 'react';
import { ShieldCheck, BarChart3 } from 'lucide-react';
import { localApi } from '@/lib/localApi';
import type {
  UsageSummary,
  UsageActivityDay,
  UsageModel,
  UsagePace,
  UsageSplit,
  UsageScope,
} from '@/lib/localApi';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';

const REFRESH_MS = 15_000;

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

/** Compact token/number formatting: 1_450 -> "1.4K", 2_300_000 -> "2.3M". */
function compact(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}K`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}

function ms(n: number | null | undefined): string {
  if (!n) return 'n/a';
  if (n < 1000) return `${Math.round(n)}ms`;
  return `${(n / 1000).toFixed(1)}s`;
}

function pct(n: number): string {
  return `${Math.round(n * 100)}%`;
}

const SCOPE_LABEL: Record<UsageScope, string> = {
  local: 'This machine',
  my_machines: 'My machines',
  circle: 'Trusted circle',
  pool: 'Open pool',
};

// A calm ember-to-warm ramp for the heatmap, matching the Hearth palette. Index
// 0 is the empty cell; higher indexes are hotter days.
const HEAT_CLASSES = [
  'bg-foreground/[0.06]',
  'bg-primary/25',
  'bg-primary/45',
  'bg-primary/70',
  'bg-primary',
];

// ---------------------------------------------------------------------------
// View
// ---------------------------------------------------------------------------

export function UsageView() {
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [activity, setActivity] = useState<UsageActivityDay[]>([]);
  const [models, setModels] = useState<UsageModel[]>([]);
  const [pace, setPace] = useState<UsagePace | null>(null);
  const [split, setSplit] = useState<UsageSplit | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(false);

  const load = useRef(async () => {
    try {
      const [s, a, m, p, sp] = await Promise.all([
        localApi.usage.summary(30),
        localApi.usage.activity(365),
        localApi.usage.models(8),
        localApi.usage.pace(),
        localApi.usage.split(30),
      ]);
      setSummary(s);
      setActivity(a);
      setModels(m);
      setPace(p);
      setSplit(sp);
      setError(false);
    } catch {
      setError(true);
    } finally {
      setLoaded(true);
    }
  });

  useEffect(() => {
    void load.current();
    const timer = setInterval(() => {
      if (!document.hidden) void load.current();
    }, REFRESH_MS);
    const onVisible = () => {
      if (!document.hidden) void load.current();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, []);

  const hasAnything = (summary?.requests ?? 0) > 0 || (split?.served_total ?? 0) > 0;

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <ViewHeader
        title="Usage"
        eyebrow="Your activity"
        description="Computed and stored on this machine. Nothing here is ever uploaded."
      />

      <div className="flex-1 overflow-y-auto px-6 py-6 space-y-8">
        {error && summary === null ? (
          <EmptyState
            icon={BarChart3}
            title="Usage is not available"
            description="The local usage store could not be reached. It comes back on its own once the sidecar is up."
          />
        ) : !loaded ? (
          <p className="font-sans text-sm text-muted-foreground">Loading your usage…</p>
        ) : !hasAnything ? (
          <EmptyState
            icon={BarChart3}
            title="No usage yet"
            description="Send a chat or serve a request and this fills in. Everything is measured on this machine and stays here."
          />
        ) : (
          <>
            {/* Hero: total tokens proxied */}
            <section aria-label="Total tokens">
              <p className="font-mono text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                Tokens proxied · last 30 days
              </p>
              <p className="mt-1 font-serif text-5xl font-light tracking-tight text-foreground">
                {compact(summary?.total_tokens ?? 0)}
              </p>
              <p className="mt-1 flex items-center gap-1.5 font-mono text-[11px] text-muted-foreground">
                <ShieldCheck className="h-3.5 w-3.5 text-sage" aria-hidden="true" />
                Local only. Never leaves this device.
              </p>
            </section>

            {/* Stat strip */}
            <section aria-label="Summary stats">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                <StatTile label="Requests" value={compact(summary?.requests ?? 0)} />
                <StatTile label="Sessions" value={compact(summary?.sessions ?? 0)} />
                <StatTile label="Active days" value={String(summary?.active_days ?? 0)} />
                <StatTile
                  label="Streak"
                  value={`${summary?.active_streak ?? 0}d`}
                  accent={(summary?.active_streak ?? 0) > 1}
                />
                <StatTile label="Success" value={pct(summary?.success_rate ?? 1)} />
                <StatTile label="p95 latency" value={ms(summary?.p95_latency_ms)} />
              </div>
            </section>

            {/* Consumed vs Served - the differentiator, led with */}
            {split && (
              <section aria-label="Consumed versus served">
                <SectionHeader>What you used vs what you served</SectionHeader>
                <ConsumedVsServed split={split} />
              </section>
            )}

            {/* Activity heatmap */}
            <section aria-label="Activity">
              <SectionHeader>Activity, past year</SectionHeader>
              <Heatmap days={activity} />
            </section>

            {/* Token mix + top models, side by side on wide screens */}
            <div className="grid gap-8 lg:grid-cols-2">
              <section aria-label="Token mix">
                <SectionHeader>Token mix</SectionHeader>
                <TokenMix summary={summary} />
              </section>

              <section aria-label="Most used models">
                <SectionHeader>Most-used models</SectionHeader>
                <ModelBars models={models} />
              </section>
            </div>

            {/* Pace */}
            {pace && (
              <section aria-label="Recent pace">
                <SectionHeader>Recent pace</SectionHeader>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <StatTile label="Last hour" value={compact(pace.last_hour_tokens)} sub="tokens" />
                  <StatTile label="Last 24h" value={compact(pace.last_24h_tokens)} sub="tokens" />
                  <StatTile
                    label="Week over week"
                    value={pace.wow_change_pct === null ? 'n/a' : `${pace.wow_change_pct > 0 ? '+' : ''}${pace.wow_change_pct}%`}
                    accent={pace.wow_change_pct !== null && pace.wow_change_pct > 0}
                  />
                  <StatTile label="Avg first token" value={ms(pace.avg_ttft_ms)} />
                </div>
              </section>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pieces
// ---------------------------------------------------------------------------

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="mb-3 font-serif text-[11px] italic text-muted-foreground tracking-[0.08em]">
      {children}
    </h2>
  );
}

function StatTile({
  label,
  value,
  sub,
  accent = false,
}: {
  label: string;
  value: string;
  sub?: string;
  accent?: boolean;
}) {
  return (
    <div className="rounded-lg border border-border bg-card/60 px-3.5 py-3">
      <p className="font-mono text-[10px] uppercase tracking-[0.1em] text-muted-foreground">{label}</p>
      <p className={`mt-1 font-serif text-xl font-light ${accent ? 'text-primary' : 'text-foreground'}`}>
        {value}
      </p>
      {sub && <p className="font-mono text-[10px] text-muted-foreground">{sub}</p>}
    </div>
  );
}

/** The symmetric consumed-vs-served panel, split by scope. This is the half no
 *  cloud or CLI competitor can render. */
function ConsumedVsServed({ split }: { split: UsageSplit }) {
  const scopes: UsageScope[] = ['local', 'my_machines', 'circle', 'pool'];
  const consumedMax = Math.max(1, ...scopes.map((s) => split.consumed[s].tokens));
  const servedMax = Math.max(1, ...scopes.map((s) => split.served[s].tokens));

  return (
    <div className="rounded-lg border border-border bg-card/60 p-4">
      <div className="grid gap-6 sm:grid-cols-2">
        {/* Consumed */}
        <div>
          <div className="mb-2 flex items-baseline justify-between">
            <span className="font-mono text-[11px] uppercase tracking-[0.1em] text-foreground">
              You used
            </span>
            <span className="font-serif text-lg font-light text-foreground">
              {compact(split.consumed_total)}
            </span>
          </div>
          <div className="space-y-1.5">
            {scopes.map((s) => (
              <ScopeBar
                key={s}
                label={SCOPE_LABEL[s]}
                tokens={split.consumed[s].tokens}
                max={consumedMax}
                tone="ember"
              />
            ))}
          </div>
        </div>

        {/* Served */}
        <div>
          <div className="mb-2 flex items-baseline justify-between">
            <span className="font-mono text-[11px] uppercase tracking-[0.1em] text-foreground">
              You served for others
            </span>
            <span className="font-serif text-lg font-light text-foreground">
              {compact(split.served_total)}
            </span>
          </div>
          <div className="space-y-1.5">
            {scopes.map((s) => (
              <ScopeBar
                key={s}
                label={SCOPE_LABEL[s]}
                tokens={split.served[s].tokens}
                max={servedMax}
                tone="sage"
              />
            ))}
          </div>
        </div>
      </div>
      {split.served_total === 0 && (
        <p className="mt-4 font-sans text-xs leading-relaxed text-muted-foreground">
          You have not served any requests yet. Turn on Contribute compute in Network to serve your
          household and the open pool, and this side fills in.
        </p>
      )}
    </div>
  );
}

function ScopeBar({
  label,
  tokens,
  max,
  tone,
}: {
  label: string;
  tokens: number;
  max: number;
  tone: 'ember' | 'sage';
}) {
  const width = tokens > 0 ? Math.max(2, (tokens / max) * 100) : 0;
  const barClass = tone === 'ember' ? 'bg-primary' : 'bg-sage';
  return (
    <div className="flex items-center gap-2">
      <span className="w-24 shrink-0 font-mono text-[10px] text-muted-foreground">{label}</span>
      <div className="h-2 flex-1 overflow-hidden rounded-full bg-foreground/[0.06]">
        <div className={`h-full rounded-full ${barClass}`} style={{ width: `${width}%` }} />
      </div>
      <span className="w-12 shrink-0 text-right font-mono text-[10px] text-foreground">
        {compact(tokens)}
      </span>
    </div>
  );
}

/** GitHub-contribution-style heatmap of daily token volume over the past year. */
function Heatmap({ days }: { days: UsageActivityDay[] }) {
  // Build a date -> tokens map, then lay out 53 weeks x 7 days ending today.
  const byDate = new Map<string, number>();
  for (const d of days) byDate.set(d.date, d.tokens);

  const WEEKS = 53;
  const today = new Date();
  // Align the last column to end on today; walk back WEEKS*7 days.
  const cells: { date: string; tokens: number }[] = [];
  const start = new Date(today);
  start.setDate(start.getDate() - (WEEKS * 7 - 1));
  for (let i = 0; i < WEEKS * 7; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    const iso = d.toISOString().slice(0, 10);
    cells.push({ date: iso, tokens: byDate.get(iso) ?? 0 });
  }

  const max = Math.max(1, ...cells.map((c) => c.tokens));
  const bucket = (t: number): number => {
    if (t <= 0) return 0;
    const r = t / max;
    if (r > 0.66) return 4;
    if (r > 0.33) return 3;
    if (r > 0.1) return 2;
    return 1;
  };

  // Column-major: each column is a week (7 day cells top to bottom).
  const columns: { date: string; tokens: number }[][] = [];
  for (let w = 0; w < WEEKS; w++) columns.push(cells.slice(w * 7, w * 7 + 7));

  return (
    <div className="rounded-lg border border-border bg-card/60 p-4">
      <div className="overflow-x-auto">
        <div className="flex gap-[3px]">
          {columns.map((col, ci) => (
            <div key={ci} className="flex flex-col gap-[3px]">
              {col.map((cell) => (
                <div
                  key={cell.date}
                  title={`${cell.date}: ${compact(cell.tokens)} tokens`}
                  className={`h-2.5 w-2.5 rounded-[2px] ${HEAT_CLASSES[bucket(cell.tokens)] ?? HEAT_CLASSES[0]}`}
                />
              ))}
            </div>
          ))}
        </div>
      </div>
      <div className="mt-3 flex items-center gap-1.5">
        <span className="font-mono text-[10px] text-muted-foreground">Less</span>
        {HEAT_CLASSES.map((c, i) => (
          <div key={i} className={`h-2.5 w-2.5 rounded-[2px] ${c}`} />
        ))}
        <span className="font-mono text-[10px] text-muted-foreground">More</span>
      </div>
    </div>
  );
}

function TokenMix({ summary }: { summary: UsageSummary | null }) {
  const input = summary?.input_tokens ?? 0;
  const output = summary?.output_tokens ?? 0;
  const total = Math.max(1, input + output);
  return (
    <div className="rounded-lg border border-border bg-card/60 p-4 space-y-3">
      <MixBar label="Fresh input" value={input} total={total} tone="sage" />
      <MixBar label="Output" value={output} total={total} tone="ember" />
      <p className="font-mono text-[10px] text-muted-foreground">
        Cache reads appear here once prompt caching ships.
      </p>
    </div>
  );
}

function MixBar({
  label,
  value,
  total,
  tone,
}: {
  label: string;
  value: number;
  total: number;
  tone: 'ember' | 'sage';
}) {
  const width = value > 0 ? Math.max(2, (value / total) * 100) : 0;
  const barClass = tone === 'ember' ? 'bg-primary' : 'bg-sage';
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between">
        <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-muted-foreground">
          {label}
        </span>
        <span className="font-mono text-[11px] text-foreground">{compact(value)}</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-foreground/[0.06]">
        <div className={`h-full rounded-full ${barClass}`} style={{ width: `${width}%` }} />
      </div>
    </div>
  );
}

function ModelBars({ models }: { models: UsageModel[] }) {
  if (models.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-card/60 p-4">
        <p className="font-sans text-sm text-muted-foreground">No model activity yet.</p>
      </div>
    );
  }
  const max = Math.max(1, ...models.map((m) => m.tokens));
  return (
    <div className="rounded-lg border border-border bg-card/60 p-4 space-y-2.5">
      {models.map((m) => {
        const width = Math.max(2, (m.tokens / max) * 100);
        return (
          <div key={m.model_slug}>
            <div className="mb-1 flex items-baseline justify-between gap-2">
              <span className="truncate font-mono text-[11px] text-foreground">{m.model_slug}</span>
              <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                {compact(m.tokens)} · {compact(m.requests)} req
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-foreground/[0.06]">
              <div className="h-full rounded-full bg-primary" style={{ width: `${width}%` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

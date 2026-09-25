import type { CliArgs } from "./args.js";
import { runtimeRequest } from "./runtime.js";
import { sanitizePerformance, type PerformanceReport } from "./status.js";
import { plainText } from "./logs.js";

export type UsageView = "overview" | "models" | "requests" | "machines";
export type UsageDirection = "consumed" | "served";
export interface UsageStats {
  requests: number | null; inputTokens: number | null; outputTokens: number | null; failures: number | null;
  avgTtftMs: number | null; avgDurationMs: number | null; p95DurationMs: number | null;
  endToEndTokensPerSecond: number | null; rateSamples: number | null;
}
export interface UsageReport {
  schemaVersion: 1; sampledAt: string; since: string; windowDays: number; direction: UsageDirection;
  totals: UsageStats;
  activity: Array<{ bucket: string; requests: number; outputTokens: number }>;
  bucketUnit: "hour" | "day";
  models: Array<UsageStats & { model: string }>;
  routes: Array<{ scope: string; requests: number; outputTokens: number }>;
  recent: Array<{ id: number; ts: string; model: string; scope: string; inputTokens: number | null;
    outputTokens: number | null; ttftMs: number | null; durationMs: number | null; ok: boolean }>;
  performance: PerformanceReport | null;
  memoryTotalGiB: number | null;
  split: { state: string; workerEnabled: boolean; workerBusy: boolean; workers: number | null; model: string } | null;
}
const object = (v: unknown): Record<string, unknown> => v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {};
const number = (v: unknown): number | null => typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;
const rows = (v: unknown, max: number) => Array.isArray(v) ? v.slice(0, max).map(object) : [];
/** Remote strings must not control a terminal, even in an uncolored snapshot. */
export const cellText = (v: unknown): string => typeof v === "string" ? plainText(v.slice(0, 2048))
  .replace(/[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]/g, " ") : "";
const scope = (v: unknown) => ["local", "my_machines", "circle", "pool"].includes(String(v)) ? String(v) : "unknown";
const stats = (v: unknown): UsageStats => {
  const o = object(v);
  return { requests: number(o.requests), inputTokens: number(o.inputTokens), outputTokens: number(o.outputTokens), failures: number(o.failures),
    avgTtftMs: number(o.avgTtftMs), avgDurationMs: number(o.avgDurationMs), p95DurationMs: number(o.p95DurationMs),
    endToEndTokensPerSecond: number(o.endToEndTokensPerSecond), rateSamples: number(o.rateSamples) };
};
export function sanitizeUsage(value: unknown): UsageReport {
  const v = object(value);
  if (v.schemaVersion !== 1 || !v.totals || ![1, 7, 30, 90].includes(Number(v.windowDays)) ||
      !["consumed", "served"].includes(String(v.direction)) || typeof v.sampledAt !== "string") throw new Error("incompatible_usage");
  const p = sanitizePerformance(object(v.performance));
  if (p) {
    p.local.model = p.local.model === null ? null : cellText(p.local.model);
    p.sampledAt = cellText(p.sampledAt);
    if (p.household) p.household.models = p.household.models.slice(0, 64).map(m => ({ ...m, model: cellText(m.model) }));
  }
  const s = object(v.split);
  return { schemaVersion: 1, sampledAt: cellText(v.sampledAt), since: cellText(v.since), windowDays: Number(v.windowDays), direction: v.direction as UsageDirection,
    totals: stats(v.totals), bucketUnit: v.bucketUnit === "hour" ? "hour" : "day",
    activity: rows(v.activity, 91).map(r => ({ bucket: cellText(r.bucket), requests: number(r.requests) ?? 0, outputTokens: number(r.outputTokens) ?? 0 })),
    models: rows(v.models, 20).map(r => ({ ...stats(r), model: cellText(r.model) || "Unreported model" })),
    routes: rows(v.routes, 4).map(r => ({ scope: scope(r.scope), requests: number(r.requests) ?? 0, outputTokens: number(r.outputTokens) ?? 0 })),
    recent: rows(v.recent, 100).map(r => ({ id: number(r.id) ?? 0, ts: cellText(r.ts), model: cellText(r.model) || "Unreported model", scope: scope(r.scope),
      inputTokens: number(r.inputTokens), outputTokens: number(r.outputTokens), ttftMs: number(r.ttftMs), durationMs: number(r.durationMs), ok: r.ok === 1 || r.ok === true })),
    performance: p, memoryTotalGiB: number(v.memoryTotalGiB),
    split: v.split ? { state: ["idle", "planning", "loading", "ready", "failed", "stopping"].includes(String(s.state)) ? String(s.state) : "unknown",
      workerEnabled: s.workerEnabled === true, workerBusy: s.workerBusy === true, workers: number(s.workers), model: cellText(s.model) } : null };
}
export async function usageSnapshot(args: Pick<CliArgs, "timeout">, window: number, direction: UsageDirection, household: boolean, signal?: AbortSignal): Promise<UsageReport> {
  return sanitizeUsage(await runtimeRequest(`/usage/dashboard?window=${window}&direction=${direction}&household=${household}`, { timeout: args.timeout, signal }));
}

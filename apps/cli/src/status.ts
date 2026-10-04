import { setTimeout as delay } from "node:timers/promises";
import type { CliArgs } from "./args.js";
import { diagnose, formatDiagnostics, type DiagnosticReport } from "./diagnostics.js";
import { runtimeRequest } from "./runtime.js";
import { plainText } from "./logs.js";

export interface PerformanceReport {
  sampledAt: string;
  local: { model: string | null; busy: boolean; contextTokens: number | null; continuousBatch: boolean;
    memoryAvailableGiB: number | null; benchmarkTokensPerSecond: number | null; benchmarkTtftMs: number | null };
  usage: { windowHours: number | null; requests: number | null; reportedOutputTokens: number | null;
    avgTtftMs: number | null; avgDurationMs: number | null; successRate: number | null } | null;
  household: { rosterCount: number | null; checked: number | null; available: number | null; busy: number | null; partial: boolean;
    models: Array<{ model: string; machines: number | null; benchmarkedMachines: number | null; meanBenchmarkTokensPerSecond: number | null }> } | null;
}
const obj = (v: unknown): Record<string, unknown> => v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {};
const number = (v: unknown): number | null => typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;
const string = (v: unknown): string | null => typeof v === "string" ? v : null;
export function sanitizePerformance(value: Record<string, unknown>): PerformanceReport | null {
  if (value.schemaVersion !== 1 || !value.local || typeof value.sampledAt !== "string") return null;
  const l = obj(value.local), u = obj(value.usage), h = obj(value.household);
  return { sampledAt: value.sampledAt, local: { model: string(l.model), busy: l.busy === true, contextTokens: number(l.contextTokens),
    continuousBatch: l.continuousBatch === true, memoryAvailableGiB: number(l.memoryAvailableGiB), benchmarkTokensPerSecond: number(l.benchmarkTokensPerSecond), benchmarkTtftMs: number(l.benchmarkTtftMs) },
    usage: value.usage ? { windowHours: number(u.windowHours), requests: number(u.requests), reportedOutputTokens: number(u.reportedOutputTokens),
      avgTtftMs: number(u.avgTtftMs), avgDurationMs: number(u.avgDurationMs), successRate: number(u.successRate) } : null,
    household: value.household ? { rosterCount: number(h.rosterCount), checked: number(h.checked), available: number(h.available), busy: number(h.busy), partial: h.partial === true,
      models: Array.isArray(h.models) ? h.models.flatMap(row => { const m = obj(row); return typeof m.model === "string" ? [{ model: m.model, machines: number(m.machines),
        benchmarkedMachines: number(m.benchmarkedMachines), meanBenchmarkTokensPerSecond: number(m.meanBenchmarkTokensPerSecond) }] : []; }) : [] } : null };
}
export async function statusSnapshot(args: CliArgs, signal?: AbortSignal): Promise<DiagnosticReport> {
  const report = await diagnose({ ...args, signal });
  try { report.performance = sanitizePerformance(await runtimeRequest(`/usage/performance?household=${report.scope === "mine"}`, { timeout: args.timeout, signal })); }
  catch { report.performance = null; }
  return report;
}
export function formatStatus(report: DiagnosticReport): string {
  const lines = [formatDiagnostics(report, false)], p = report.performance;
  if (!p) { lines.push("Performance metrics unavailable; a compatible local runtime is required."); return lines.join("\n"); }
  const n = (v: number | null, unit: string) => v === null ? "unavailable" : `${Number(v.toFixed(1))}${unit}`;
  lines.push(`Local runtime: ${p.local.busy ? "busy" : "idle"}; context ${p.local.contextTokens ?? "unknown"}; free RAM ${n(p.local.memoryAvailableGiB, " GiB")}.`);
  lines.push(`Last benchmark: ${n(p.local.benchmarkTokensPerSecond, " tok/s")}; first token ${n(p.local.benchmarkTtftMs, " ms")}.`);
  if (p.usage) lines.push(`Recorded last 24h: ${p.usage.requests ?? "unknown"} requests; average first token ${n(p.usage.avgTtftMs, " ms")}; average duration ${n(p.usage.avgDurationMs, " ms")}.`);
  if (p.household) {
    lines.push(`Other own machines: ${p.household.available ?? "unknown"} serving; ${p.household.busy ?? "unknown"} busy; ${p.household.checked ?? "unknown"}/${p.household.rosterCount ?? "unknown"} checked${p.household.partial ? " (partial)" : ""}.`);
    for (const m of p.household.models) lines.push(`  ${m.model}: ${m.machines} machine(s); mean last benchmark ${n(m.meanBenchmarkTokensPerSecond, " tok/s")} (${m.benchmarkedMachines} measured).`);
  }
  return lines.join("\n");
}
export async function watchReports(sample: () => Promise<DiagnosticReport>, out: (text: string) => void,
  options: { interval: number; signal: AbortSignal; json: boolean; tty: boolean }): Promise<void> {
  while (!options.signal.aborted) {
    const report = await sample();
    if (options.signal.aborted) break;
    out(options.json ? JSON.stringify({ ...report, sampledAt: new Date().toISOString() }) + "\n" :
      (options.tty ? "\x1b[2J\x1b[H" : "") + `${new Date().toISOString()}\n${plainText(formatStatus(report))}\n\n`);
    try { await delay(options.interval, undefined, { signal: options.signal }); }
    catch { if (!options.signal.aborted) throw new Error("Status refresh failed."); }
  }
}
export async function runStatus(args: CliArgs, out: (text: string) => void): Promise<number> {
  if (!args.watch) {
    const report = await statusSnapshot(args);
    out(args.json ? JSON.stringify(report) + "\n" : plainText(formatStatus(report)) + "\n");
    return report.ready ? 0 : 1;
  }
  const controller = new AbortController();
  const stop = () => controller.abort();
  process.on("SIGINT", stop); process.on("SIGTERM", stop);
  try { await watchReports(() => statusSnapshot(args, controller.signal), out,
    { interval: args.interval ?? 2000, signal: controller.signal, json: args.json, tty: Boolean(process.stdout.isTTY) }); }
  finally { process.off("SIGINT", stop); process.off("SIGTERM", stop); }
  return 0;
}

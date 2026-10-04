import { expect, it } from "vitest";
import { parseArgs } from "../src/args.js";
import { formatStatus, sanitizePerformance, watchReports } from "../src/status.js";
import type { DiagnosticReport } from "../src/diagnostics.js";
const report: DiagnosticReport = { schemaVersion: 1, scope: "local", ready: true, checks: [],
  runtime: { reachable: true, model: "coder", version: "1" }, mesh: { reachable: false, paired: null, connected: null, peersOnline: null, peersKnown: null },
  gateway: { endpoint: "http://localhost/v1", configured: true, reachable: true, models: ["coder"] } };
it("allowlists metrics, keeping missing samples unavailable and peer identity out of output", () => {
  const perf = sanitizePerformance({ schemaVersion: 1, sampledAt: "now", secret: "never-echo", local: { model: "coder", api_key: "never-echo" },
    usage: { requests: 0, avgTtftMs: null }, household: { models: [{ model: "coder", hostname: "never-echo", meanBenchmarkTokensPerSecond: 8 }] } });
  expect(perf?.local.benchmarkTokensPerSecond).toBeNull();
  expect(perf?.usage?.avgTtftMs).toBeNull(); expect(JSON.stringify(perf)).not.toContain("never-echo");
  expect(formatStatus({ ...report, performance: perf })).toContain("Last benchmark: unavailable");
});
it("watch emits complete NDJSON samples, does not overlap polls, and stops during the interval", async () => {
  const controller = new AbortController(); const lines: string[] = []; let calls = 0;
  await watchReports(async () => { calls++; return report; }, text => { lines.push(text); controller.abort(); },
    { interval: 60000, signal: controller.signal, json: true, tty: false });
  expect(calls).toBe(1); expect(lines).toHaveLength(1); expect(JSON.parse(lines[0]).ready).toBe(true);
  expect(lines[0]).not.toContain("\x1b");
});
it("watch never presents a cancelled in-flight sample as fresh", async () => {
  const controller = new AbortController(); const lines: string[] = [];
  await watchReports(async () => { controller.abort(); return report; }, line => lines.push(line),
    { interval: 1000, signal: controller.signal, json: true, tty: false });
  expect(lines).toEqual([]);
});
it("validates refresh intervals and keeps watch specific to status", () => {
  expect(parseArgs(["status", "--watch", "--interval", "1000", "--json"]).interval).toBe(1000);
  for (const args of [["doctor", "--watch"], ["status", "--interval", "1000"], ["status", "--watch", "--interval", "1"]]) expect(() => parseArgs(args)).toThrow();
});

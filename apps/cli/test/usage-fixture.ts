import { sanitizeUsage } from "../src/usage-data.js";
/** Synthetic telemetry for layout tests, never production data. */
export function usageFixture() {
  return sanitizeUsage({ schemaVersion: 1, sampledAt: "2026-09-24T19:42:18+00:00", since: "2026-09-17T19:42:18+00:00", windowDays: 7, direction: "consumed", bucketUnit: "day",
    totals: { requests: 1248, inputTokens: 2849100, outputTokens: 382400, failures: 4, avgTtftMs: 286, avgDurationMs: 8120, p95DurationMs: 14520, endToEndTokensPerSecond: 38.2, rateSamples: 1238 },
    activity: [32, 108, 61, 190, 147, 204, 320, 186].map((requests, i) => ({ bucket: `2026-09-${17 + i}`, requests, outputTokens: requests * 300 })),
    models: [{ model: "qwen2.5-coder-7b", requests: 728, outputTokens: 240300, failures: 2, endToEndTokensPerSecond: 42.6, rateSamples: 725, avgTtftMs: 220 },
      { model: "llama-3.1-8b", requests: 416, outputTokens: 121300, failures: 2, endToEndTokensPerSecond: 34.7, rateSamples: 410, avgTtftMs: 380 },
      { model: "qwen2.5-0.5b", requests: 104, outputTokens: 20800, failures: 0, endToEndTokensPerSecond: 61.2, rateSamples: 103, avgTtftMs: 110 }],
    routes: [{ scope: "local", requests: 608, outputTokens: 180000 }, { scope: "my_machines", requests: 640, outputTokens: 202400 }],
    recent: Array.from({ length: 24 }, (_, i) => ({ id: 1000 - i, ts: `2026-09-24T19:${String(42 - i).padStart(2, "0")}:12+00:00`, model: i % 3 ? "qwen2.5-coder-7b" : "llama-3.1-8b",
      scope: i % 3 ? "my_machines" : "local", inputTokens: 2100 + i * 41, outputTokens: 210 + i * 7, ttftMs: 230 + i * 5, durationMs: 7200 + i * 70, ok: i !== 6 ? 1 : 0 })),
    performance: { schemaVersion: 1, sampledAt: "2026-09-24T19:42:18+00:00", local: { model: "qwen2.5-coder-7b", busy: true, contextTokens: 32768, continuousBatch: false, memoryAvailableGiB: 17.4,
      benchmarkTokensPerSecond: 46.8, benchmarkTtftMs: 200 }, household: { rosterCount: 3, checked: 3, available: 2, busy: 1, partial: false,
      models: [{ model: "qwen2.5-coder-7b", machines: 1 }, { model: "llama-3.1-8b", machines: 1 }] } },
    memoryTotalGiB: 32, split: { state: "idle", workers: 0, workerEnabled: false, workerBusy: false } });
}

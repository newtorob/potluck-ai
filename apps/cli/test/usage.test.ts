import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import type { ReadStream } from "node:tty";
import { afterEach, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { sanitizeUsage } from "../src/usage-data.js";
import { renderUsage, textWidth, type UsageScreen } from "../src/usage-render.js";
import { enterUsageTerminal, runUsage, type UsageDependencies } from "../src/usage.js";
import { plainText } from "../src/logs.js";
import { usageFixture } from "./usage-fixture.js";
const screen = (): UsageScreen => ({ view: "overview", window: 7, direction: "consumed", theme: "dark", paused: false, offset: 0, report: usageFixture(), error: false, refreshing: false });
function terminal(tty = true) {
  const input = new PassThrough() as unknown as ReadStream;
  Object.assign(input, { isTTY: tty, isRaw: false, setRawMode: vi.fn((raw: boolean) => { input.isRaw = raw; return input; }) }); input.pause();
  const output = Object.assign(new EventEmitter(), { isTTY: tty, columns: 130, rows: 38 });
  const sample = vi.fn<UsageDependencies["sample"]>(async () => usageFixture());
  return { input, output, sample, env: { TERM: "xterm-256color" } } as UsageDependencies & { sample: typeof sample; output: typeof output };
}
afterEach(() => vi.useRealTimers());
it("validates dashboard commands and rejects misplaced or incompatible flags", () => {
  expect(parseArgs(["usage", "models", "--window", "30", "--direction", "served", "--once", "--theme", "light"]))
    .toMatchObject({ command: "usage", action: "models", window: 30, direction: "served", once: true, theme: "light" });
  expect(parseArgs(["usage", "--json", "--watch", "--interval", "1500"]).watch).toBe(true);
  for (const args of [["usage", "bad"], ["usage", "--window", "365"], ["usage", "--direction", "all"], ["usage", "--once", "--watch"],
    ["usage", "--theme", "red"], ["status", "--once"], ["status", "--window", "7"], ["usage", "--scope", "pool"]]) expect(() => parseArgs(args)).toThrow();
});
it("allowlists and bounds metadata and strips terminal injection", () => {
  const f = usageFixture(); const r = sanitizeUsage({ ...f, prompt: "private", recent: Array.from({ length: 102 }, () => ({ ...f.recent[0], prompt: "private" })),
    performance: { schemaVersion: 1, ...f.performance, local: { model: "x\x1b]52;c;stolen\x07\x1b[2J\n\u202eSECRET" }, household: { models: [{ model: "clean", hostname: "private" }] } } });
  expect(r.recent).toHaveLength(100); expect(JSON.stringify(r)).not.toMatch(/private|stolen|\\u001b|\\u202e/);
  expect(r.performance?.local.benchmarkTokensPerSecond).toBeNull(); expect(() => sanitizeUsage({ schemaVersion: 9 })).toThrow();
});
it.each(["overview", "models", "requests", "machines"] as const)("fits %s to 40–180 columns and 16–48 rows with CJK/emoji", view => {
  for (const [width, height] of [[40, 16], [64, 20], [80, 24], [130, 38], [180, 48]]) {
    const s = screen(); s.view = view; s.report!.models[0].model = "模型🦙é🇯🇵".repeat(50); s.report!.recent[0].model = s.report!.models[0].model;
    for (const color of [true, false]) {
      const rendered = renderUsage(s, { width, height, color }), lines = plainText(rendered).split("\n");
      expect(lines).toHaveLength(height); expect(lines.every(line => textWidth(line) <= width)).toBe(true);
      expect(rendered).not.toContain("\ufffd"); if (!color) expect(rendered).not.toContain("\x1b");
    }
  }
});
it("restores previous keyboard and cursor state exactly once", () => {
  const { input } = terminal(), out = vi.fn(); const restore = enterUsageTerminal(input, out);
  expect(input.isRaw).toBe(true); expect(input.isPaused()).toBe(false); restore(); restore();
  expect(input.isRaw).toBe(false); expect(input.isPaused()).toBe(true); expect(input.setRawMode).toHaveBeenCalledTimes(2);
  expect(out.mock.calls.at(-1)?.[0]).toContain("\x1b[?25h\x1b[?1049l");
});
it("pipes emit one JSON document, never touch input, and return a structured offline failure", async () => {
  const t = terminal(false), out = vi.fn(); expect(await runUsage(parseArgs(["usage", "--json"]), out, t)).toBe(0);
  expect(out).toHaveBeenCalledTimes(1); expect(JSON.parse(out.mock.calls[0][0]).totals.requests).toBe(1248); expect(t.input.setRawMode).not.toHaveBeenCalled();
  t.sample.mockRejectedValue(new Error("private credentials")); out.mockClear(); expect(await runUsage(parseArgs(["usage", "--json"]), out, t)).toBe(1);
  expect(JSON.parse(out.mock.calls[0][0])).toMatchObject({ available: false, error: "usage_unavailable" }); expect(out.mock.calls[0][0]).not.toContain("private");
});
it("TERM=dumb uses one ASCII snapshot without terminal control sequences", async () => {
  const t = terminal(); t.env.TERM = "dumb"; const out = vi.fn(); await runUsage(parseArgs(["usage"]), out, t);
  expect(out).toHaveBeenCalledTimes(1); expect(out.mock.calls[0][0]).not.toContain("\x1b"); expect(out.mock.calls[0][0]).not.toMatch(/[╭╮╰╯─│█●◈]/);
  expect(t.input.setRawMode).not.toHaveBeenCalled();
});
it("quit cancels an in-flight poll and restores the keyboard and listeners", async () => {
  const t = terminal(), out = vi.fn(), before = process.listenerCount("SIGINT");
  t.sample.mockImplementation((_w, _d, _h, signal) => new Promise((_resolve, reject) => { signal.addEventListener("abort", () => reject(new Error("cancel")), { once: true }); }));
  const run = runUsage(parseArgs(["usage"]), out, t); t.input.emit("keypress", "q", { name: "q" });
  expect(await run).toBe(0); expect(t.input.isRaw).toBe(false); expect(t.input.isPaused()).toBe(true); expect(t.input.listenerCount("keypress")).toBe(0);
  expect(t.output.listenerCount("resize")).toBe(0); expect(process.listenerCount("SIGINT")).toBe(before); expect(t.sample).toHaveBeenCalledTimes(1);
});
it("filters discard old samples, Machines opts into owned probes, and pause freezes polling", async () => {
  vi.useFakeTimers(); const t = terminal(), out = vi.fn(); let finish: (r: ReturnType<typeof usageFixture>) => void = () => {};
  t.sample.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })); const run = runUsage(parseArgs(["usage"]), out, t);
  t.input.emit("keypress", "d", { name: "d" }); t.input.emit("keypress", "4", { name: "4" });
  const old = usageFixture(); old.totals.requests = 987654321; finish(old); await vi.advanceTimersByTimeAsync(1);
  expect(t.sample.mock.calls.map(c => [c[0], c[1], c[2]])).toEqual([[7, "consumed", false], [7, "served", true]]);
  expect(out.mock.calls.map(c => c[0]).join("")).not.toContain("987.7M"); t.input.emit("keypress", " ", { name: "space" });
  await vi.advanceTimersByTimeAsync(15000); expect(t.sample).toHaveBeenCalledTimes(2);
  t.input.emit("keypress", "r", { name: "r" }); await vi.advanceTimersByTimeAsync(1); expect(t.sample).toHaveBeenCalledTimes(3);
  t.input.emit("keypress", "q", { name: "q" }); await run;
});
it("rendering exceptions still release raw mode and the alternate screen", async () => {
  const t = terminal(); let writes = 0; const out = vi.fn((_s: string) => { if (++writes === 2) throw new Error("broken sink"); });
  await expect(runUsage(parseArgs(["usage"]), out, t)).rejects.toThrow("broken sink");
  expect(t.input.isRaw).toBe(false); expect(t.input.listenerCount("keypress")).toBe(0); expect(out.mock.calls.at(-1)?.[0]).toContain("?1049l");
});

it("shows stale data explicitly and repaints a resized terminal without overlapping polls", async () => {
  vi.useFakeTimers(); const t = terminal(), out = vi.fn();
  t.sample.mockResolvedValueOnce(usageFixture()).mockRejectedValueOnce(new Error("offline")).mockResolvedValue(usageFixture());
  const run = runUsage(parseArgs(["usage"]), out, t); await vi.advanceTimersByTimeAsync(1);
  t.output.columns = 80; t.output.rows = 24; t.output.emit("resize");
  expect(out.mock.calls.at(-2)?.[0]).toBe("\x1b[2J");
  await vi.advanceTimersByTimeAsync(2000);
  expect(out.mock.calls.at(-1)?.[0]).toContain("Connection lost");
  expect(out.mock.calls.at(-1)?.[0]).toContain("19:42:18");
  await vi.advanceTimersByTimeAsync(2000);
  expect(out.mock.calls.at(-1)?.[0]).not.toContain("Connection lost");
  expect(t.sample).toHaveBeenCalledTimes(3);
  t.input.emit("keypress", "q", { name: "q" }); await run;
});

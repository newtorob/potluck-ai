/** Dependency-free terminal canvas: cell-aware clipping, no cursor input tricks. */
import { cellText, type UsageReport, type UsageView } from "./usage-data.js";

export interface UsageScreen {
  view: UsageView; window: number; direction: "consumed" | "served"; theme: "dark" | "light";
  paused: boolean; offset: number; report: UsageReport | null; error: boolean; refreshing: boolean;
}
export interface RenderOptions { width: number; height?: number; color?: boolean; unicode?: boolean; interactive?: boolean }
type Tone = "text" | "muted" | "ember" | "sage" | "red" | "border";
const colors = {
  dark: { bg: "26;23;20", text: "243;237;225", muted: "180;169;153", ember: "220;143;115", sage: "157;183;160", red: "226;133;115", border: "73;63;54" },
  light: { bg: "246;243;237", text: "34;27;21", muted: "99;84;64", ember: "147;66;36", sage: "75;118;84", red: "161;54;30", border: "207;196;180" },
};
const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
export function glyphs(value: string): Array<{ text: string; width: number }> {
  return Array.from(segmenter.segment(cellText(value)), ({ segment }) => {
    const cp = segment.codePointAt(0)!;
    const wide = /[\p{Extended_Pictographic}\p{Regional_Indicator}\ufe0f\u20e3]/u.test(segment) || (cp >= 0x1100 && (cp <= 0x115f || cp === 0x2329 || cp === 0x232a ||
      (cp >= 0x2e80 && cp <= 0xa4cf) || (cp >= 0xac00 && cp <= 0xd7a3) || (cp >= 0xf900 && cp <= 0xfaff) ||
      (cp >= 0xfe10 && cp <= 0xfe6f) || (cp >= 0xff01 && cp <= 0xff60) || (cp >= 0xffe0 && cp <= 0xffe6) || cp >= 0x20000));
    return { text: segment, width: /^\p{Mark}+$/u.test(segment) ? 0 : wide ? 2 : 1 };
  });
}
export const textWidth = (value: string) => glyphs(value).reduce((n, g) => n + g.width, 0);
export function fit(value: string, width: number): string {
  if (width <= 0) return "";
  const gs = glyphs(value); if (gs.reduce((n, g) => n + g.width, 0) <= width) return gs.map(g => g.text).join("");
  let result = "", used = 0;
  for (const g of gs) { if (!g.width) continue; if (used + g.width > width - 1) break; result += g.text; used += g.width; }
  return result + "~";
}
class Canvas {
  cells: Array<Array<{ text: string; tone: Tone; bold: boolean }>>;
  constructor(readonly width: number, readonly height: number, readonly theme: UsageScreen["theme"], readonly color: boolean, readonly unicode: boolean) {
    this.cells = Array.from({ length: height }, () => Array.from({ length: width }, () => ({ text: " ", tone: "text" as Tone, bold: false })));
  }
  text(x: number, y: number, value: string, tone: Tone = "text", bold = false, max = this.width - x) {
    if (y < 0 || y >= this.height) return;
    for (const g of glyphs(fit(value, Math.min(max, this.width - x)))) {
      if (!g.width) continue;
      if (x >= 0 && x + g.width <= this.width) {
        this.cells[y][x] = { text: g.text, tone, bold };
        if (g.width === 2) this.cells[y][x + 1] = { text: "", tone, bold };
      }
      x += g.width;
    }
  }
  line(x: number, y: number, width: number) { this.text(x, y, (this.unicode ? "─" : "-").repeat(Math.max(0, width)), "border"); }
  box(x: number, y: number, width: number, height: number, title: string) {
    const chars = this.unicode ? ["╭", "╮", "╰", "╯", "─", "│"] : ["+", "+", "+", "+", "-", "|"];
    this.text(x, y, chars[0] + chars[4].repeat(width - 2) + chars[1], "border");
    for (let dy = 1; dy < height - 1; dy++) { this.text(x, y + dy, chars[5], "border"); this.text(x + width - 1, y + dy, chars[5], "border"); }
    this.text(x, y + height - 1, chars[2] + chars[4].repeat(width - 2) + chars[3], "border");
    this.text(x + 2, y, ` ${title} `, "muted", false, width - 4);
  }
  render(): string {
    const palette = colors[this.theme];
    return this.cells.map(row => {
      if (!this.color) return row.map(c => c.text).join("").trimEnd();
      let line = `\x1b[48;2;${palette.bg}m`, style = "";
      for (const cell of row) {
        const next = `${cell.bold ? 1 : 22};38;2;${palette[cell.tone]}`;
        if (next !== style) { line += `\x1b[${next}m`; style = next; }
        line += cell.text;
      }
      return line + "\x1b[0m";
    }).join("\n");
  }
}
const compact = (v: number | null) => v === null ? "--" : Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(v);
const n = (v: number | null) => v === null ? "--" : Intl.NumberFormat("en", { maximumFractionDigits: 1 }).format(v);
const duration = (ms: number | null) => ms === null ? "--" : ms < 1000 ? `${n(Math.round(ms))}ms` : ms < 60000 ? `${n(ms / 1000)}s` : `${n(ms / 60000)}m`;
const pct = (ratio: number | null) => ratio === null ? "--" : `${n(ratio * 100)}%`;
const route = (scope: string) => ({ local: "This machine", my_machines: "Your machines", circle: "Trusted circle", pool: "Open pool" }[scope] ?? "Unknown");
function bar(c: Canvas, x: number, y: number, width: number, ratio: number, tone: Tone) {
  const used = Math.round(Math.min(1, Math.max(0, ratio)) * width);
  c.text(x, y, (c.unicode ? "━" : "=").repeat(used), tone);
  c.text(x + used, y, (c.unicode ? "─" : ".").repeat(width - used), "border");
}
function chart(c: Canvas, x: number, y: number, width: number, height: number, r: UsageReport) {
  const values: number[] = [];
  const bin = Math.max(1, Math.ceil(r.activity.length / width));
  for (let i = 0; i < r.activity.length; i += bin) values.push(r.activity.slice(i, i + bin).reduce((s, a) => s + a.requests, 0));
  const peak = Math.max(1, ...values), bars = "▁▂▃▄▅▆▇█";
  for (let col = 0; col < width; col++) {
    const index = Math.min(values.length - 1, Math.floor(col * values.length / width));
    const level = (values[index] ?? 0) / peak * height;
    for (let row = 0; row < height; row++) {
      const fill = level - (height - row - 1);
      c.text(x + col, y + row, fill > 0 ? c.unicode ? bars[Math.min(7, Math.ceil(fill * 8) - 1)] : "#" : " ", index === values.length - 1 ? "ember" : "sage");
    }
  }
  c.line(x, y + height, width);
  const first = r.activity[0]?.bucket ?? "", last = r.activity.at(-1)?.bucket ?? "";
  const label = (s: string) => r.bucketUnit === "hour" ? `${s.slice(11)}:00` : s.slice(5, 10);
  c.text(x, y + height + 1, `${label(first)} UTC`, "muted");
  c.text(x + width - 12, y + height + 1, `${label(last)} UTC`, "muted");
  c.text(x, y - 1, `${compact(r.totals.requests)} requests  /  ${bin > 1 ? `${bin} ` : ""}${r.bucketUnit} buckets`, "muted", false, width);
}
function recent(c: Canvas, r: UsageReport, x: number, y: number, width: number, count: number, offset = 0) {
  const wide = width >= 94, modelWidth = Math.max(12, width - (wide ? 78 : 52));
  c.text(x, y, "ENDED UTC", "muted"); c.text(x + 16, y, "MODEL", "muted");
  const at = x + 17 + modelWidth;
  c.text(at, y, "OUTPUT", "muted"); c.text(at + 10, y, "TTFT", "muted"); c.text(at + 20, y, "TIME", "muted");
  if (wide) { c.text(at + 30, y, "ROUTE", "muted"); c.text(at + 48, y, "INPUT", "muted"); }
  for (const [i, row] of r.recent.slice(offset, offset + count).entries()) {
    const dy = y + i + 2;
    c.text(x, dy, row.ok ? c.unicode ? "●" : "+" : "!", row.ok ? "sage" : "red");
    c.text(x + 2, dy, row.ts.slice(5, 10) + " " + row.ts.slice(11, 16), "muted", false, 13);
    c.text(x + 16, dy, row.model, "text", false, modelWidth);
    c.text(at, dy, compact(row.outputTokens), "ember"); c.text(at + 10, dy, duration(row.ttftMs)); c.text(at + 20, dy, duration(row.durationMs), row.ok ? "text" : "red");
    if (wide) { c.text(at + 30, dy, route(row.scope), "muted", false, 16); c.text(at + 48, dy, compact(row.inputTokens), "muted"); }
  }
  if (!r.recent.length) c.text(x, y + 2, "No recorded requests in this window.", "muted");
}
function models(c: Canvas, r: UsageReport, x: number, y: number, width: number, count: number, offset: number) {
  const wide = width >= 94, name = width - (wide ? 78 : 47), at = x + name;
  c.text(x, y, "MODEL", "muted");
  for (const [dx, label] of [[0, "REQUESTS"], [12, "OUTPUT"], [24, "E2E TOK/S"], [38, "TTFT"]] as const) c.text(at + dx, y, label, "muted");
  if (wide) { c.text(at + 49, y, "SUCCESS", "muted"); c.text(at + 61, y, "SAMPLES", "muted"); }
  for (const [i, row] of r.models.slice(offset, offset + count).entries()) {
    const dy = y + 2 + i * 2;
    c.text(x, dy, row.model, "text", true, name - 2);
    c.text(at, dy, compact(row.requests)); c.text(at + 12, dy, compact(row.outputTokens), "ember");
    c.text(at + 24, dy, n(row.endToEndTokensPerSecond), "sage"); c.text(at + 38, dy, duration(row.avgTtftMs));
    if (wide) { c.text(at + 49, dy, pct(row.requests && row.failures !== null ? 1 - row.failures / row.requests : null)); c.text(at + 61, dy, n(row.rateSamples), "muted"); }
  }
  if (!r.models.length) c.text(x, y + 2, "Model comparisons appear after your first request.", "muted");
}
function runtime(c: Canvas, r: UsageReport, x: number, y: number, width: number) {
  const p = r.performance?.local;
  c.text(x, y, p ? `${c.unicode ? "●" : "+"}  ${p.busy ? "Working" : "Ready"}` : "Runtime telemetry unavailable", p ? "sage" : "muted", true, width);
  c.text(x, y + 2, p?.model || "No local model loaded", "text", false, width);
  c.text(x, y + 4, `Context capacity   ${compact(p?.contextTokens ?? null)} tokens`, "muted", false, width);
  c.text(x, y + 5, `Free system RAM    ${n(p?.memoryAvailableGiB ?? null)} GiB`, "muted", false, width);
  if (p?.memoryAvailableGiB !== null && p?.memoryAvailableGiB !== undefined && r.memoryTotalGiB) {
    bar(c, x, y + 6, width, p.memoryAvailableGiB / r.memoryTotalGiB, "sage");
  }
  c.text(x, y + 7, `Last benchmark     ${n(p?.benchmarkTokensPerSecond ?? null)} tok/s`, "ember", false, width);
}
export function renderUsage(state: UsageScreen, options: RenderOptions): string {
  const width = Math.min(200, Math.max(10, Math.floor(options.width))), height = Math.max(4, Math.min(100, Math.floor(options.height ?? 38)));
  const c = new Canvas(width, height, state.theme, options.color ?? false, options.unicode ?? true);
  const r = state.report, status = state.error ? "OFFLINE / STALE" : state.paused ? "PAUSED" : state.refreshing ? "UPDATING" : options.interactive ? "LIVE" : "SNAPSHOT";
  c.text(1, 0, c.unicode ? "◈ POTLUCK" : "* POTLUCK", "ember", true);
  c.text(13, 0, "/ usage", "muted");
  if (width >= 64) c.text(width - 37, 0, `${state.direction === "served" ? "CONTRIBUTION" : "YOUR USAGE"}  /  ${state.window}D  /  ${status}`, state.error ? "red" : "muted", false, 36);
  const tabs: UsageView[] = ["overview", "models", "requests", "machines"];
  let tabX = 1;
  tabs.forEach((t, i) => { const label = `${i + 1} ${t[0].toUpperCase() + t.slice(1)}`; c.text(tabX, 2, label, state.view === t ? "ember" : "muted", state.view === t); tabX += label.length + 4; });
  c.line(1, 3, width - 2);
  if (width < 64 || height < 20) {
    c.text(1, 5, `${state.direction === "served" ? "Contribution" : "Your usage"} / ${state.window}d / ${status}`, "muted");
    c.text(1, 7, r ? `${n(r.totals.requests)} requests  |  ${compact(r.totals.outputTokens)} output tokens` : "Waiting for local runtime", "ember");
    c.text(1, 9, "Enlarge to 64 x 20 for the full dashboard.", "muted");
    c.text(1, height - 2, options.interactive ? "q quit  r refresh" : "potluck usage --json", "muted");
    return c.render();
  }
  if (!r) {
    c.text(3, 6, state.error ? "Your usage will be here when the runtime is ready." : "Connecting to your local runtime...", "text", true, width - 6);
    c.text(3, 8, "Run potluck up. This screen needs a dashboard-capable runtime.", "muted", false, width - 6);
    c.text(3, 10, "After an upgrade, restart the runtime to load the new version.", "muted", false, width - 6);
  } else {
    const t = r.totals, cardWidth = Math.floor((width - 5) / 4);
    const short = width < 100;
    const cards: Array<[string, string, string]> = [["Requests", compact(t.requests), `${compact(t.failures)} failed`],
      [short ? "Output" : "Output tokens", compact(t.outputTokens), `${compact(t.inputTokens)} ${short ? "in" : "input"}`],
      [short ? "E2E tok/s" : "End-to-end tok/s", n(t.endToEndTokensPerSecond), `${compact(t.rateSamples)} ${short ? "samples" : "measured requests"}`],
      ["First token", duration(t.avgTtftMs), `p95 ${short ? "" : "total "}${duration(t.p95DurationMs)}`]];
    cards.forEach(([label, value, sub], i) => { const x = 1 + i * (cardWidth + 1); c.box(x, 5, cardWidth, 5, label); c.text(x + 2, 7, value, i === 2 ? "sage" : "ember", true, cardWidth - 4); c.text(x + 2, 8, sub, "muted", false, cardWidth - 4); });
    const bottom = height - 4;
    if (state.view === "overview") {
      const middleHeight = Math.min(12, Math.max(5, bottom - 11 - (height >= 30 ? 8 : 0))), side = width >= 100 ? 38 : 0, chartWidth = width - side - 2;
      c.box(1, 11, chartWidth, middleHeight, "Activity");
      if (t.requests && middleHeight >= 7) chart(c, 3, 14, chartWidth - 4, middleHeight - 6, r);
      else if (t.requests) c.text(3, 13, `${n(t.requests)} recorded requests / ${state.window} days`, "sage", false, chartWidth - 4);
      else { c.text(3, 13, "A quiet start. Your next request begins the story.", "muted", false, chartWidth - 4); }
      if (side) { c.box(chartWidth + 2, 11, side - 1, middleHeight, "This machine"); if (middleHeight >= 10) runtime(c, r, chartWidth + 4, 13, side - 5); else c.text(chartWidth + 4, 13, r.performance?.local.model || "No model loaded", "muted", false, side - 5); }
      const y = 12 + middleHeight;
      if (y + 4 < bottom) { c.text(2, y, "LATEST REQUESTS", "muted", true); recent(c, r, 2, y + 2, width - 4, bottom - y - 4); }
    } else if (state.view === "models") {
      c.text(2, 11, "MODEL PERFORMANCE", "muted", true);
      models(c, r, 2, 13, width - 4, Math.max(1, Math.floor((bottom - 15) / 2)), state.offset);
    } else if (state.view === "requests") {
      c.text(2, 11, `REQUEST HISTORY  /  ${r.recent.length ? state.offset + 1 : 0}-${Math.min(r.recent.length, state.offset + Math.max(1, bottom - 15))} of ${r.recent.length} recent`, "muted", true);
      recent(c, r, 2, 13, width - 4, Math.max(1, bottom - 15), state.offset);
    } else {
      const half = Math.floor((width - 3) / 2), h = r.performance?.household;
      c.box(1, 11, half, 11, "This machine"); runtime(c, r, 3, 13, half - 4);
      c.box(half + 2, 11, width - half - 3, 11, "Your other machines");
      const x = half + 4, w = width - x - 3;
      c.text(x, 13, h ? `${n(h.available)} serving  /  ${n(h.busy)} busy` : "Availability unavailable", h ? "sage" : "muted", true, w);
      c.text(x, 15, h ? `${n(h.checked)} / ${n(h.rosterCount)} checked${h.partial ? " (partial)" : ""}` : "Owned-machine probes only", "muted", false, w);
      for (const [i, m] of (h?.models ?? []).slice(0, 3).entries()) c.text(x, 17 + i, `${m.machines}x  ${m.model}`, "text", false, w);
      if (bottom > 27) {
        c.text(2, 23, "ROUTING", "muted", true);
        r.routes.forEach((row, i) => { c.text(2, 25 + i, route(row.scope), "text", false, 16); bar(c, 20, 25 + i, Math.max(4, half - 33), row.requests / Math.max(1, t.requests ?? 1), "ember"); c.text(half - 10, 25 + i, compact(row.requests), "muted"); });
        c.text(x, 23, "SPLIT MODEL", "muted", true);
        c.text(x, 25, r.split ? `${r.split.state}  /  ${n(r.split.workers)} workers` : "Unavailable", "sage", false, w);
        c.text(x, 26, r.split?.model || (r.split?.workerEnabled ? r.split.workerBusy ? "Serving as a split worker" : "Worker sharing enabled" : "Worker sharing off"), "muted", false, w);
      }
    }
  }
  const footer = height - 4;
  // Footer is drawn last and clears its rows, so a short terminal never overlaps it.
  for (let y = footer; y < height; y++) c.cells[y] = Array.from({ length: width }, () => ({ text: " ", tone: "text", bold: false }));
  c.line(1, footer, width - 2);
  c.text(1, footer + 1, state.error ? "Connection lost. Showing the last sample; r retries." : "E2E includes prefill. Reported tokens only. -- means unavailable.", state.error ? "red" : "muted", false, width - 2);
  c.text(1, footer + 2, options.interactive ? "1-4 views  w window  d usage/served  space pause  r refresh  q quit" : `Recorded on this machine  /  ${r?.sampledAt.slice(0, 19).replace("T", " ") ?? "unavailable"} UTC`, "muted", false, width - 2);
  if (options.interactive && r) c.text(1, footer + 3, `Sample ${r.sampledAt.slice(11, 19)} UTC / local records`, "muted", false, width - 2);
  if (options.interactive && width >= 110) c.text(width - 38, footer + 3, "arrows scroll  t theme  ? help", "muted");
  return c.render();
}

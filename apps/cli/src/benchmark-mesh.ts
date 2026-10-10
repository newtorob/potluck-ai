/**
 * `potluck benchmark --scope mine`: time the models your other machines have
 * loaded, from this machine, through this machine's own Potluck engine.
 *
 * The benchmark must only ever measure Potluck. Nothing here reads gatewayUrl
 * from cli.json, POTLUCK_GATEWAY_URL or POTLUCK_GATEWAY_KEY: those can point at
 * another server (an SSH tunnel to Ollama has done so). The address comes from
 * the runtime record this CLI already verifies for every management call, and
 * the key comes from that engine's own settings.
 */
import type { CliArgs } from "./args.js";
import type { Availability } from "./availability.js";
import type { BenchmarkDeps, BenchmarkIO } from "./benchmark.js";
import { Interrupted, label, round1, roundSpread, spread, fmtMs, fmtRange, fmtRate, type Spread } from "./benchmark-stats.js";
import { requestJSON, ControlError } from "./control.js";
import { loadConfig, type GatewayConfig } from "./config.js";
import { GatewayRefusedError, streamChat } from "./gateway.js";
import { readRuntimeRecord, RuntimeError, runtimeRequest, verifyRuntime } from "./runtime.js";

export interface LocalGateway { cfg: GatewayConfig; configuredElsewhere: boolean }

const PROMPT = "Write a short paragraph about the ocean.";
const MAX_TOKENS = 64;
const MAX_MODELS = 8;

const obj = (v: unknown): Record<string, unknown> => v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {};
function sameEndpoint(a: string, b: string): boolean {
  try {
    const [x, y] = [new URL(a), new URL(b)];
    const host = (u: URL) => u.hostname === "localhost" ? "127.0.0.1" : u.hostname;
    return x.protocol === y.protocol && host(x) === host(y) && x.port === y.port && x.pathname.replace(/\/+$/, "") === y.pathname.replace(/\/+$/, "");
  } catch { return false; }
}

/**
 * The only gateway the benchmark talks to: the engine this CLI controls. The
 * address is the verified runtime record's loopback origin and the key is read
 * from that engine's settings, so a different gatewayUrl or key in cli.json or
 * the environment is never used. `configuredElsewhere` only reports that the
 * configured address differs, so the output can say it was set aside.
 */
export async function localGateway(timeout: number, signal?: AbortSignal): Promise<LocalGateway> {
  const record = await readRuntimeRecord();
  if (!record) throw new RuntimeError("No shared runtime found. Start it with potluck up.");
  await verifyRuntime(record, timeout, signal);
  const settings = await requestJSON({ url: record.url + "/settings", token: record.token, timeout, ...(signal ? { signal } : {}) });
  const gateway = obj(settings.gateway);
  if (gateway.enabled !== true || typeof gateway.api_key !== "string" || !gateway.api_key) {
    throw new RuntimeError("--scope mine sends its requests through this machine's gateway, which is off. Run potluck gateway enable, then try again.");
  }
  const cfg: GatewayConfig = { baseURL: record.url + "/v1", apiKey: gateway.api_key, enabled: true };
  let configuredElsewhere = false;
  try { configuredElsewhere = !sameEndpoint((await loadConfig()).baseURL, cfg.baseURL); } catch { /* unreadable config: nothing to report */ }
  return { cfg, configuredElsewhere };
}

export interface MineRun { firstTokenMs: number; chunks: number; tokensPerSecond: number | null }
export interface MineRow {
  model: string; runs: MineRun[]; error: string | null;
  firstTokenMs: Spread | null; tokensPerSecond: Spread | null;
  /** Machines that advertise this model. The engine does not say which one answered, so none is named as the server. */
  offeredBy: string[];
}
export interface MineReport {
  measuredFrom: "this machine over the mesh, including network time";
  configuredGatewayIgnored: boolean; error: string | null;
  rows: MineRow[]; skipped: Array<{ model: string; reason: string }>; notMeasured: number;
}

class NotFromMesh extends Error { constructor(readonly servedBy: string | null) { super("not from the mesh"); } }

async function timeOne(cfg: GatewayConfig, model: string, signal: AbortSignal, deps: BenchmarkDeps): Promise<MineRun> {
  const start = deps.clock();
  let first: number | null = null, last = 0, chunks = 0;
  for await (const _ of streamChat(cfg, [{ role: "user", content: PROMPT }], {
    model, scope: "mine", maxTokens: MAX_TOKENS, temperature: 0, signal, requireDone: true,
    // Only the engine's own answer counts, and only when it says another machine served it.
    onHeaders: headers => { const by = headers.get("x-potluck-served-by"); if (by !== "household") throw new NotFromMesh(by); },
  })) {
    const now = deps.clock();
    first ??= now; last = now; chunks++;
  }
  if (first === null) throw new RuntimeError("The model returned no text.");
  return { firstTokenMs: first - start, chunks, tokensPerSecond: chunks > 1 && last > first ? (chunks - 1) / ((last - first) / 1000) : null };
}

function rowError(error: unknown, timeout: number): string {
  if (error instanceof NotFromMesh) return error.servedBy === "local"
    ? "This machine answered the request itself, so it was not measured over the mesh."
    : "The answer did not come from a Potluck engine, so it was not counted.";
  if (error instanceof GatewayRefusedError) return error.status === 401 ? "The local gateway rejected its key. Run potluck gateway status."
    : [404, 503].includes(error.status) ? "No other machine of yours is serving this model right now." : "The request was refused.";
  if (error instanceof Error && error.name === "TimeoutError") return `No complete answer within ${Math.round(timeout / 1000)} s.`;
  if (error instanceof RuntimeError) return error.message;
  return "The request failed or the stream ended early.";
}

export const emptyMine = (gateway: LocalGateway): MineReport => ({ measuredFrom: "this machine over the mesh, including network time",
  configuredGatewayIgnored: gateway.configuredElsewhere, error: null, rows: [], skipped: [], notMeasured: 0 });

/**
 * Models other machines of yours have loaded, measured from here with a warm-up
 * call and `runs` timed calls each. Fills `report` as it goes, so an interrupt
 * keeps the rows already measured.
 */
export async function measureMine(report: MineReport, args: CliArgs, io: BenchmarkIO, deps: BenchmarkDeps, gateway: LocalGateway, runs: number): Promise<MineReport> {
  let available: Availability;
  try { available = await runtimeRequest<Availability>("/models/available", { timeout: args.timeout, ...(io.signal ? { signal: io.signal } : {}) }); }
  catch (error) {
    if (io.signal?.aborted) throw new Interrupted();
    report.error = error instanceof ControlError && error.status === 404 ? "This engine cannot list the models on your other machines. Update the Potluck runtime."
      : "Could not list the models on your other machines.";
    return report;
  }
  if (!Array.isArray(available.models)) { report.error = "The engine returned an invalid model list."; return report; }
  const localModel = typeof available.local?.model === "string" ? available.local.model : null;
  // Group by model ID: a request names only the ID, so rows for one ID cannot be told apart.
  const byId = new Map<string, { names: Set<string>; local: boolean }>();
  for (const row of available.models) {
    const machines = Array.isArray(row.machines) ? row.machines : [];
    if (typeof row.id !== "string" || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}$/.test(row.id) || !machines.length) continue;
    const entry = byId.get(row.id) ?? { names: new Set<string>(), local: false };
    for (const m of machines) { const name = label(obj(m).name, 64); if (name) entry.names.add(name); }
    entry.local ||= row.local === true || row.id === localModel;
    byId.set(row.id, entry);
  }
  const candidates: string[] = [];
  for (const [model, entry] of byId) {
    if (entry.local) report.skipped.push({ model, reason: "also loaded on this machine, so a request for it is answered here and not over the mesh" });
    else candidates.push(model);
  }
  report.notMeasured = Math.max(0, candidates.length - MAX_MODELS);
  for (const model of candidates.slice(0, MAX_MODELS)) {
    const row: MineRow = { model, runs: [], error: null, firstTokenMs: null, tokensPerSecond: null, offeredBy: [...byId.get(model)!.names].sort() };
    const timed: MineRun[] = []; // unrounded, so rounding the report never shifts a median
    for (let i = 0; i <= runs && !row.error; i++) {
      io.progress(i === 0 ? `Warming up ${model} over the mesh...\n` : `${model} over the mesh: run ${i} of ${runs}\n`);
      const signal = AbortSignal.any([...(io.signal ? [io.signal] : []), AbortSignal.timeout(args.timeout)]);
      try {
        const run = await timeOne(gateway.cfg, model, signal, deps);
        if (i > 0) {
          timed.push(run);
          row.runs.push({ ...run, firstTokenMs: round1(run.firstTokenMs), tokensPerSecond: run.tokensPerSecond === null ? null : round1(run.tokensPerSecond) });
        }
      } catch (error) {
        if (io.signal?.aborted) throw new Interrupted();
        row.error = rowError(error, args.timeout);
      }
    }
    if (!row.error) {
      row.firstTokenMs = roundSpread(spread(timed.map(r => r.firstTokenMs)));
      row.tokensPerSecond = roundSpread(spread(timed.map(r => r.tokensPerSecond)));
    }
    report.rows.push(row);
  }
  return report;
}

/** The engine names only the tier that answered, so a machine is named only when it is the one offering the model. */
function whoAnswered(offeredBy: string[]): string {
  if (offeredBy.length === 1) return `On ${offeredBy[0]}, the only other machine of yours offering this model.`;
  if (offeredBy.length > 1) return `Answered by one of: ${offeredBy.join(", ")}. The engine does not say which.`;
  return "The machine that answered is not known.";
}

export function formatMine(mine: MineReport): string[] {
  const lines = [`Your other machines, measured from this machine over the mesh. These times include the network.${mine.configuredGatewayIgnored ? " The gateway address in your settings was not used; this reads only the Potluck engine on this machine." : ""}`];
  if (mine.error) return [...lines, mine.error];
  for (const row of mine.rows) {
    if (row.error) { lines.push(`${row.model}: ${row.error}`); continue; }
    const g = row.tokensPerSecond, t = row.firstTokenMs;
    const parts = [g ? `${fmtRate(g.median)} tokens/s after the first token${fmtRange(g, fmtRate)}` : "speed after the first token not measured (too few tokens)"];
    if (t) parts.push(`first token ${fmtMs(t.median)}${fmtRange(t, fmtMs)}`);
    lines.push(`${row.model}, ${row.runs.length} run${row.runs.length === 1 ? "" : "s"}: ${parts.join(", ")}`, `  ${whoAnswered(row.offeredBy)}`);
  }
  for (const s of mine.skipped) lines.push(`${s.model}: skipped, ${s.reason}.`);
  if (!mine.rows.length && !mine.skipped.length) lines.push("No other machine of yours has a model loaded right now.");
  if (mine.notMeasured) lines.push(`${mine.notMeasured} more model${mine.notMeasured === 1 ? " was" : "s were"} not measured; each run covers at most ${MAX_MODELS} models.`);
  return lines;
}

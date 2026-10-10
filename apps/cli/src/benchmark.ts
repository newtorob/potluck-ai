/**
 * `potluck benchmark`: show the model this machine runs, optionally switch to
 * another installed or downloadable one, and measure it. Switching uses the
 * same install, poll and load steps as `potluck setup` (model-setup.ts).
 */
import { setTimeout as sleep } from "node:timers/promises";
import { createInterface } from "node:readline/promises";
import { UsageError, type CliArgs } from "./args.js";
import { ControlError } from "./control.js";
import { plainText } from "./logs.js";
import { emptyMine, formatMine, localGateway, measureMine, type MineReport } from "./benchmark-mesh.js";
import { acceptanceFor, acceptanceLine, describeLoaded, describeMachine, fmtMs, fmtRate, formatSummary, gpuNote, Interrupted, parseInference, parseRun,
  roundRun, summarizeRuns, type Acceptance, type InferenceInfo, type LastBenchmark, type Machine, type Placement, type RunResult, type RunSummary } from "./benchmark-stats.js";
import { ensureInstalled, gib, isLoadPending, loadModel, readSetupOptions, validID, type ModelCall, type ModelDeps, type SetupModel, type SetupOptions, type TransferContext } from "./model-setup.js";
import type { OnboardingIO } from "./operational-setup.js";
import { RuntimeError, runtimeRequest } from "./runtime.js";

export interface BenchmarkIO extends OnboardingIO {
  /** A person can answer questions. Without one, nothing is asked and a model switch needs --yes. */
  interactive: boolean;
}
export interface BenchmarkDeps extends ModelDeps {
  /** High-resolution milliseconds for client-side timing over the mesh. */
  clock: () => number;
}
const defaultDeps: BenchmarkDeps = { wait: signal => sleep(1000, undefined, { signal }), now: () => Date.now(), clock: () => performance.now() };

/** `restored`/`unloaded`: the machine is back as it was (the earlier model loaded again, or no model when none was loaded). */
export type SwitchOutcome = "restored" | "unloaded" | "kept" | "restore_failed" | "unload_failed" | "load_failed" | "load_pending";
export interface BenchmarkReport {
  schemaVersion: 1;
  /** `quit`: you chose not to run it, nothing was changed. */
  status: "complete" | "failed" | "interrupted" | "quit";
  error?: { code: string; message: string };
  machine: Machine;
  /** The model that was chosen to measure. Null when only other machines were measured. */
  model: string | null;
  /** Set when no model was loaded here and `--scope mine` measured only the other machines. */
  localSkipped: { reason: "no_model_loaded"; message: string } | null;
  /** Where that model ran, read after it was loaded. Null when the engine does not say. */
  ranOn: Placement | null;
  /** The engine's check of this model's answers on this setup. Null when the engine has no such check or has not run it on this load. */
  acceptance: Acceptance | null;
  gpuDevices: string[];
  /** The engine's remembered result for this model from before this run. */
  lastBenchmarkBefore: LastBenchmark | null;
  /** Counted runs requested; each run follows one warm-up call that is not counted. */
  requestedRuns: number;
  runs: RunResult[];
  summary: RunSummary | null;
  switch: { from: string | null; to: string; outcome: SwitchOutcome } | null;
  loadedAtEnd: string | null;
  loadedAtEndConfirmed: boolean;
  mine?: MineReport;
}

const quote = (id: string) => encodeURIComponent(id);
const yes = (answer: string | null) => answer !== null && /^(y|yes)$/i.test(answer.trim());
function joinSteps(steps: string[]): string {
  return steps.length < 3 ? steps.join(" and ") : `${steps.slice(0, -1).join(", ")}, and ${steps[steps.length - 1]}`;
}

function listModels(models: SetupModel[], recommended: string | null, io: OnboardingIO): void {
  const line = (m: SetupModel) => `  ${m.id}: ${gib(m.sizeBytes)}${m.loaded ? ", loaded" : m.installed ? ", installed" : ", download"}${m.fits ? "" : ", " + m.reasons.join(" ")}\n`;
  const here = models.filter(m => m.installed && m.fits).sort((a, b) => Number(b.loaded) - Number(a.loaded) || a.sizeBytes - b.sizeBytes || a.id.localeCompare(b.id));
  const download = models.filter(m => !m.installed && m.fits).sort((a, b) => Number(b.id === recommended) - Number(a.id === recommended) || a.sizeBytes - b.sizeBytes || a.id.localeCompare(b.id));
  const tooBig = models.filter(m => !m.fits).sort((a, b) => a.sizeBytes - b.sizeBytes || a.id.localeCompare(b.id));
  const section = (title: string, rows: SetupModel[], max: number) => {
    if (!rows.length) return;
    io.progress(`${title}\n`);
    for (const m of rows.slice(0, max)) io.progress(plainText(line(m)));
    if (rows.length > max) io.progress(`  and ${rows.length - max} more; potluck models list shows all of them\n`);
  };
  section("Installed on this machine:", here, 20);
  section("Can be downloaded:", download, 8);
  section("Not possible on this machine right now:", tooBig, 4);
}

/** Ask which model to measure. Enter keeps the loaded one; q quits. Null means quit. */
async function pickModel(options: SetupOptions, models: SetupModel[], before: string | null, io: BenchmarkIO, halt: () => void): Promise<string | null> {
  listModels(models, options.recommendedModel, io);
  const fallback = before ?? options.recommendedModel;
  for (let attempt = 0; attempt < 3; attempt++) {
    const answer = await io.ask(`Model to benchmark${fallback ? ` [${fallback}]` : ""}, or q to quit: `);
    halt();
    if (answer === null) return null;
    const text = answer.trim();
    if (/^q(uit)?$/i.test(text)) return null;
    const choice = text || fallback;
    if (!choice) { io.progress("Type a model ID from the list, or q to quit.\n"); continue; }
    const model = models.find(m => m.id === choice);
    if (choice === before || (model && model.fits)) return choice;
    io.progress(plainText(model ? `${choice} is not possible on this machine right now: ${model.reasons.join(" ")}\n` : `${choice} is not in the list.\n`));
  }
  throw new RuntimeError("No model was chosen. Run potluck benchmark again, or pass --model <id>. Nothing was changed.");
}

function failureMessage(error: unknown): string {
  return error instanceof RuntimeError || error instanceof ControlError || error instanceof UsageError ? error.message
    : "The benchmark could not be completed. Check potluck logs.";
}
/** Say what a failed POST /models/benchmark means without echoing anything the engine wrote. */
function benchFailure(error: unknown, timeout: number): unknown {
  if (!(error instanceof ControlError)) return error;
  if (error.status === 503) return new RuntimeError("No model is loaded to benchmark.");
  if (error.status === 500) return new RuntimeError("The engine could not benchmark this model. Check potluck logs.");
  if (error.code === "timeout") return new RuntimeError(`The benchmark did not finish within ${Math.round(timeout / 1000)} s. The engine may still be running it. Raise --timeout (up to 300000) for a slow model.`);
  return error;
}

export async function benchmarkMachine(args: CliArgs, io: BenchmarkIO, deps: BenchmarkDeps = defaultDeps): Promise<BenchmarkReport> {
  const runs = args.runs ?? 3, signal = io.signal, quickTimeout = Math.min(args.timeout, 30000);
  const halt = () => { if (signal?.aborted) throw new Interrupted(); };
  const post = (timeout: number, withSignal: boolean): ModelCall => (route, body) => runtimeRequest(route, { timeout,
    ...(body !== undefined ? { method: "POST" as const, body } : {}), ...(withSignal && signal ? { signal } : {}) });
  const call = post(args.timeout, true);
  // After Ctrl-C the signal is spent, so cleanup requests carry none.
  const clean = post(args.timeout, false);
  const stopped = "Benchmark interrupted. Download progress and existing weights are preserved; rerun benchmark to continue.";
  const transfer: TransferContext = { call, io, deps, rerun: "benchmark", stopped };
  const cleanup: TransferContext = { call: clean, io: { ask: io.ask, progress: io.progress }, deps, rerun: "benchmark", stopped };
  const readInference = async (): Promise<InferenceInfo> => {
    try { return parseInference(await post(quickTimeout, true)("/health/inference")); }
    catch { halt(); return parseInference(null); } // an engine without this route still benchmarks; "where it runs" is then unreported
  };
  const readLoaded = async (): Promise<{ id: string | null; confirmed: boolean }> => {
    try {
      const options = await runtimeRequest<SetupOptions>("/models/setup-options", { timeout: quickTimeout });
      if (options.schemaVersion === 1) return { id: validID(options.currentModel) ? options.currentModel : null, confirmed: true };
    } catch { /* reported as unconfirmed */ }
    return { id: null, confirmed: false };
  };

  const report: BenchmarkReport = { schemaVersion: 1, status: "complete", machine: describeMachine({} as SetupOptions["hardware"]), model: null, localSkipped: null, ranOn: null, acceptance: null,
    gpuDevices: [], lastBenchmarkBefore: null, requestedRuns: runs, runs: [], summary: null, switch: null, loadedAtEnd: null, loadedAtEndConfirmed: false };
  try {
    // Nothing on the machine changes until the checks below pass; a failure here throws.
    const gateway = args.scope === "mine" ? await localGateway(args.timeout, signal) : null;
    const options = await readSetupOptions(quickTimeout, "benchmark", signal);
    halt();
    const models = options.models.filter(m => validID(m.id));
    const before = validID(options.currentModel) ? options.currentModel : null;
    // A loaded model with an ID this command cannot pass back to the engine could not be loaded again after a switch.
    if (options.currentModel && !before) throw new RuntimeError("The model loaded on this machine has an ID that benchmark cannot switch back to. Unload it with potluck models unload first. Nothing was changed.");
    report.machine = describeMachine(options.hardware);
    report.loadedAtEnd = before; report.loadedAtEndConfirmed = true;
    let inference = await readInference();
    if (io.interactive) {
      io.progress(`This machine: ${report.machine.description}\n${describeLoaded(before, inference.placement)}\n`);
      const checked = acceptanceFor(inference.acceptance, before);
      const answers = acceptanceLine(checked);
      if (answers) io.progress(answers + "\n");
      const note = gpuNote(inference.placement, inference.gpuDevices, checked);
      if (note) io.progress(note + "\n");
      const last = inference.lastBenchmark;
      if (before && last?.model === before && last.tokensPerSecond !== null) io.progress(`Last benchmark: ${fmtRate(last.tokensPerSecond)} tokens/s counted over the whole response${last.firstTokenMs !== null ? `, first token ${fmtMs(last.firstTokenMs)}` : ""}.\n`);
    }
    halt();

    const withMine = async (): Promise<BenchmarkReport> => {
      if (gateway && !signal?.aborted) {
        report.mine = emptyMine(gateway);
        try { await measureMine(report.mine, args, io, deps, gateway, runs); }
        catch (error) { if (error instanceof Interrupted || signal?.aborted) report.status = "interrupted"; else throw error; }
      }
      return report;
    };

    let id: string;
    if (args.model) id = args.model;
    else if (io.interactive) {
      const picked = await pickModel(options, models, before, io, halt);
      if (picked === null) { report.status = "quit"; return report; }
      id = picked;
    } else if (before) id = before;
    else if (gateway) {
      // Nothing to measure here, and the other machines are the point of --scope mine.
      report.localSkipped = { reason: "no_model_loaded", message: "No model is loaded on this machine, so only your other machines were measured." };
      return await withMine();
    } else {
      const installed = models.filter(m => m.installed).map(m => m.id).slice(0, 5);
      throw new RuntimeError(`No model is loaded on this machine. Pass --model <id> to choose one${installed.length ? ` (installed: ${installed.join(", ")})` : "; none are installed yet, so it will be downloaded"}.`);
    }

    const switching = id !== before;
    const target = models.find(m => m.id === id);
    if (switching) {
      if (!target) throw new RuntimeError("Model not found. Use potluck models list to choose an available model ID.");
      if (!target.fits) throw new RuntimeError(plainText(`${id} is not recommended for current resources: ${target.reasons.join(" ")} Nothing was changed; no model was substituted.`));
      if (before && inference.reported && !inference.placement) {
        throw new RuntimeError(`${before} is loaded in a way benchmark cannot switch back to (for example a split or pipeline model). Benchmark it as it is, or unload it first. Nothing was changed.`);
      }
      const steps = joinSteps([target.installed ? "" : `download ${gib(target.sizeBytes)}`, before ? `unload ${before} on this machine` : "", `load ${id}`].filter(Boolean));
      if (!args.yes) {
        if (!io.interactive) throw new RuntimeError(`To benchmark ${id}, this command would ${steps}. Pass --yes to allow that, or run it in a terminal to confirm. Nothing was changed.`);
        const answer = await io.ask(`To benchmark ${id}, this will ${steps}. Continue? [y/N] `);
        halt();
        if (!yes(answer)) { report.status = "quit"; report.model = id; return report; }
      }
    }

    report.model = id;
    const state = { switchStarted: false, loaded: false, ownDownload: false };
    let failure: unknown = null;
    try {
      if (switching) {
        halt();
        // Download first: a failed download leaves the loaded model alone.
        if (!target!.installed) await ensureInstalled(transfer, { id, sizeBytes: target!.sizeBytes }, () => call(`/models/${quote(id)}/install`, {}), own => { state.ownDownload = own; });
        halt();
        state.switchStarted = true; // the engine replaces the loaded model when this request starts
        await loadModel(transfer, id);
        state.loaded = true;
        halt();
        inference = await readInference(); // describe the model that is measured, not the one before
      }
      report.ranOn = inference.placement; report.gpuDevices = inference.gpuDevices; report.acceptance = acceptanceFor(inference.acceptance, id);
      report.lastBenchmarkBefore = inference.lastBenchmark?.model === id ? inference.lastBenchmark : null;
      const bench = async (): Promise<RunResult> => {
        halt();
        try { return parseRun(await call("/models/benchmark", {}), id); }
        catch (error) { halt(); throw benchFailure(error, args.timeout); }
      };
      const raw: RunResult[] = []; // unrounded, so the summary is not computed from rounded numbers
      io.progress(`Warming up ${id}...\n`);
      await bench(); // not counted: the first call pays for caches and clocks ramping up
      for (let i = 1; i <= runs; i++) {
        const run = await bench();
        raw.push(run); report.runs.push(roundRun(run));
        io.progress(`Run ${i} of ${runs}: ${run.generationTokensPerSecond !== null ? fmtRate(run.generationTokensPerSecond) + " tokens/s" : "speed not measured"}, first token ${fmtMs(run.ttftMs)}\n`);
      }
      if (raw.every(r => r.outputTokens === 0)) throw new RuntimeError("The model returned no text, so there is nothing to measure. Check it with potluck run --scope local.");
      report.summary = summarizeRuns(raw);
      if (report.acceptance?.status === "running") {
        // The check was still going when the model was described; the runs gave it time to finish.
        const later = acceptanceFor((await readInference()).acceptance, id);
        if (later && later.status !== "running") {
          report.acceptance = later;
          const answers = acceptanceLine(later);
          if (io.interactive && answers) io.progress(answers + "\n");
        }
      }
    } catch (error) { failure = error; }

    const interrupted = failure instanceof Interrupted || Boolean(signal?.aborted);
    if (state.ownDownload && interrupted) { try { await clean(`/models/${quote(id)}/cancel-download`, {}); } catch { /* progress shows the real state */ } }
    if (state.switchStarted) {
      let outcome: SwitchOutcome;
      // Put the machine back as it was: load the model that was loaded before, or unload the benchmark model when none was.
      const putBack = async (): Promise<boolean> => {
        try {
          if (before) await loadModel(cleanup, before);
          else { io.progress(`Unloading ${id}...\n`); await clean(`/models/${quote(id)}/unload`, {}); }
          return true;
        } catch { return false; }
      };
      if (isLoadPending(failure)) outcome = "load_pending"; // a second load would queue behind the first
      else if (!state.loaded && !interrupted) {
        // The load request ended with an error, so the engine is settled: ask what it holds before touching it.
        outcome = "load_failed";
        const now = before ? await readLoaded() : null;
        if (before && !(now!.confirmed && now!.id === before) && !await putBack()) outcome = "restore_failed";
      } else {
        let back: boolean;
        // A load cut off by Ctrl-C may still finish on the engine; putting back queues behind it.
        if (interrupted) back = true;
        else if (failure) back = !args.keep;
        else if (args.keep) back = false;
        else if (args.restore || !io.interactive) back = true;
        else if (before) {
          const answer = await io.ask(`Keep ${id} loaded, or switch back to ${before}? [k = keep, s = switch back] (s): `);
          back = !(answer !== null && /^k/i.test(answer.trim()));
        } else {
          const answer = await io.ask(`Keep ${id} loaded, or unload it? Nothing was loaded before. [k = keep, u = unload] (k): `);
          back = answer !== null ? /^u/i.test(answer.trim()) : Boolean(signal?.aborted); // end of input keeps; Ctrl-C puts back
        }
        outcome = !back ? "kept" : await putBack() ? (before ? "restored" : "unloaded") : (before ? "restore_failed" : "unload_failed");
      }
      report.switch = { from: before, to: id, outcome };
      const now = await readLoaded();
      report.loadedAtEnd = now.id; report.loadedAtEndConfirmed = now.confirmed;
    }
    if (failure) {
      if (interrupted) report.status = "interrupted";
      else { report.status = "failed"; report.error = { code: "benchmark_failed", message: failureMessage(failure) }; }
      return report;
    }

    return await withMine();
  } catch (error) {
    if (error instanceof Interrupted || signal?.aborted) { report.status = "interrupted"; return report; }
    throw error;
  }
}

/** `headerShown`: a terminal session already printed the machine and loaded model, so only a different measured model is described again. */
export function formatReport(r: BenchmarkReport, headerShown = false): string {
  if (r.status === "quit") return "Nothing was changed. No benchmark was run.\n";
  const lines = headerShown ? [] : [`This machine: ${r.machine.description}`];
  if (r.localSkipped) lines.push(r.localSkipped.message);
  if (r.model && !(headerShown && !r.switch)) {
    lines.push(describeLoaded(r.model, r.ranOn));
    const answers = acceptanceLine(r.acceptance);
    if (answers) lines.push(answers);
    const note = gpuNote(r.ranOn, r.gpuDevices, r.acceptance);
    if (note) lines.push(note);
    const last = r.lastBenchmarkBefore;
    if (last && last.tokensPerSecond !== null) lines.push(`Last benchmark of ${r.model} before this run: ${fmtRate(last.tokensPerSecond)} tokens/s counted over the whole response${last.firstTokenMs !== null ? `, first token ${fmtMs(last.firstTokenMs)}` : ""}.`);
  }
  if (r.summary) lines.push(...formatSummary(r.model!, r.runs.length, r.summary));
  if (r.summary && r.acceptance?.status === "failed") lines.push("These speeds are for a model whose answers did not pass the check on this setup.");
  if (r.status === "failed") lines.push(`Benchmark did not complete: ${r.error?.message}`);
  if (r.status === "interrupted") lines.push(`Interrupted.${r.runs.length ? ` ${r.runs.length} of ${r.requestedRuns} runs finished.` : ""}${r.switch ? "" : " The model loaded on this machine was not changed."}`);
  if (r.switch) {
    const { from, to, outcome } = r.switch;
    lines.push({ restored: `Switched to ${to} for the benchmark and back to ${from}.`,
      kept: from ? `Kept ${to} loaded; it replaced ${from}.` : `Kept ${to} loaded. No model was loaded before.`,
      unloaded: `Unloaded ${to} again, as no model was loaded before.`,
      restore_failed: `Could not switch back to ${from}. To load it yourself: potluck models load ${from}`,
      unload_failed: `Could not unload ${to}, and no model was loaded before. To unload it yourself: potluck models unload ${to}`,
      load_failed: `${to} could not be loaded.`,
      load_pending: `The load of ${to} was still running when the time limit ended, and it may finish later. No second load was started.`,
    }[outcome]);
    lines.push(r.loadedAtEndConfirmed ? `Loaded now: ${r.loadedAtEnd ?? "no model"}` : "Loaded now: could not be confirmed. Run potluck status.");
  }
  if (r.mine) lines.push(...formatMine(r.mine));
  return lines.join("\n") + "\n";
}

export function benchmarkExitCode(r: BenchmarkReport): number {
  if (r.status === "interrupted") return 130;
  if (r.status !== "complete") return 1;
  if (r.switch?.outcome === "restore_failed" || r.switch?.outcome === "unload_failed") return 1;
  // Nothing was measured here, so something must have been measured on the other machines.
  if (r.localSkipped) return r.mine?.rows.some(row => !row.error) ? 0 : 1;
  // Asked for the other machines and got nothing back at all.
  if (r.mine && (r.mine.error || (r.mine.rows.length > 0 && r.mine.rows.every(row => row.error)))) return 1;
  return 0;
}

/** CLI entry: wires Ctrl-C and, in a terminal, the questions. */
export async function runBenchmark(args: CliArgs, out: (text: string) => void): Promise<number> {
  const abort = new AbortController();
  let presses = 0;
  const interrupt = () => {
    if (presses++) { process.stderr.write("Stopped. Run potluck status to see which model is loaded.\n"); process.exit(130); }
    abort.abort();
    process.stderr.write("\nStopping. Press Ctrl-C again to stop right away.\n");
  };
  process.on("SIGINT", interrupt);
  const io: BenchmarkIO = {
    signal: abort.signal,
    interactive: !args.json && Boolean(process.stdin.isTTY && process.stderr.isTTY),
    progress: message => { if (!args.json) process.stderr.write(plainText(message)); },
    ask: async question => {
      const rl = createInterface({ input: process.stdin, output: process.stderr });
      rl.once("SIGINT", interrupt);
      try { return await rl.question(plainText(question), { signal: abort.signal }); } catch { return null; } finally { rl.close(); }
    },
  };
  try {
    const report = await benchmarkMachine(args, io);
    out(args.json ? JSON.stringify(report) + "\n" : plainText(formatReport(report, io.interactive)));
    return benchmarkExitCode(report);
  } finally { process.off("SIGINT", interrupt); }
}

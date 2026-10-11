import { setTimeout as sleep } from "node:timers/promises";
import type { CliArgs } from "./args.js";
import { ControlError } from "./control.js";
import { fmtRate, label, parseAcceptance, setupAcceptanceLine } from "./benchmark-stats.js";
import { loadCliPrefs, saveCliPrefs } from "./config.js";
import { plainText } from "./logs.js";
import { pinnedModel, readProjectModelLock } from "./model-lock.js";
import { runtimeRequest, RuntimeError } from "./runtime.js";
import type { OnboardingIO } from "./operational-setup.js";

/** What the engine measured for a model on this machine. Absent from engines that do not measure. */
export interface Measured { hostGiB: number | null; gpuGiB: number | null; contextTokens: number | null; tokensPerSecond: number | null; checkedAt: string | null }
export interface SetupModel {
  id: string; name: string; sizeBytes: number; installed: boolean; loaded: boolean;
  fits: boolean; reasons: string[]; estimatedWorkingGiB: number;
  /** Whether `fits` was judged from a measurement on this machine or from the size-based estimate. */
  fitBasis?: "measured" | "estimate"; measured?: Measured | null; checked?: "passed" | "failed" | null;
}
/** One of the engine's (at most three) picks for this machine, best first. */
export interface Recommendation { id: string; basis: string; tokensPerSecond: number | null }
export interface SetupOptions {
  schemaVersion: number; hardware: { ram_total_gb: number; ram_available_gb: number; gpu_name?: string; cpu_name?: string; os?: string; arch?: string };
  diskFreeBytes: number; currentModel: string | null; recommendedModel: string | null; models: SetupModel[];
  /** Absent from an engine that does not rank models. */
  recommended?: Recommendation[];
}
export const gib = (bytes: number) => `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
export const validID = (id: unknown): id is string => typeof id === "string" && /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}$/.test(id);

/**
 * The engine's picks as this client will show them: valid IDs of models in `models`, each once, at most three,
 * in the engine's order. An engine without `recommended`, or one that sends something else, gives none.
 */
export function parseRecommended(value: unknown, models: SetupModel[]): Recommendation[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>(), picks: Recommendation[] = [];
  for (const item of value) {
    const r = item && typeof item === "object" && !Array.isArray(item) ? item as Record<string, unknown> : {};
    if (!validID(r.id) || seen.has(r.id) || !models.some(m => m.id === r.id)) continue;
    seen.add(r.id);
    const speed = typeof r.tokensPerSecond === "number" && Number.isFinite(r.tokensPerSecond) && r.tokensPerSecond >= 0 ? r.tokensPerSecond : null;
    picks.push({ id: r.id, basis: label(r.basis, 40) ?? "", tokensPerSecond: speed });
    if (picks.length === 3) break;
  }
  return picks;
}
/** Why a model is picked, in the words a person reads. A basis this client does not know says nothing about proof. */
function whyPicked(pick: Recommendation, model: SetupModel): string {
  const rate = pick.tokensPerSecond !== null ? `${fmtRate(pick.tokensPerSecond)} tokens/s` : null;
  const proof = pick.basis === "proven_fast" || pick.basis === "proven_slow" ? `passed its answer check here${rate ? ", " + rate : ""}`
    : pick.basis === "proven_speed_unknown" ? "passed its answer check here, speed not measured yet"
    : pick.basis === "estimate" ? "not measured on this machine yet" : "";
  const need = model.fitBasis === "measured" && typeof model.measured?.hostGiB === "number" ? `needs ${model.measured.hostGiB.toFixed(1)} GiB, measured here` : "";
  return [proof, need].filter(Boolean).join("; ");
}
/** The "Recommended for this machine" block shown ahead of the full list. Empty when the engine gave no picks. */
export function recommendedLines(picks: Recommendation[], models: SetupModel[]): string[] {
  const rows = picks.flatMap(pick => {
    const model = models.find(m => m.id === pick.id);
    if (!model) return [];
    const why = whyPicked(pick, model);
    return [`  ${pick.id} (${gib(model.sizeBytes)}${model.loaded ? ", loaded" : model.installed ? ", installed" : ", download"})${why ? ": " + why : ""}`];
  });
  return rows.length ? ["Recommended for this machine:", ...rows] : [];
}
const SETUP_STOPPED = "Setup interrupted. Run setup again to resume; existing weights are preserved.";

/** Statuses for which the engine's `detail` on POST /models/{id}/test is a plain sentence for this machine's own user. */
const TEST_DETAIL: readonly number[] = [409, 503];
export const TEST_NOT_PASSED = "Model did not pass the local response check. Check potluck logs.";
/** POST /models/{id}/test. The engine's sentence for a refusal is kept on the error; `testFailure` words it. */
export const testRequest = (id: string, body: Record<string, unknown>, timeout: number): Promise<Record<string, unknown>> =>
  runtimeRequest(`/models/${encodeURIComponent(id)}/test`, { timeout, method: "POST", body, detailFor: TEST_DETAIL });
/**
 * A refused test shows the engine's own sentence and nothing else from the response. An error the engine words
 * differently, or not at all, stays the generic one it always was.
 */
export function testFailure(error: unknown): unknown {
  if (!(error instanceof ControlError) || !TEST_DETAIL.includes(error.status ?? 0)) return error;
  const detail = label(error.detail, 300);
  return detail ? new RuntimeError(detail) : error;
}

/** The engine's hardware, disk and model list. `feature` names the command in the upgrade message. */
export async function readSetupOptions(timeout: number, feature: string, signal?: AbortSignal): Promise<SetupOptions> {
  let options: SetupOptions;
  try { options = await runtimeRequest<SetupOptions>("/models/setup-options", { timeout, ...(signal ? { signal } : {}) }); }
  catch (error) {
    if (error instanceof ControlError && error.status === 404) throw new RuntimeError(`Upgrade the Potluck runtime to use ${feature}.`);
    throw error;
  }
  if (options.schemaVersion !== 1 || !Array.isArray(options.models) || !options.hardware) throw new RuntimeError("Runtime returned invalid model setup options.");
  return options;
}

export type ModelCall = (route: string, body?: unknown) => Promise<Record<string, unknown>>;
export interface ModelDeps { wait: (signal?: AbortSignal) => Promise<unknown>; now: () => number }
/** What the shared install and load steps need from the command using them. `rerun` and `stopped` put that command's name in the messages. */
export interface TransferContext { call: ModelCall; io?: OnboardingIO; deps: ModelDeps; rerun: string; stopped: string }
export function checkStopped(ctx: TransferContext): void {
  if (ctx.io?.signal?.aborted) throw new RuntimeError(ctx.stopped);
}
/** True for the error `loadModel` throws when the request timed out: the engine may still be loading, so no second load is started. */
export const isLoadPending = (error: unknown): boolean => error instanceof RuntimeError && (error as { loadPending?: boolean }).loadPending === true;

/**
 * Start a download with `start` (or follow one already running) and wait until
 * the weights are complete. `track(true)` marks a transfer this command started,
 * so an interrupt cancels only that one; it is `track(false)` again once done.
 */
export async function ensureInstalled(ctx: TransferContext, model: { id: string; sizeBytes: number },
  start: () => Promise<Record<string, unknown>>, track: (own: boolean) => void): Promise<void> {
  const { id } = model, { io, deps } = ctx;
  const prefix = `/models/${encodeURIComponent(id)}`;
  io?.progress(`Preparing ${id}...\n`);
  const started = await start();
  track(started.status === "downloading");
  if (!["already_installed", "verified"].includes(String(started.status))) {
    if (!["downloading", "already_downloading"].includes(String(started.status))) throw new RuntimeError("Unexpected download state. Check potluck models progress " + id);
    const deadline = deps.now() + 60 * 60 * 1000;
    let last = "";
    while (true) {
      checkStopped(ctx);
      if (deps.now() > deadline) throw new RuntimeError(`Still waiting for the download. Use models progress, then rerun ${ctx.rerun} to finish.`);
      const progress = await ctx.call(prefix + "/download-progress");
      if (progress.status === "complete") break;
      if (["error", "cancelled"].includes(String(progress.status))) throw new RuntimeError(`Download ${progress.status}. Check potluck logs; rerun ${ctx.rerun} to resume.`);
      if (!["downloading", "verifying", "not_started"].includes(String(progress.status))) throw new RuntimeError(`Download state is unavailable. Rerun ${ctx.rerun} after checking models progress.`);
      const done = typeof progress.bytes_downloaded === "number" ? progress.bytes_downloaded : 0;
      const total = typeof progress.bytes_total === "number" ? progress.bytes_total : model.sizeBytes;
      const percent = Math.min(100, Math.floor(done / Math.max(total, 1) * 100));
      const line = progress.status === "verifying" ? "Verifying model checksum" : `Downloading ${id}: ${percent}% (${gib(done)} / ${gib(total)})`;
      if (line !== last) { io?.progress(line + "\n"); last = line; }
      await deps.wait(io?.signal);
    }
  }
  track(false);
}

/** Load an installed model on this machine. The engine replaces whatever is loaded. */
export async function loadModel(ctx: TransferContext, id: string): Promise<void> {
  ctx.io?.progress(`Loading ${id} locally...\n`);
  try { await ctx.call(`/models/${encodeURIComponent(id)}/load`, {}); }
  catch (error) {
    if (error instanceof ControlError && error.code === "timeout") throw Object.assign(new RuntimeError(`The load is still pending or timed out. Check potluck status, then rerun ${ctx.rerun}; no second load was started.`), { loadPending: true });
    throw error;
  }
}

export async function setupModel(args: CliArgs, io?: OnboardingIO, deps: ModelDeps = {
  wait: (signal?: AbortSignal) => sleep(1000, undefined, { signal }), now: () => Date.now(),
}): Promise<Record<string, unknown>> {
  const lock = await readProjectModelLock(process.cwd());
  const selected = pinnedModel(lock, args.model);
  const options = await readSetupOptions(args.timeout, "guided model setup");
  io?.progress(plainText(`Memory: ${options.hardware.ram_available_gb} GiB available / ${options.hardware.ram_total_gb} GiB total. Model disk space: ${gib(options.diskFreeBytes)}.\n`));
  // A pinned model may be absent from a refreshed catalog. Keep its identity and
  // source, using its recorded size for the conservative fit estimate.
  if (lock) {
    const existing = options.models.find(m => m.id === lock.model.id);
    const working = lock.model.sizeBytes / 1024 ** 3 * 1.2 + 1.5;
    const memoryFits = existing?.loaded || options.hardware.ram_available_gb >= working;
    const diskFits = existing?.installed || options.diskFreeBytes >= lock.model.sizeBytes + 256 * 1024 ** 2;
    const pinned = { id: lock.model.id, name: lock.model.id, sizeBytes: lock.model.sizeBytes,
      installed: existing?.installed ?? false, loaded: existing?.loaded ?? false, estimatedWorkingGiB: working,
      fits: Boolean(memoryFits && diskFits),
      reasons: ["Free more memory or disk space to install this project's pinned model."] };
    options.models = [...options.models.filter(m => m.id !== lock.model.id), pinned];
  }
  let id = selected ?? options.currentModel ?? options.recommendedModel;
  const picks = parseRecommended(options.recommended, options.models), found = picks.length ? { recommended: picks } : {};
  if (io && !selected && !args.withModel) {
    for (const line of recommendedLines(picks, options.models)) io.progress(plainText(line + "\n"));
    const choices = [...options.models].sort((a, b) => Number(b.fits) - Number(a.fits) || Number(b.installed) - Number(a.installed) || a.sizeBytes - b.sizeBytes);
    for (const m of choices.slice(0, 8)) io.progress(plainText(`  ${m.id}: ${gib(m.sizeBytes)}${m.loaded ? ", loaded" : m.installed ? ", installed" : " download"}${m.fits ? "" : ", " + m.reasons.join(" ")}\n`));
    const answer = await io.ask(`Model ID${id ? ` [${id}]` : ""}, or skip: `);
    if (answer === null) throw new RuntimeError("Setup interrupted. Run setup again to resume.");
    if (answer.trim().toLowerCase() === "skip") return { status: "skipped", ...found };
    id = answer.trim() || id;
  }
  if (!id || !validID(id)) throw new RuntimeError("No fitting model was found. Free memory/disk space or choose an installed model with setup --model <id>.");
  const model = options.models.find(m => m.id === id);
  if (!model) throw new RuntimeError("Model not found. Use potluck models list to choose an available model ID.");
  if (!model.fits) throw new RuntimeError(plainText(`${id} is not recommended for current resources: ${model.reasons.join(" ")} Free resources and rerun setup; no model was substituted.`));
  if (io && !args.withModel && !args.model) {
    const action = `${model.installed ? "Use" : "Download " + gib(model.sizeBytes) + " for"} ${id}, load it locally, and run a short test? [y/N] `;
    const answer = await io.ask(action);
    if (answer === null) throw new RuntimeError("Setup interrupted. Run setup again to resume.");
    if (!/^(y|yes)$/i.test(answer.trim())) return { status: "skipped", ...found };
  }
  const prefix = `/models/${encodeURIComponent(id)}`;
  let ownDownload = false;
  const request = (route: string, body?: unknown) => runtimeRequest(route, {
    timeout: args.timeout, ...(body !== undefined ? { method: "POST" as const, body } : {}),
  });
  const transfer: TransferContext = { call: request, io, deps, rerun: "setup", stopped: SETUP_STOPPED };
  checkStopped(transfer);
  try {
    if (lock || !model.installed) {
      await ensureInstalled(transfer, { id, sizeBytes: model.sizeBytes }, async () => {
        try { return await request(prefix + (lock ? "/install-locked" : "/install"), lock?.model ?? {}); }
        catch (error) {
          if (lock && error instanceof ControlError && error.status === 409) {
            let progress: Record<string, unknown> = {};
            try { progress = await request(prefix + "/download-progress"); } catch { /* conflict, not an active transfer */ }
            if (!["downloading", "verifying"].includes(String(progress.status))) throw new RuntimeError("Pinned model conflicts with installed weights. Run models sync to inspect the conflict. No weights were replaced.");
            return { status: "already_downloading" }; // follow, never cancel someone else's transfer
          }
          throw error;
        }
      }, own => { ownDownload = own; });
      if (lock) {
        const verified = await request(prefix + "/install-locked", lock.model);
        if (verified.model_lock_version !== 1 || verified.status !== "verified" || verified.sha256 !== lock.model.sha256) throw new RuntimeError("Downloaded model did not verify against the project lock.");
      }
    }
    checkStopped(transfer);
    if (!model.loaded) await loadModel(transfer, id);
    checkStopped(transfer);
    io?.progress("Checking a real local response...\n");
    let test: Record<string, unknown>;
    try { test = await testRequest(id, lock ? { sha256: lock.model.sha256 } : {}, args.timeout); } catch (error) { throw testFailure(error); }
    if (test.status !== "passed" || test.model !== id || test.scope !== "local") throw new RuntimeError(TEST_NOT_PASSED);
    const acceptance = parseAcceptance(test.acceptance), checked = setupAcceptanceLine(acceptance);
    if (checked) io?.progress(label(checked, 400) + "\n");
    // Persist only a successful selection; project-specific intent stays in its lock.
    if (!lock) { const prefs = await loadCliPrefs(); await saveCliPrefs({ ...prefs, model: id }); }
    io?.progress(`Ready: ${id} returned a local response.\n`);
    return { status: "ready", model: id, scope: "local", durationMs: test.durationMs, outputTokens: test.outputTokens,
      pinned: Boolean(lock), gatewayRequiredForAgent: true, ...(acceptance ? { acceptance } : {}), ...found };
  } catch (error) {
    if (io?.signal?.aborted) {
      if (ownDownload) { try { await request(prefix + "/cancel-download", {}); } catch { /* progress command reports actual state */ } }
      throw new RuntimeError("Setup interrupted. Download progress and existing weights are preserved; rerun setup to continue.");
    }
    throw error;
  }
}

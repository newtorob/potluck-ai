import { setTimeout as sleep } from "node:timers/promises";
import type { CliArgs } from "./args.js";
import { ControlError } from "./control.js";
import { loadCliPrefs, saveCliPrefs } from "./config.js";
import { plainText } from "./logs.js";
import { pinnedModel, readProjectModelLock } from "./model-lock.js";
import { runtimeRequest, RuntimeError } from "./runtime.js";
import type { OnboardingIO } from "./operational-setup.js";

export interface SetupModel {
  id: string; name: string; sizeBytes: number; installed: boolean; loaded: boolean;
  fits: boolean; reasons: string[]; estimatedWorkingGiB: number;
}
export interface SetupOptions {
  schemaVersion: number; hardware: { ram_total_gb: number; ram_available_gb: number; gpu_name?: string };
  diskFreeBytes: number; currentModel: string | null; recommendedModel: string | null; models: SetupModel[];
}
const gib = (bytes: number) => `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
const validID = (id: unknown): id is string => typeof id === "string" && /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}$/.test(id);
function interrupted(signal?: AbortSignal): void {
  if (signal?.aborted) throw new RuntimeError("Setup interrupted. Run setup again to resume; existing weights are preserved.");
}

export async function setupModel(args: CliArgs, io?: OnboardingIO, deps = {
  wait: (signal?: AbortSignal) => sleep(1000, undefined, { signal }), now: () => Date.now(),
}): Promise<Record<string, unknown>> {
  const lock = await readProjectModelLock(process.cwd());
  const selected = pinnedModel(lock, args.model);
  let options: SetupOptions;
  try { options = await runtimeRequest<SetupOptions>("/models/setup-options", { timeout: args.timeout }); }
  catch (error) {
    if (error instanceof ControlError && error.status === 404) throw new RuntimeError("Upgrade the Potluck runtime to use guided model setup.");
    throw error;
  }
  if (options.schemaVersion !== 1 || !Array.isArray(options.models) || !options.hardware) throw new RuntimeError("Runtime returned invalid model setup options.");
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
  if (io && !selected && !args.withModel) {
    const choices = [...options.models].sort((a, b) => Number(b.fits) - Number(a.fits) || Number(b.installed) - Number(a.installed) || a.sizeBytes - b.sizeBytes);
    for (const m of choices.slice(0, 8)) io.progress(plainText(`  ${m.id}: ${gib(m.sizeBytes)}${m.loaded ? ", loaded" : m.installed ? ", installed" : " download"}${m.fits ? "" : ", " + m.reasons.join(" ")}\n`));
    const answer = await io.ask(`Model ID${id ? ` [${id}]` : ""}, or skip: `);
    if (answer === null) throw new RuntimeError("Setup interrupted. Run setup again to resume.");
    if (answer.trim().toLowerCase() === "skip") return { status: "skipped" };
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
    if (!/^(y|yes)$/i.test(answer.trim())) return { status: "skipped" };
  }
  interrupted(io?.signal);
  const prefix = `/models/${encodeURIComponent(id)}`;
  let ownDownload = false;
  const request = (route: string, body?: unknown) => runtimeRequest(route, {
    timeout: args.timeout, ...(body !== undefined ? { method: "POST" as const, body } : {}),
  });
  try {
    if (lock || !model.installed) {
      io?.progress(`Preparing ${id}...\n`);
      let started: Record<string, unknown>;
      try { started = await request(prefix + (lock ? "/install-locked" : "/install"), lock?.model ?? {}); }
      catch (error) {
        if (lock && error instanceof ControlError && error.status === 409) {
          let progress: Record<string, unknown> = {};
          try { progress = await request(prefix + "/download-progress"); } catch { /* conflict, not an active transfer */ }
          if (!["downloading", "verifying"].includes(String(progress.status))) throw new RuntimeError("Pinned model conflicts with installed weights. Run models sync to inspect the conflict. No weights were replaced.");
          started = { status: "already_downloading" }; // follow, never cancel someone else's transfer
        } else throw error;
      }
      ownDownload = started.status === "downloading";
      if (!["already_installed", "verified"].includes(String(started.status))) {
        if (!["downloading", "already_downloading"].includes(String(started.status))) throw new RuntimeError("Unexpected download state. Check potluck models progress " + id);
        const deadline = deps.now() + 60 * 60 * 1000;
        let last = "";
        while (true) {
          interrupted(io?.signal);
          if (deps.now() > deadline) throw new RuntimeError("Still waiting for the download. Use models progress, then rerun setup to finish.");
          const progress = await request(prefix + "/download-progress");
          if (progress.status === "complete") break;
          if (["error", "cancelled"].includes(String(progress.status))) throw new RuntimeError(`Download ${progress.status}. Check potluck logs; rerun setup to resume.`);
          if (!["downloading", "verifying", "not_started"].includes(String(progress.status))) throw new RuntimeError("Download state is unavailable. Rerun setup after checking models progress.");
          const done = typeof progress.bytes_downloaded === "number" ? progress.bytes_downloaded : 0;
          const total = typeof progress.bytes_total === "number" ? progress.bytes_total : model.sizeBytes;
          const percent = Math.min(100, Math.floor(done / Math.max(total, 1) * 100));
          const line = progress.status === "verifying" ? "Verifying model checksum" : `Downloading ${id}: ${percent}% (${gib(done)} / ${gib(total)})`;
          if (line !== last) { io?.progress(line + "\n"); last = line; }
          await deps.wait(io?.signal);
        }
      }
      ownDownload = false;
      if (lock) {
        const verified = await request(prefix + "/install-locked", lock.model);
        if (verified.model_lock_version !== 1 || verified.status !== "verified" || verified.sha256 !== lock.model.sha256) throw new RuntimeError("Downloaded model did not verify against the project lock.");
      }
    }
    interrupted(io?.signal);
    if (!model.loaded) {
      io?.progress(`Loading ${id} locally...\n`);
      try { await request(prefix + "/load", {}); }
      catch (error) {
        if (error instanceof ControlError && error.code === "timeout") throw new RuntimeError("The load is still pending or timed out. Check potluck status, then rerun setup; no second load was started.");
        throw error;
      }
    }
    interrupted(io?.signal);
    io?.progress("Checking a real local response...\n");
    const test = await request(prefix + "/test", lock ? { sha256: lock.model.sha256 } : {});
    if (test.status !== "passed" || test.model !== id || test.scope !== "local") throw new RuntimeError("Model did not pass the local response check. Check potluck logs.");
    // Persist only a successful selection; project-specific intent stays in its lock.
    if (!lock) { const prefs = await loadCliPrefs(); await saveCliPrefs({ ...prefs, model: id }); }
    io?.progress(`Ready: ${id} returned a local response.\n`);
    return { status: "ready", model: id, scope: "local", durationMs: test.durationMs, outputTokens: test.outputTokens,
      pinned: Boolean(lock), gatewayRequiredForAgent: true };
  } catch (error) {
    if (io?.signal?.aborted) {
      if (ownDownload) { try { await request(prefix + "/cancel-download", {}); } catch { /* progress command reports actual state */ } }
      throw new RuntimeError("Setup interrupted. Download progress and existing weights are preserved; rerun setup to continue.");
    }
    throw error;
  }
}

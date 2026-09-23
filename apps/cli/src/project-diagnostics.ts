import type { GatewayConfig, Scope } from "./config.js";
import type { Check } from "./diagnostics.js";
import { requestJSON, ControlError } from "./control.js";
import { readProjectModelLock } from "./model-lock.js";
import { runtimeRequest } from "./runtime.js";

export async function diagnoseProject(cfg: GatewayConfig, scope: Scope, timeout: number, cwd = process.cwd(), signal?: AbortSignal): Promise<Check[]> {
  let lock;
  try { lock = await readProjectModelLock(cwd); }
  catch { return [{ id: "project_lock", state: "error", message: "The project model lock is invalid or unreadable.", action: "Review potluck.lock; do not replace it with a global default." }]; }
  if (!lock) return [];
  const checks: Check[] = [{ id: "project_lock", state: "ok", message: `Project pins ${lock.model.id} (SHA-256 ${lock.model.sha256}).` }];
  let route: unknown;
  try {
    const caps = await requestJSON({ url: `${cfg.baseURL}/potluck/capabilities`, token: cfg.apiKey, timeout, signal });
    if (caps.model_lock_version !== 1 || caps.artifact_format !== "gguf" || !Array.isArray(caps.scopes) || !caps.scopes.includes(scope)) throw new Error("incompatible");
    checks.push({ id: "lock_protocol", state: "ok", message: "Gateway supports exact GGUF model locks for this scope." });
    const state = await requestJSON({ url: `${cfg.baseURL}/potluck/readiness`, token: cfg.apiKey, timeout, signal, method: "POST",
      body: { model: lock.model.id, sha256: lock.model.sha256, scope } });
    if (state.model_lock_version !== 1 || state.model !== lock.model.id || state.scope !== scope) throw new Error("incompatible");
    route = state.ready === true && ["local", "household"].includes(String(state.route)) ? state.route : null;
    checks.push({ id: "pinned_route", state: route ? "ok" : "error", message: route
      ? `Exact pinned weights are loaded ${route === "local" ? "locally" : "on an eligible own machine"}; checked without generating text.`
      : "No eligible loaded model matches this project's ID and SHA-256.",
      ...(!route ? { action: "Run potluck models sync, then load the pinned model here or on an eligible own machine." } : {}) });
  } catch {
    checks.push({ id: "pinned_route", state: "error", message: "Could not verify project-lock compatibility and readiness through this gateway.",
      action: "Check gateway authentication and upgrade the runtime to a version supporting project diagnostics." });
  }
  // These inspect this machine only; absence is normal when an own machine serves the pin.
  const results = await Promise.allSettled([
    runtimeRequest(`/models/${encodeURIComponent(lock.model.id)}/identity`, { timeout, signal }),
    runtimeRequest("/models/setup-options", { timeout, signal }),
  ]);
  const identity = results[0];
  if (identity.status === "fulfilled") {
    const model = identity.value.model as Record<string, unknown> | undefined;
    const same = model?.sha256 === lock.model.sha256 && model?.sizeBytes === lock.model.sizeBytes && model?.id === lock.model.id;
    checks.push({ id: "installed_weights", state: same ? "ok" : route === "household" ? "warning" : "error",
      message: same ? "Installed local weights match the project's checksum and byte size." : "Local weights differ from the project lock.",
      ...(!same ? { action: "Move conflicting weights aside deliberately, then run potluck models sync. Existing files are never overwritten automatically." } : {}) });
  } else checks.push({ id: "installed_weights", state: route === "local" || route === "household" ? "warning" : "error",
    message: identity.reason instanceof ControlError && identity.reason.status === 404 ? "Pinned weights are not installed here." : "Could not verify installed local weights through the private runtime API.",
    action: "Use potluck models sync for a local copy; verify that the local runtime is up." });
  const fit = results[1];
  if (fit.status === "fulfilled") {
    const hardware = fit.value.hardware as Record<string, unknown> | undefined;
    const need = lock.model.sizeBytes / 1024 ** 3 * 1.2 + 1.5;
    const available = hardware?.ram_available_gb;
    const loaded = route === "local";
    const enough = loaded || typeof available === "number" && available >= need;
    checks.push({ id: "hardware_fit", state: enough ? "ok" : "warning", message: loaded
      ? "Pinned model is already loaded locally. Inference still rechecks identity for each request."
      : `Pinned weights need an estimated ${need.toFixed(1)} GiB available RAM; ${typeof available === "number" ? available.toFixed(1) + " GiB available" : "available memory could not be measured"}.`,
      ...(!enough ? { action: "Free memory or use an eligible own machine. Run setup to test actual local inference." } : {}) });
  } else checks.push({ id: "hardware_fit", state: "warning", message: "Local hardware-fit data is unavailable.", action: "Run potluck up and retry doctor." });
  return checks;
}

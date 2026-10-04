import { manageModelLock } from "./model-lock.js";
import { availableModels } from "./availability.js";
import type { CliArgs } from "./args.js";
import { ControlError } from "./control.js";
import { runtimeRequest } from "./runtime.js";

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ControlError("invalid_response", "Service returned an invalid response.");
  return value as Record<string, unknown>;
}

/** Allowlist output. Settings contain credentials; never print the raw object. */
export async function manageGateway(args: CliArgs): Promise<Record<string, unknown>> {
  if (args.action === "enable") await runtimeRequest("/settings/gateway/enable", { method: "POST", timeout: args.timeout });
  if (args.action === "rotate") await runtimeRequest("/settings/gateway/rotate", { method: "POST", timeout: args.timeout });
  if (args.action === "disable") await runtimeRequest("/settings", { method: "PATCH", body: { gateway: { enabled: false } }, timeout: args.timeout });
  const settings = await runtimeRequest("/settings", { timeout: args.timeout });
  const gateway = object(settings.gateway);
  return { schemaVersion: 1, enabled: gateway.enabled === true, keyConfigured: typeof gateway.api_key === "string" && gateway.api_key.length > 0,
    rotated: args.action === "rotate" };
}

export async function manageModels(args: CliArgs): Promise<Record<string, unknown>> {
  if (args.action === "available") return { ...await availableModels(args.timeout) };
  if (["pin", "lock", "sync", "unpin"].includes(args.action!)) return manageModelLock(args);
  if (args.action === "autoload") {
    const settings = await runtimeRequest("/settings", args.target
      ? { method: "PATCH", body: { ui: { "potluck.autoload_on_startup": String(args.target === "on") } }, timeout: args.timeout }
      : { timeout: args.timeout });
    const runtime = object(settings.runtime);
    const ui = object(settings.ui);
    return { schemaVersion: 1, action: "autoload", enabled: ui["potluck.autoload_on_startup"] !== "false",
      model: typeof runtime.startup_model_id === "string" ? runtime.startup_model_id : null };
  }
  if (args.action === "list") {
    const rows = await runtimeRequest<unknown[]>("/models", { timeout: args.timeout, allowArray: true });
    if (!Array.isArray(rows)) throw new ControlError("invalid_response", "Model listing is invalid.");
    return { schemaVersion: 1, models: rows.map(row => {
      const model = object(row);
      return { id: typeof model.slug === "string" ? model.slug : "unknown",
        name: typeof model.name === "string" ? model.name : null,
        installed: model.installed === true, sizeBytes: typeof model.size_bytes === "number" ? model.size_bytes : null };
    }) };
  }
  const suffix = args.action === "progress" ? "download-progress" : args.action === "cancel" ? "cancel-download" : args.action;
  const result = await runtimeRequest(`/models/${encodeURIComponent(args.target!)}/${suffix}`, {
    method: args.action === "progress" ? "GET" : "POST", timeout: args.timeout,
  });
  // Do not expose backend exception strings/download URLs. Keep stable state fields.
  const report: Record<string, unknown> = { schemaVersion: 1, model: args.target, action: args.action };
  for (const key of ["status", "bytes_downloaded", "bytes_total", "speed_bps", "eta_seconds"]) {
    const value = result[key];
    if (typeof value === "number" || (key === "status" && typeof value === "string")) report[key] = value;
  }
  if (typeof report.bytes_downloaded === "number" && typeof report.bytes_total === "number" && report.bytes_total > 0) {
    report.progress = Math.round(report.bytes_downloaded / report.bytes_total * 100);
  }
  return report;
}

import { readProjectModelLock } from "./model-lock.js";
import { runtimeRequest } from "./runtime.js";

export interface Availability {
  schemaVersion: 1; action?: string; scope: "mine"; checkedAt: number;
  local: { model: string | null; sha256: string | null };
  models: Array<{ id: string; sha256: string | null; local: boolean; idleMachineCount: number;
    machines: Array<{ name: string; meshIp: string; idle: boolean }>; matchesProjectPin?: boolean }>;
  machines: Array<{ name: string; meshIp: string; responding: boolean; splitReady: boolean; splitCompatible: boolean; backend: string | null; freeMemoryBytes: number | null }>;
  additionalModels: number; splitWorkerCount: number; splitEngineAvailable: boolean; truncated: boolean; note: string;
  projectPin?: { id: string; sha256: string; available: boolean } | null;
}

export async function availableModels(timeout: number): Promise<Availability> {
  const lock = await readProjectModelLock(process.cwd());
  const report = await runtimeRequest<Availability>("/models/available", { timeout });
  report.action = "available";
  report.models = report.models.map(model => ({ ...model,
    ...(lock ? { matchesProjectPin: model.id === lock.model.id && model.sha256 === lock.model.sha256 } : {}) }));
  report.projectPin = lock ? { id: lock.model.id, sha256: lock.model.sha256,
    available: report.models.some(model => model.matchesProjectPin) } : null;
  return report;
}

export function formatAvailability(report: Availability): string {
  const lines = ["Your machines · models ready now", ""];
  if (!report.models.length) lines.push("No models are serving yet. Load a model locally or on a paired computer with sharing enabled.");
  for (const model of report.models) {
    const sources = [model.local ? "this computer" : "", ...model.machines.map(m => m.name)].filter(Boolean);
    lines.push(`${model.id}  ${model.sha256 ? model.sha256.slice(0, 12) : "version unverified"}`,
      `  ${sources.join(", ")}${model.machines.length ? ` · ${model.idleMachineCount} remote machine(s) idle` : ""}${model.matchesProjectPin ? " · matches project pin" : ""}`);
  }
  lines.push("", `${report.additionalModels} additional model choice(s) across your machines.`);
  if (report.projectPin) lines.push(`Project pin: ${report.projectPin.id} — ${report.projectPin.available ? "exact bytes available" : "not currently available; no model substitution"}.`);
  lines.push("", `Split workers ready: ${report.splitWorkerCount}`);
  for (const machine of report.machines) {
    lines.push(`  ${machine.name}: ${machine.splitReady ? `ready (${machine.backend || "unknown backend"})` : !machine.responding ? "runtime not responding" : "split worker unavailable"}`);
  }
  if (!report.splitEngineAvailable) lines.push("Install a split-capable runtime on this head computer to use split workers.");
  else if (report.splitWorkerCount) lines.push("Preview: potluck split plan <pinned-model> --peer <owned-machine>");
  if (report.truncated) lines.push("Showing the first 32 reachable machines; discovery is bounded.");
  lines.push("", "Use --scope mine explicitly to run on your machines. This view does not change routing or sharing.", report.note);
  return lines.join("\n");
}

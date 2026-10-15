import { label } from "./benchmark-stats.js";
import { gib, parseRecommended, readSetupOptions, recommendedLines, validID, type SetupModel, type SetupOptions } from "./model-setup.js";
import { RuntimeError } from "./runtime.js";
import { isModelUse, useLabel, type ModelUse } from "./model-use.js";

export interface ModelRecommendations extends SetupOptions {
  action: "recommend";
  status: "ready" | "unavailable";
}
const number = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;

/** One read of this machine's engine. No model loads, downloads or preferences. */
export async function recommendModels(timeout: number, useCase: ModelUse = "general"): Promise<ModelRecommendations> {
  const options = await readSetupOptions(timeout, "model recommendations", undefined, useCase);
  if (!Array.isArray(options.recommended)) throw new RuntimeError("Upgrade the Potluck runtime to use model recommendations.");
  // Keep the public report bounded to useful fields, including in JSON output.
  const models: SetupModel[] = options.models.filter(m => m && validID(m.id) && (number(m.sizeBytes) ?? 0) > 0).map(m => ({
    id: m.id, name: label(m.name, 120) ?? m.id, sizeBytes: m.sizeBytes, installed: m.installed === true, loaded: m.loaded === true,
    fits: m.fits === true, reasons: Array.isArray(m.reasons) ? m.reasons.flatMap(r => label(r, 300) ?? []).slice(0, 5) : [],
    estimatedWorkingGiB: number(m.estimatedWorkingGiB) ?? 0,
    checked: m.checked === "passed" || m.checked === "failed" ? m.checked : null,
    fitBasis: m.fitBasis === "measured" ? "measured" : "estimate",
    measured: m.measured ? { hostGiB: number(m.measured.hostGiB), gpuGiB: number(m.measured.gpuGiB),
      contextTokens: number(m.measured.contextTokens), tokensPerSecond: number(m.measured.tokensPerSecond), checkedAt: label(m.measured.checkedAt, 80) } : null,
    recommendationReason: label(m.recommendationReason, 200),
    useCases: Array.isArray(m.useCases) ? [...new Set(m.useCases.filter(isModelUse))] : [],
  }));
  const recommended = parseRecommended(options.recommended, models), h = options.hardware;
  return { schemaVersion: 1, action: "recommend", useCase, status: recommended.length ? "ready" : "unavailable",
    hardware: { os: label(h.os, 40), arch: label(h.arch, 40), cpu_name: label(h.cpu_name, 120), gpu_name: label(h.gpu_name, 120),
      ram_total_gb: number(h.ram_total_gb), ram_available_gb: number(h.ram_available_gb), gpu_vram_gb: number(h.gpu_vram_gb),
      inference_gpu_available: typeof h.inference_gpu_available === "boolean" ? h.inference_gpu_available : null,
      inference_gpu_vram_gb: number(h.inference_gpu_vram_gb) },
    diskFreeBytes: number(options.diskFreeBytes) ?? 0, currentModel: validID(options.currentModel) ? options.currentModel : null,
    recommendedModel: recommended[0]?.id ?? null, recommended, models };
}

export function formatRecommendations(report: ModelRecommendations): string {
  const h = report.hardware, memory = (value: number | null | undefined) => value == null ? "unknown" : `${value.toFixed(1)} GiB`;
  const unified = h.os === "Darwin" && h.arch === "arm64";
  const gpu = h.gpu_name ? `${h.gpu_name}${unified ? " (shares system memory)" : ` (${memory(h.gpu_vram_gb)} VRAM total)`}` : "not detected";
  const lines = ["This machine:", `  CPU: ${h.cpu_name || "unknown"}`, `  GPU: ${gpu}`,
    `  Memory: ${memory(h.ram_available_gb)} available / ${memory(h.ram_total_gb)} total${unified ? ", unified" : ""}`,
    `  Model disk space: ${gib(report.diskFreeBytes)}`];
  if (h.inference_gpu_available === false) lines.push("  The runtime is using CPU execution.");
  else if (h.os === "Linux" && h.inference_gpu_available !== true) lines.push("  GPU acceleration has not been verified by the runtime.");
  lines.push("");
  if (report.recommended?.length) {
    const useCase = report.useCase ?? "general";
    lines.push(`Intended use: ${useLabel[useCase]}`, ...recommendedLines(report.recommended, report.models, useCase));
    lines.push("", `First choice: ${report.recommendedModel}`, `To download if needed, load and test it: potluck setup --model ${report.recommendedModel}`);
    lines.push("Estimates use catalog requirements and memory headroom; actual speed and context capacity need a local test.");
    if (useCase !== "general") lines.push("Use-case matches are catalog guidance. Task quality and tool reliability have not been benchmarked here.");
  } else {
    lines.push("No model can be recommended with the current resources and test results.");
    for (const m of report.models.filter(m => !m.fits || m.checked === "failed").slice(0, 3)) {
      lines.push(`  ${m.id}: ${m.checked === "failed" ? "failed its local answer check" : m.reasons.join(" ") || "does not fit"}`);
    }
    lines.push("Free memory or model disk space, then run potluck models recommend again.");
  }
  return lines.join("\n");
}

/** Numbers and wording for `potluck benchmark`. Engine text is bounded and stripped before it can reach a terminal or JSON. */
import { plainText } from "./logs.js";
import { RuntimeError } from "./runtime.js";
import type { SetupOptions } from "./model-setup.js";

/** The user pressed Ctrl-C. Callers turn it into an `interrupted` report rather than an error. */
export class Interrupted extends Error {}

const obj = (v: unknown): Record<string, unknown> => v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {};
const num = (v: unknown): number | null => typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;
/** Engine-reported text that is shown to the user: one bounded line without control sequences. */
export const label = (v: unknown, max = 100): string | null => {
  const text = typeof v === "string" ? plainText(v).replace(/\s+/g, " ").trim().slice(0, max) : "";
  return text || null;
};

/** One decimal, for every rate and time that is derived or reported: more digits would only be noise. */
export const round1 = (n: number): number => Math.round(n * 10) / 10;
const roundOrNull = (n: number | null): number | null => n === null ? null : round1(n);
export const roundSpread = (s: Spread | null): Spread | null => s && { median: round1(s.median), min: round1(s.min), max: round1(s.max) };

export interface RunResult {
  outputTokens: number; ttftMs: number; durationMs: number;
  /** The engine's own tokens_per_sec: output tokens over the whole response, so it includes the wait for the first token. */
  engineTokensPerSecond: number;
  /** Speed after the first token arrived. Null when the response was too short to tell. */
  generationTokensPerSecond: number | null;
  promptTokensPerSecond: number | null;
}
export interface Spread { median: number; min: number; max: number }

/** Tokens after the first, over the time after the first. Null for one token or less, or when the times do not allow a rate. */
export function generationRate(outputTokens: number, ttftMs: number, durationMs: number): number | null {
  const seconds = (durationMs - ttftMs) / 1000;
  if (!(outputTokens > 1) || !(seconds > 0)) return null;
  const rate = (outputTokens - 1) / seconds;
  return Number.isFinite(rate) ? rate : null;
}

/** Median (the mean of the middle two for an even count) with min and max. Null when there is nothing to measure. */
export function spread(values: Array<number | null>): Spread | null {
  const v = values.filter((x): x is number => typeof x === "number" && Number.isFinite(x)).sort((a, b) => a - b);
  if (!v.length) return null;
  const mid = v.length >> 1;
  return { median: v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2, min: v[0], max: v[v.length - 1] };
}

/** Check one POST /models/benchmark answer. The model must be the one being measured: another client may have switched models meanwhile. */
export function parseRun(value: Record<string, unknown>, model: string): RunResult {
  if (value.model_id !== model) throw new RuntimeError(`The loaded model changed to another model while ${model} was being measured. Nothing from this run was kept.`);
  const outputTokens = num(value.output_tokens), ttftMs = num(value.ttft_ms), durationMs = num(value.duration_ms), engine = num(value.tokens_per_sec);
  if (outputTokens === null || ttftMs === null || durationMs === null || engine === null) throw new RuntimeError("The engine returned an invalid benchmark result.");
  const prompt = num(value.prompt_tokens_per_sec);
  return { outputTokens, ttftMs, durationMs, engineTokensPerSecond: engine,
    generationTokensPerSecond: generationRate(outputTokens, ttftMs, durationMs), promptTokensPerSecond: prompt && prompt > 0 ? prompt : null };
}

/** The run as it appears in the report. Summaries are computed from the unrounded run, so rounding never shifts a median. */
export const roundRun = (r: RunResult): RunResult => ({ ...r, ttftMs: round1(r.ttftMs), durationMs: round1(r.durationMs), engineTokensPerSecond: round1(r.engineTokensPerSecond),
  generationTokensPerSecond: roundOrNull(r.generationTokensPerSecond), promptTokensPerSecond: roundOrNull(r.promptTokensPerSecond) });

export interface RunSummary {
  generationTokensPerSecond: Spread | null; firstTokenMs: Spread | null;
  promptTokensPerSecond: Spread | null; engineTokensPerSecond: Spread | null;
}
export function summarizeRuns(runs: RunResult[]): RunSummary {
  return { generationTokensPerSecond: roundSpread(spread(runs.map(r => r.generationTokensPerSecond))),
    // A response with no text has no first token, so it says nothing about first-token time.
    firstTokenMs: roundSpread(spread(runs.filter(r => r.outputTokens > 0).map(r => r.ttftMs))),
    promptTokensPerSecond: roundSpread(spread(runs.map(r => r.promptTokensPerSecond))),
    engineTokensPerSecond: roundSpread(spread(runs.filter(r => r.outputTokens > 0).map(r => r.engineTokensPerSecond))) };
}

// Where a model runs, from GET /health/inference.
export interface Placement {
  kind: "gpu" | "cpu" | "mixed" | "unknown"; backend: string | null; device: string | null;
  layersOnGpu: number | null; totalLayers: number | null; contextTokens: number | null; verified: boolean;
}
export interface LastBenchmark { model: string | null; tokensPerSecond: number | null; firstTokenMs: number | null; promptTokensPerSecond: number | null }
export interface InferenceInfo {
  /** Null when the engine reports nothing loaded the usual way (no model, a split or pipeline model, or an engine without the route). */
  placement: Placement | null; gpuDevices: string[]; lastBenchmark: LastBenchmark | null; reported: boolean;
}
export function parseInference(value: Record<string, unknown> | null): InferenceInfo {
  if (!value) return { placement: null, gpuDevices: [], lastBenchmark: null, reported: false };
  const loaded = obj(value.loaded), summary = obj(loaded.summary);
  const kind = ["gpu", "cpu", "mixed"].includes(String(summary.device_kind)) ? summary.device_kind as Placement["kind"] : "unknown";
  const placement: Placement | null = Object.keys(loaded).length === 0 ? null : {
    kind, backend: backendName(label(summary.backend, 24)), device: label(summary.device_name), layersOnGpu: num(loaded.offloaded_layers), totalLayers: num(loaded.total_layers),
    contextTokens: num(loaded.used_n_ctx), verified: summary.execution_verified === true };
  const gpuDevices = (Array.isArray(value.devices) ? value.devices : []).flatMap(d => {
    const device = obj(d);
    return device.type === "gpu" || device.type === "igpu" ? [label(device.description) ?? label(device.name) ?? "GPU"] : [];
  });
  const b = obj(value.benchmark);
  const last = Object.keys(b).length === 0 ? null : { model: label(b.model_id, 200), tokensPerSecond: roundOrNull(num(b.tokens_per_sec)),
    firstTokenMs: roundOrNull(num(b.ttft_ms)), promptTokensPerSecond: roundOrNull(num(b.prompt_tokens_per_sec)) };
  return { placement, gpuDevices, lastBenchmark: last, reported: true };
}

export interface Machine {
  description: string; os: string | null; arch: string | null; cpu: string | null; gpu: string | null; ramTotalGB: number | null; ramAvailableGB: number | null;
}
// platform.processor() is only an architecture name on macOS and many Linux systems.
const GENERIC_CPU = /^(unknown|arm|arm64|aarch64|x86_64|amd64|i386|i686)?$/i;
export function describeMachine(hardware: SetupOptions["hardware"]): Machine {
  const cpu = label(hardware.cpu_name), gpu = label(hardware.gpu_name), os = label(hardware.os, 24), arch = label(hardware.arch, 24);
  const ram = num(hardware.ram_total_gb);
  const parts = [cpu && !GENERIC_CPU.test(cpu) ? cpu : null, gpu && gpu !== cpu ? gpu : null].filter((p): p is string => p !== null);
  if (!parts.length) parts.push([os, arch].filter(Boolean).join(" ") || "Unknown machine");
  // The engine's memory figures are GiB, the unit `potluck setup` prints.
  const available = num(hardware.ram_available_gb);
  if (ram !== null) parts.push(`${Math.round(ram)} GiB memory`);
  if (available !== null) parts.push(`${available.toFixed(1)} GiB available now`);
  return { description: parts.join(", "), os, arch, cpu, gpu, ramTotalGB: ram, ramAvailableGB: available };
}

// ggml names a backend after its device buffers ("MTL0_Mapped", "Vulkan0", "CUDA0"), so match on the start of the name.
const BACKENDS: Array<[RegExp, string]> = [[/^(mtl|metal)/i, "Metal"], [/^vulkan/i, "Vulkan"], [/^cuda/i, "CUDA"], [/^rocm/i, "ROCm"], [/^hip/i, "HIP"],
  [/^sycl/i, "SYCL"], [/^opencl/i, "OpenCL"], [/^cpu/i, "CPU"]];
/** A readable backend name; a name nothing matches is shown as the engine gave it. */
export const backendName = (raw: string | null): string | null => raw === null ? null : BACKENDS.find(([pattern]) => pattern.test(raw))?.[1] ?? raw;

export function describeLoaded(model: string | null, p: Placement | null): string {
  if (!model) return "Loaded: no model";
  if (!p) return `Loaded: ${model} (where it runs is not reported by this engine)`;
  const context = p.contextTokens !== null ? `, ${p.contextTokens}-token context` : "";
  const backend = p.backend;
  if (p.kind === "gpu") return `Loaded: ${model} on the GPU${backend ? ` (${backend})` : ""}, ${p.totalLayers !== null ? `all ${p.totalLayers} layers` : "all layers"}${context}`;
  if (p.kind === "mixed") return `Loaded: ${model}, ${p.layersOnGpu ?? "some"} of ${p.totalLayers ?? "all"} layers on the GPU${backend ? ` (${backend})` : ""}, the rest on the CPU${context}`;
  if (p.kind === "cpu") return `Loaded: ${model} on the CPU${context}`;
  return `Loaded: ${model} (where it runs could not be confirmed)${context}`;
}

/** One line when the model is on the CPU, or only partly on the GPU, although the engine lists a GPU. */
export function gpuNote(p: Placement | null, gpuDevices: string[]): string | null {
  if (!p || !gpuDevices.length) return null;
  if (p.kind === "cpu") return `Note: this model runs on the CPU although the engine lists a GPU (${gpuDevices[0]}). The speeds below are CPU speeds.`;
  if (p.kind === "mixed") return `Note: only ${p.layersOnGpu ?? "some"} of ${p.totalLayers ?? "all"} layers are on the GPU (${gpuDevices[0]}); the rest run on the CPU.`;
  return null;
}

export const fmtRate = (n: number) => n >= 100 ? String(Math.round(n)) : n.toFixed(1);
export const fmtMs = (n: number) => `${Math.round(n)} ms`;
/** " (min to max)" when the runs differ at the printed precision, otherwise nothing. */
export function fmtRange(s: Spread, fmt: (n: number) => string): string {
  const lo = fmt(s.min), hi = fmt(s.max);
  return lo === hi ? "" : ` (${lo} to ${hi})`;
}
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** The headline numbers for one model, as plain lines. */
export function formatSummary(model: string, runs: number, s: RunSummary): string[] {
  const g = s.generationTokensPerSecond, t = s.firstTokenMs, p = s.promptTokensPerSecond, e = s.engineTokensPerSecond;
  const parts = [g ? `${fmtRate(g.median)} tokens/s${fmtRange(g, fmtRate)}` : "speed after the first token not measured (the model wrote too few tokens)"];
  if (t) parts.push(`first token ${fmtMs(t.median)}${fmtRange(t, fmtMs)}`);
  if (p) parts.push(`prompt processing ${fmtRate(p.median)} tokens/s${fmtRange(p, fmtRate)}`);
  const lines = [`${model}, ${plural(runs, "run")}: ${parts.join(", ")}`];
  if (e) lines.push(`Engine's own figure: ${fmtRate(e.median)} tokens/s${fmtRange(e, fmtRate)}, counted over the whole response including the wait for the first token.`);
  return lines;
}

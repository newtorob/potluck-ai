import type { CliArgs } from "./args.js";
import { label } from "./benchmark-stats.js";
import { ControlError } from "./control.js";
import { plainText } from "./logs.js";
import { gib, validID } from "./model-setup.js";
import { runtimeRequest } from "./runtime.js";

interface Recipe {
  id: string; name: string; qualification: "experimental"; notes: string;
  model: { id: string; repository: string; revision: string; filename: string; sha256: string; size_bytes: number };
  engine: { backend: "llama-server"; commit: string; acceleration: "cpu" | "metal" | "cuda"; platforms: { os: string; arch: string }[] };
  runtime: { context_size: number; gpu_layers: number; native_cache_mib: number };
  resources: { kv_bytes_per_token: number; overhead_bytes: number };
}
interface Preflight {
  recipe: Recipe; can_install: boolean; can_load: boolean; installed: boolean; artifact_verified: boolean;
  issues: { code: string; message: string; blocks: ("install" | "load")[] }[]; warnings: string[];
  resources: { estimated_ram_bytes: number; available_ram_bytes: number | null; disk_free_bytes: number | null;
    required_disk_bytes: number; estimated_vram_bytes: number | null; available_vram_bytes: number | null };
}

const invalid = (): never => { throw new ControlError("invalid_response", "Runtime returned invalid model recipe data. Upgrade the CLI and runtime together."); };
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
function text(value: unknown, limit = 500): string {
  if (typeof value !== "string" || value.length > limit) return invalid();
  return label(value, limit) ?? "";
}
function match(value: unknown, pattern: RegExp): string {
  if (typeof value !== "string" || !pattern.test(value)) return invalid();
  return value;
}
function boundedMatch(value: unknown, pattern: RegExp, max: number): string {
  return typeof value === "string" && value.length <= max ? match(value, pattern) : invalid();
}
function id(value: unknown): string { return validID(value) ? value : invalid(); }
function integer(value: unknown, min = 0, max = Number.MAX_SAFE_INTEGER): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= min && value <= max ? value : invalid();
}
function available(value: unknown): number | null { return value == null ? null : integer(value); }
function boolean(value: unknown): boolean { return typeof value === "boolean" ? value : invalid(); }
function array(value: unknown, max: number): unknown[] { return Array.isArray(value) && value.length <= max ? value : invalid(); }

/** Build a bounded allowlist for both JSON and terminal output; never echo paths, URLs or engine settings. */
function parseRecipe(value: unknown): Recipe {
  const r = object(value), model = object(r.model), engine = object(r.engine), runtime = object(r.runtime), resources = object(r.resources);
  if (r.qualification !== "experimental" || engine.backend !== "llama-server"
      || !["cpu", "metal", "cuda"].includes(String(engine.acceleration))) return invalid();
  const acceleration = engine.acceleration as Recipe["engine"]["acceleration"];
  const gpuLayers = acceleration === "cpu" ? 0 : -1;
  if (runtime.gpu_layers !== gpuLayers) return invalid();
  const platforms = array(engine.platforms, 8).map(value => {
    const p = object(value);
    if (!["Darwin", "Linux"].includes(String(p.os)) || !["arm64", "x86_64", "aarch64"].includes(String(p.arch))) return invalid();
    return { os: p.os as string, arch: p.arch as string };
  });
  const targets = platforms.map(p => `${p.os}/${p.arch}`);
  if (!platforms.length || new Set(targets).size !== targets.length || targets.includes("Darwin/aarch64")
      || (acceleration === "metal" && (targets.length !== 1 || targets[0] !== "Darwin/arm64"))
      || (acceleration === "cuda" && platforms.some(p => p.os !== "Linux"))) return invalid();
  const name = text(r.name, 200);
  const filename = boundedMatch(model.filename, /^[a-zA-Z0-9][a-zA-Z0-9._-]*\.gguf$/, 256);
  if (!name || /-\d{5}-of-\d{5}\.gguf$/.test(filename)) return invalid();
  return {
    id: id(r.id), name, qualification: "experimental", notes: text(r.notes, 4096),
    model: { id: id(model.id), repository: boundedMatch(model.repository, /^[a-zA-Z0-9_-]+\/[a-zA-Z0-9][a-zA-Z0-9._-]*$/, 256),
      revision: match(model.revision, /^[a-fA-F0-9]{40}$/), filename,
      sha256: match(model.sha256, /^[a-fA-F0-9]{64}$/), size_bytes: integer(model.size_bytes, 1) },
    engine: { backend: "llama-server", commit: match(engine.commit, /^[a-fA-F0-9]{40}$/), acceleration, platforms },
    runtime: { context_size: integer(runtime.context_size, 2048, 131072), gpu_layers: gpuLayers, native_cache_mib: integer(runtime.native_cache_mib, 0, 4096) },
    resources: { kv_bytes_per_token: integer(resources.kv_bytes_per_token, 1), overhead_bytes: integer(resources.overhead_bytes, 1) },
  };
}

function parsePreflight(value: unknown, recipeID: string): Preflight {
  const result = object(value), recipe = parseRecipe(result.recipe), resources = object(result.resources);
  if (result.schema_version !== 1 || recipe.id !== recipeID) return invalid();
  const issues = array(result.issues, 64).map(value => {
    const issue = object(value), blocks = array(issue.blocks, 2);
    if (!blocks.length || blocks.some(b => b !== "install" && b !== "load") || new Set(blocks).size !== blocks.length) return invalid();
    return { code: id(issue.code), message: text(issue.message, 1000), blocks: blocks as ("install" | "load")[] };
  });
  const parsed: Preflight = {
    recipe, can_install: boolean(result.can_install), can_load: boolean(result.can_load),
    installed: boolean(result.installed), artifact_verified: boolean(result.artifact_verified), issues,
    warnings: array(result.warnings, 64).map(v => text(v, 1000)),
    resources: { estimated_ram_bytes: integer(resources.estimated_ram_bytes, 1), available_ram_bytes: available(resources.available_ram_bytes),
      disk_free_bytes: available(resources.disk_free_bytes), required_disk_bytes: integer(resources.required_disk_bytes),
      estimated_vram_bytes: available(resources.estimated_vram_bytes), available_vram_bytes: available(resources.available_vram_bytes) },
  };
  const capacity = parsed.resources;
  const vramFits = capacity.estimated_vram_bytes !== null && capacity.available_vram_bytes !== null && capacity.estimated_vram_bytes <= capacity.available_vram_bytes;
  if ((parsed.can_load && (!parsed.installed || !parsed.artifact_verified || issues.some(i => i.blocks.includes("load"))
        || capacity.available_ram_bytes === null || capacity.estimated_ram_bytes > capacity.available_ram_bytes
        || (recipe.engine.acceleration === "cuda" && !vramFits)))
      || (parsed.can_install && (issues.some(i => i.blocks.includes("install")) || capacity.disk_free_bytes === null || capacity.required_disk_bytes > capacity.disk_free_bytes))
      || (parsed.artifact_verified && !parsed.installed)) return invalid();
  return parsed;
}

const caution = "Experimental recipe. Resource estimates are not model quality, speed or hardware qualification.";
function describeRecipe(r: Recipe): string[] {
  return [
    `${r.id}: ${r.name} (experimental)`,
    `  Model: ${r.model.id}, ${gib(r.model.size_bytes)} weights`,
    `  Artifact: ${r.model.repository} @ ${r.model.revision}`, `  File: ${r.model.filename}`, `  SHA-256: ${r.model.sha256}`,
    `  Engine: ${r.engine.backend} @ ${r.engine.commit} (${r.engine.acceleration})`,
    `  Declared platforms: ${r.engine.platforms.map(p => `${p.os}/${p.arch}`).join(", ")}`,
    `  Context: ${r.runtime.context_size} tokens; prompt cache: ${r.runtime.native_cache_mib} MiB`,
    ...(r.notes ? [`  Note: ${r.notes}`] : []),
  ];
}
const memory = (bytes: number | null) => bytes === null ? "unknown" : gib(bytes);
function formatPreflight(p: Preflight): string {
  const lines = [...describeRecipe(p.recipe),
    `Install requirements: ${p.can_install ? "pass" : "blocked"}. Load requirements: ${p.can_load ? "pass" : "blocked"}.`,
    `Installed: ${p.installed ? "yes" : "no"}. Pinned artifact verified: ${p.artifact_verified ? "yes" : "no"}.`,
    `RAM: ${gib(p.resources.estimated_ram_bytes)} estimated / ${memory(p.resources.available_ram_bytes)} available`,
    `Disk: ${gib(p.resources.required_disk_bytes)} required / ${memory(p.resources.disk_free_bytes)} free`,
  ];
  if (p.resources.estimated_vram_bytes !== null || p.recipe.engine.acceleration === "cuda") {
    lines.push(`VRAM: ${memory(p.resources.estimated_vram_bytes)} estimated / ${memory(p.resources.available_vram_bytes)} available`);
  }
  for (const issue of p.issues) lines.push(`Blocks ${issue.blocks.join(" + ")}: ${issue.message} [${issue.code}]`);
  for (const warning of p.warnings) lines.push(`Note: ${warning}`);
  lines.push(caution, "This check does not download, load or change preferences.");
  if (p.can_load) lines.push(`To replace the current local model for this session: potluck models load-recipe ${p.recipe.id}`);
  else if (p.can_install && !p.installed) lines.push(`To download the pinned weights: potluck models install-recipe ${p.recipe.id}`);
  return lines.join("\n");
}

/** Inspection only uses GET; install and load are separate explicit commands. The runtime rechecks before mutating. */
export async function runModelRecipes(args: CliArgs, out: (value: string) => void): Promise<number> {
  const action = args.action!, recipeID = args.target;
  const suffix = action === "install-recipe" ? "install" : action === "load-recipe" ? "load" : "preflight";
  const route = action === "recipes" ? "/models/recipes" : `/models/recipes/${encodeURIComponent(recipeID!)}/${suffix}`;
  let value: unknown;
  try {
    value = await runtimeRequest(route, { timeout: args.timeout, ...(["install-recipe", "load-recipe"].includes(action) ? { method: "POST" as const } : {}) });
  } catch (error) {
    if (error instanceof ControlError && error.status === 404) throw new ControlError("http_error", action === "recipes"
      ? "Model recipes are unavailable. Upgrade the Potluck runtime."
      : "Recipe not found or runtime does not support recipes. Run potluck models recipes; upgrade the runtime if unavailable.", 404);
    if (error instanceof ControlError && error.status === 409) throw new ControlError("http_error",
      `Recipe request was refused. Run potluck models preflight ${recipeID} to inspect requirements; check potluck status for an active load. Existing weights are not overwritten.`, 409);
    if (error instanceof ControlError && error.code === "timeout" && action === "load-recipe") throw new ControlError("timeout",
      "The recipe load timed out and may still finish. Check potluck status before retrying; no second load was started.");
    if (error instanceof ControlError && error.code === "timeout" && action === "install-recipe") throw new ControlError("timeout",
      `The recipe install request timed out and may still be running. Check potluck models preflight ${recipeID} and model download progress before retrying.`);
    throw error;
  }
  const result = object(value);
  let report: Record<string, unknown>, human: string, code = 0;
  if (action === "recipes") {
    if (result.schema_version !== 1) return invalid();
    const recipes = array(result.recipes, 128).map(parseRecipe);
    if (new Set(recipes.map(r => r.id)).size !== recipes.length) return invalid();
    report = { schemaVersion: 1, action, recipes };
    human = [caution, recipes.length ? recipes.flatMap(r => [...describeRecipe(r), `  Check: potluck models preflight ${r.id}`, ""]).join("\n") : "No recipes available.",
      "Listing recipes does not probe hardware, download or load a model."].join("\n");
  } else if (action === "preflight") {
    const preflight = parsePreflight(result, recipeID!);
    report = { schemaVersion: 1, action, ...preflight };
    human = formatPreflight(preflight);
    code = preflight.can_load ? 0 : 1;
  } else if (action === "install-recipe") {
    if (result.recipe_id !== recipeID || !["downloading", "already_installed"].includes(String(result.status))) return invalid();
    const model = id(result.slug), status = result.status as string;
    const next = status === "downloading" ? `Check: potluck models progress ${model}` : `Check load requirements: potluck models preflight ${recipeID}`;
    report = { schemaVersion: 1, action, recipe_id: recipeID, model, status, next };
    human = `${status === "downloading" ? "Downloading pinned weights for" : "Pinned weights already installed for"} ${recipeID} (${model}).\n${next}\nLoading is a separate command: potluck models load-recipe ${recipeID}\n${caution}`;
  } else {
    if (result.recipe_id !== recipeID || result.status !== "loaded" || result.autoload !== false) return invalid();
    const model = id(result.model_id);
    report = { schemaVersion: 1, action, recipe_id: recipeID, model, status: "loaded", autoload: false, sessionOnly: true };
    human = `Loaded ${model} using ${recipeID} on this machine.\nSession only: startup model restore is cleared. Runtime preferences are unchanged.\n${caution}`;
  }
  out(args.json ? JSON.stringify(report) + "\n" : plainText(human) + "\n");
  return code;
}

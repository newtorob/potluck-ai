/**
 * Config + auth resolution for the Potluck gateway, plus CLI-owned prefs.
 *
 * Two files live under ~/.potluck:
 *   - config.json  — owned by the desktop app. The CLI READS it for the gateway
 *     key/switch, the coordinator URL, and the default model. It may write back
 *     only safe SHARED fields (default model, coordinator URL) and NEVER the
 *     gateway api_key or enabled flag.
 *   - cli.json     — owned by the CLI. Holds CLI-only prefs (default model,
 *     default scope, gateway URL override).
 *
 * Layering (lowest -> highest precedence):
 *   defaults  <  config.json (shared)  <  cli.json (CLI prefs)  <  env  <  flags
 *
 * Concretely:
 *   base URL : DEFAULT_BASE_URL < cli.json.gatewayUrl < POTLUCK_GATEWAY_URL
 *   api key  : gateway.api_key (config.json) < POTLUCK_GATEWAY_KEY
 *   enabled  : gateway.enabled (config.json); forced true if POTLUCK_GATEWAY_KEY set
 *   model    : active_model_id (config.json) < cli.json.model < --model flag
 *   scope    : DEFAULT_SCOPE < cli.json.scope < --scope flag
 */

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

import { listModels } from "./gateway.js";

export const DEFAULT_BASE_URL = "http://127.0.0.1:8321/v1";

/** Scopes usable as an agent-session default. */
export type Scope = "local" | "mine";
export const AGENT_SCOPES: readonly Scope[] = ["local", "mine"] as const;
/** Default scope when nothing else is configured: the household ("your machines"). */
export const DEFAULT_SCOPE: Scope = "mine";

export interface GatewayConfig {
  baseURL: string;
  apiKey: string;
  enabled: boolean;
}

interface RawConfig {
  gateway?: {
    enabled?: boolean;
    api_key?: string;
  };
  network?: {
    coordinator_url?: string;
  };
  active_model_id?: string | null;
}

/** Thrown when the gateway is off or has no key — actionable, not a crash. */
export class GatewayDisabledError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GatewayDisabledError";
  }
}

function configPath(): string {
  return join(homedir(), ".potluck", "config.json");
}

function cliPrefsPath(): string {
  return join(homedir(), ".potluck", "cli.json");
}

async function readRawConfig(): Promise<RawConfig> {
  try {
    const text = await readFile(configPath(), "utf8");
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object") return parsed as RawConfig;
  } catch {
    // Missing or unreadable config: fall back to env / disabled. Not fatal.
  }
  return {};
}

/** Load + resolve gateway config from env, cli.json, and ~/.potluck/config.json. */
export async function loadConfig(): Promise<GatewayConfig> {
  const envKey = process.env.POTLUCK_GATEWAY_KEY?.trim() || undefined;
  const envURL = process.env.POTLUCK_GATEWAY_URL?.trim() || undefined;

  const raw = await readRawConfig();
  const gw = raw.gateway ?? {};
  const cli = await loadCliPrefs();

  const apiKey = envKey ?? (gw.api_key ?? "");
  const baseURL = (envURL ?? cli.gatewayUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
  // An explicit env key means the user is opting in regardless of the stored flag.
  const enabled = envKey !== undefined ? true : gw.enabled === true;

  return { baseURL, apiKey, enabled };
}

/** Guard the gateway is usable; otherwise throw an actionable message. */
export function assertGatewayReady(cfg: GatewayConfig): void {
  if (!cfg.enabled || !cfg.apiKey) {
    throw new GatewayDisabledError(
      "The Potluck gateway is off (or has no API key).\n" +
        "Enable the gateway in the Potluck app: Settings -> Connect tools.\n" +
        "Or set POTLUCK_GATEWAY_KEY (and optionally POTLUCK_GATEWAY_URL).",
    );
  }
}

/**
 * Resolve which model to use: an explicit preference wins (a --model flag or the
 * layered cli.json/config.json default); otherwise the first installed model
 * reported by GET /v1/models.
 */
export async function resolveModel(
  cfg: GatewayConfig,
  preferred?: string,
): Promise<string> {
  const pref = preferred?.trim();
  if (pref) return pref;

  const models = await listModels(cfg);
  const first = models[0]?.id;
  if (!first) {
    throw new Error(
      "No models are installed on this Potluck node. Load a model in the app, " +
        "or pass --model <id>.",
    );
  }
  return first;
}

// ---------------------------------------------------------------------------
// CLI-owned prefs (~/.potluck/cli.json)
// ---------------------------------------------------------------------------

export interface CliPrefs {
  /** Default model id. */
  model?: string;
  /** Default agent scope. */
  scope?: Scope;
  /** Override for the gateway base URL. */
  gatewayUrl?: string;
}

function isScope(value: unknown): value is Scope {
  return value === "local" || value === "mine";
}

/** Read CLI prefs, tolerating a missing/garbled file and ignoring bad fields. */
export async function loadCliPrefs(): Promise<CliPrefs> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(await readFile(cliPrefsPath(), "utf8"));
  } catch {
    return {};
  }
  if (!parsed || typeof parsed !== "object") return {};
  const obj = parsed as Record<string, unknown>;
  const prefs: CliPrefs = {};
  if (typeof obj.model === "string" && obj.model.trim()) prefs.model = obj.model.trim();
  if (isScope(obj.scope)) prefs.scope = obj.scope;
  if (typeof obj.gatewayUrl === "string" && obj.gatewayUrl.trim()) {
    prefs.gatewayUrl = obj.gatewayUrl.trim();
  }
  return prefs;
}

/** True if ~/.potluck/cli.json exists (used to auto-run setup on first use). */
export async function cliPrefsExist(): Promise<boolean> {
  try {
    await readFile(cliPrefsPath(), "utf8");
    return true;
  } catch {
    return false;
  }
}

/** Write CLI prefs to ~/.potluck/cli.json (pretty-printed). */
export async function saveCliPrefs(prefs: CliPrefs): Promise<void> {
  const path = cliPrefsPath();
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, `${JSON.stringify(prefs, null, 2)}\n`, "utf8");
}

// ---------------------------------------------------------------------------
// Shared config (~/.potluck/config.json) — read, and write-back of SAFE fields
// ---------------------------------------------------------------------------

export interface SharedPrefs {
  /** The app's default/active model id, if any. */
  activeModelId?: string;
  /** The coordinator HTTP base, if configured. */
  coordinatorUrl?: string;
}

/** Read the safe shared fields the CLI cares about from config.json. */
export async function loadSharedPrefs(): Promise<SharedPrefs> {
  const raw = await readRawConfig();
  const prefs: SharedPrefs = {};
  if (typeof raw.active_model_id === "string" && raw.active_model_id.trim()) {
    prefs.activeModelId = raw.active_model_id.trim();
  }
  const coord = raw.network?.coordinator_url;
  if (typeof coord === "string" && coord.trim()) prefs.coordinatorUrl = coord.trim();
  return prefs;
}

/** Read config.json as an opaque object (for a safe read-modify-write). */
export async function readRawSharedConfig(): Promise<Record<string, unknown>> {
  try {
    const parsed: unknown = JSON.parse(await readFile(configPath(), "utf8"));
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // Missing/garbled — start from an empty object.
  }
  return {};
}

/**
 * Merge ONLY safe shared fields into a raw config object, preserving everything
 * else. Sets active_model_id and network.coordinator_url when provided. Never
 * touches gateway.* (the app owns the api_key and the enabled flag).
 */
export function mergeSharedSafeFields(
  raw: Record<string, unknown>,
  patch: { model?: string; coordinatorUrl?: string },
): Record<string, unknown> {
  const next: Record<string, unknown> = { ...raw };

  if (patch.model !== undefined) {
    next.active_model_id = patch.model;
  }

  if (patch.coordinatorUrl !== undefined) {
    const existingNetwork =
      raw.network && typeof raw.network === "object" && !Array.isArray(raw.network)
        ? (raw.network as Record<string, unknown>)
        : {};
    next.network = { ...existingNetwork, coordinator_url: patch.coordinatorUrl };
  }

  // gateway is intentionally passed through untouched (api_key + enabled).
  return next;
}

/** Persist safe shared fields back to config.json (read-modify-write). */
export async function saveSharedSafeFields(patch: {
  model?: string;
  coordinatorUrl?: string;
}): Promise<void> {
  const raw = await readRawSharedConfig();
  const next = mergeSharedSafeFields(raw, patch);
  const path = configPath();
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, `${JSON.stringify(next, null, 2)}\n`, "utf8");
}

// ---------------------------------------------------------------------------
// Scope resolution
// ---------------------------------------------------------------------------

export interface ScopeResolution {
  scope: Scope;
  /** A user-facing note when the requested scope was adjusted. */
  notice?: string;
}

/**
 * Resolve the effective agent scope. A --scope flag wins, then cli.json, then
 * the default. circle/cooperative are accepted but are single-turn only, so they
 * fall back to "mine" with a notice. Unknown words also fall back with a notice.
 */
export function resolveScope(
  flag: string | undefined,
  cliScope: Scope | undefined,
): ScopeResolution {
  if (flag !== undefined) return normalizeScope(flag, cliScope);
  return { scope: cliScope ?? DEFAULT_SCOPE };
}

/** Normalize a raw scope word for the in-session /scope command and the flag. */
export function normalizeScope(
  raw: string,
  fallback: Scope | undefined,
): ScopeResolution {
  const v = raw.trim().toLowerCase();
  if (v === "local" || v === "mine") return { scope: v };
  if (v === "circle" || v === "trusted" || v === "cooperative" || v === "pool") {
    return {
      scope: "mine",
      notice: `scope "${v}" is single-turn only; not available for agent sessions yet — using "mine".`,
    };
  }
  return {
    scope: fallback ?? DEFAULT_SCOPE,
    notice: `unknown scope "${raw}"; using "${fallback ?? DEFAULT_SCOPE}".`,
  };
}

/**
 * Config + auth resolution for the Potluck gateway.
 *
 * The CLI never mutates ~/.potluck/config.json — it only reads it. The gateway
 * key and on/off switch are owned by the Potluck app (Settings -> Connect tools).
 *
 * Resolution order (highest precedence first):
 *   base URL : POTLUCK_GATEWAY_URL  ->  http://127.0.0.1:8321/v1
 *   api key  : POTLUCK_GATEWAY_KEY  ->  gateway.api_key from config.json
 *   enabled  : true if POTLUCK_GATEWAY_KEY is set, else gateway.enabled
 */

import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

import { listModels } from "./gateway.js";

export const DEFAULT_BASE_URL = "http://127.0.0.1:8321/v1";

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

/** Load + resolve gateway config from env overrides and ~/.potluck/config.json. */
export async function loadConfig(): Promise<GatewayConfig> {
  const envKey = process.env.POTLUCK_GATEWAY_KEY?.trim() || undefined;
  const envURL = process.env.POTLUCK_GATEWAY_URL?.trim() || undefined;

  const raw = await readRawConfig();
  const gw = raw.gateway ?? {};

  const apiKey = envKey ?? (gw.api_key ?? "");
  const baseURL = (envURL ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
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
 * Resolve which model to use: an explicit --model flag wins; otherwise the first
 * installed model reported by GET /v1/models.
 */
export async function resolveModel(
  cfg: GatewayConfig,
  flagModel?: string,
): Promise<string> {
  const flag = flagModel?.trim();
  if (flag) return flag;

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

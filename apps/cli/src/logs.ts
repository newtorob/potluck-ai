import { open, realpath, stat } from "node:fs/promises";
import { constants } from "node:fs";
import { homedir } from "node:os";
import { join, relative, isAbsolute } from "node:path";
import { dataDir } from "./paths.js";
import { loadConfig } from "./config.js";
import { readRuntimeRecord } from "./runtime.js";

/** Terminal output must not execute escape sequences embedded in logs. */
export function plainText(value: string): string {
  return value.replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, "");
}

export function redactLog(value: string, secrets: string[] = []): string {
  let clean = value;
  for (const secret of secrets.filter(Boolean).sort((a, b) => b.length - a.length)) {
    clean = clean.split(secret).join("[REDACTED]");
  }
  if (/-----END [^-]*PRIVATE KEY-----/.test(clean) && !/-----BEGIN [^-]*PRIVATE KEY-----/.test(clean)) {
    clean = clean.replace(/^[\s\S]*?-----END [^-]*PRIVATE KEY-----/, "[REDACTED PRIVATE KEY]");
  }
  return plainText(clean
    .replace(/-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)/g, "[REDACTED PRIVATE KEY]")
    .replace(/\b(Bearer|Basic)\s+[^\s"',;]+/gi, "$1 [REDACTED]")
    .replace(/\bsk-potluck-[a-z0-9_-]+/gi, "[REDACTED]")
    .replace(/(["']?(?:api[_-]?key|private[_-]?key|pairing[_-]?token|access[_-]?token|refresh[_-]?token|session[_-]?token|node[_-]?token|device[_-]?code|token|password|secret)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s&,}]+)/gi, "$1[REDACTED]")
    .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/gi, "$1[REDACTED]@"));
}

export function logCandidates(source: "runtime" | "mesh" | "app"): string[] {
  const local = join(dataDir(), "logs");
  if (source === "runtime") return [join(local, "runtime.log"), join(local, "python.log")];
  if (source === "mesh") return [join(local, "mesh.log"),
    ...(process.platform === "darwin" && !process.env.POTLUCK_DATA_DIR ? ["/Library/Logs/Potluck/mesh.log"] : [])];
  // An isolated data directory must never fall through to real desktop logs.
  if (process.env.POTLUCK_DATA_DIR) return [join(local, "app.log")];
  const appBase = process.platform === "darwin" ? join(homedir(), "Library", "Application Support")
    : process.platform === "win32" ? process.env.APPDATA || join(homedir(), "AppData", "Roaming")
      : process.env.XDG_CONFIG_HOME || join(homedir(), ".config");
  return [join(local, "app.log"), join(appBase, "Potluck AI", "logs", "main.log")];
}

export interface LogReport {
  schemaVersion: 1;
  source: "runtime" | "mesh" | "app";
  path: string | null;
  lines: string[];
  truncated: boolean;
  error?: "not_found" | "permission_denied" | "unsafe_path" | "unreadable";
  action?: string;
}

export async function readLogs(source: LogReport["source"], lines: number): Promise<LogReport> {
  const cfg = await loadConfig();
  const secrets = [cfg.apiKey, process.env.POTLUCK_SIDECAR_TOKEN || ""];
  try { secrets.push((await readRuntimeRecord())?.token || ""); } catch { /* Logs remain available for diagnosing invalid records. */ }
  for (const path of logCandidates(source)) {
    let handle;
    try {
      const canonical = await realpath(path);
      const parent = await realpath(join(path, ".."));
      const rel = relative(parent, canonical);
      if (rel.startsWith("..") || isAbsolute(rel)) {
        return { schemaVersion: 1, source, path, lines: [], truncated: false, error: "unsafe_path" };
      }
      const info = await stat(canonical);
      if (!info.isFile()) continue;
      handle = await open(canonical, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      const opened = await handle.stat();
      if (!opened.isFile()) continue;
      const size = opened.size;
      const bytes = Math.min(size, 256 * 1024);
      const buffer = Buffer.alloc(bytes);
      const { bytesRead } = await handle.read(buffer, 0, bytes, size - bytes);
      let text = buffer.subarray(0, bytesRead).toString("utf8");
      if (bytes < size) text = text.includes("\n") ? text.slice(text.indexOf("\n") + 1) : "";
      // Redact the entire bounded tail before slicing lines, including PEM blocks.
      const all = redactLog(text, secrets).replace(/\n$/, "").split("\n");
      return { schemaVersion: 1, source, path, lines: text ? all.slice(-lines) : [],
        truncated: bytes < size || all.length > lines };
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code === "ENOENT") continue;
      return { schemaVersion: 1, source, path, lines: [], truncated: false,
        error: code === "EACCES" || code === "EPERM" ? "permission_denied" : "unreadable" };
    } finally { await handle?.close(); }
  }
  return { schemaVersion: 1, source, path: null, lines: [], truncated: false, error: "not_found",
    action: source === "mesh" && process.platform === "linux"
      ? "The mesh may log to systemd: journalctl -u potluck-mesh --no-pager -n 100"
      : "No log file found. A foreground runtime writes to its launching terminal; desktop logs are available in the app's log directory." };
}

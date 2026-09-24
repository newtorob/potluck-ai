/** Runtime discovery is private to the OS user, independent of gateway/member keys. */
import { constants, existsSync } from "node:fs";
import { chmod, lstat, mkdir, open } from "node:fs/promises";
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { ControlError, requestJSON, runtimeURL } from "./control.js";
import { dataDir } from "./paths.js";

export class RuntimeError extends Error {}
export interface RuntimeRecord {
  schemaVersion: 1; instanceId: string; pid: number; url: string;
  owner: "cli" | "desktop" | "standalone"; version: string; token: string;
}

function supported(): void {
  if (process.platform === "win32") throw new RuntimeError("Shared runtime management on Windows is not available yet. Use the desktop app.");
}

export async function readRuntimeRecord(): Promise<RuntimeRecord | null> {
  supported();
  const dir = join(dataDir(), "run");
  try {
    const directory = await lstat(dir);
    if (!directory.isDirectory() || directory.uid !== process.getuid?.() || (directory.mode & 0o077)) {
      throw new RuntimeError("Runtime directory must be owned by you with permissions 0700.");
    }
    const file = await open(join(dir, "runtime.json"), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const stat = await file.stat();
      if (!stat.isFile() || stat.uid !== process.getuid?.() || (stat.mode & 0o077) || stat.size > 8192) {
        throw new RuntimeError("Runtime record must be a private regular file owned by you.");
      }
      const record = JSON.parse(await file.readFile("utf8")) as RuntimeRecord;
      if (record.schemaVersion !== 1 || !/^[a-f0-9-]{36}$/.test(record.instanceId)
          || !Number.isInteger(record.pid) || record.pid < 1 || !["cli", "desktop", "standalone"].includes(record.owner)
          || typeof record.token !== "string" || !/^[a-f0-9]{64}$/.test(record.token)
          || typeof record.version !== "string" || typeof record.url !== "string") {
        throw new RuntimeError("Invalid runtime record; restart the runtime with a compatible Potluck version.");
      }
      runtimeURL({ POTLUCK_SIDECAR_URL: record.url });
      return record;
    } finally { await file.close(); }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    if (error instanceof RuntimeError) throw error;
    throw new RuntimeError("Cannot read the private runtime record. Check ownership, permissions, and runtime version.");
  }
}

export async function verifyRuntime(record: RuntimeRecord, timeout: number, signal?: AbortSignal): Promise<void> {
  const response = await requestJSON({ url: record.url + "/control", token: record.token, timeout, signal });
  if (response.schemaVersion !== 1 || response.instanceId !== record.instanceId || response.pid !== record.pid
      || response.owner !== record.owner || response.url !== record.url) {
    throw new RuntimeError("Runtime identity changed. Run potluck up again before retrying.");
  }
}

export async function runtimeRequest<T = Record<string, unknown>>(path: string, options: {
  timeout: number; method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE"; body?: unknown;
  allowArray?: boolean; signal?: AbortSignal; errorHints?: Readonly<Record<string, string>>;
}): Promise<T> {
  const record = await readRuntimeRecord();
  if (!record) throw new RuntimeError("No shared runtime found. Start it with potluck up.");
  await verifyRuntime(record, options.timeout, options.signal);
  return requestJSON<T>({ ...options, url: record.url + path, token: record.token });
}

export interface RuntimeReport {
  schemaVersion: 1; state: "running" | "stopped"; started?: boolean;
  owner?: RuntimeRecord["owner"]; pid?: number; endpoint?: string;
}
const running = (record: RuntimeRecord, started: boolean): RuntimeReport => ({
  schemaVersion: 1, state: "running", started, owner: record.owner, pid: record.pid, endpoint: record.url,
});

export function runtimeCommand(): { binary: string; args: string[]; cwd?: string } {
  if (process.env.POTLUCK_RUNTIME_BIN) {
    if (!isAbsolute(process.env.POTLUCK_RUNTIME_BIN)) throw new RuntimeError("POTLUCK_RUNTIME_BIN must be an absolute executable path.");
    return { binary: process.env.POTLUCK_RUNTIME_BIN, args: [] };
  }
  const bundled = resolve(dirname(fileURLToPath(import.meta.url)), "../../runtime/potluck-local");
  if (existsSync(bundled)) return { binary: bundled, args: [] };
  // Source checkout convenience. Published CLI uses the independently installed runtime.
  const pythonRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../desktop/python");
  const python = join(pythonRoot, ".venv/bin/python");
  if (existsSync(python) && existsSync(join(pythonRoot, "potluck_local/app.py"))) {
    return { binary: python, args: ["-m", "potluck_local.app"], cwd: pythonRoot };
  }
  return { binary: "potluck-local", args: [] };
}

export async function openRuntimeLog() {
  const logs = join(dataDir(), "logs");
  await mkdir(logs, { recursive: true, mode: 0o700 });
  const directory = await lstat(logs);
  if (!directory.isDirectory() || directory.uid !== process.getuid?.()) throw new RuntimeError("Unsafe runtime log directory.");
  await chmod(logs, 0o700);
  const log = await open(join(logs, "runtime.log"), constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND | constants.O_NOFOLLOW | constants.O_NONBLOCK, 0o600);
  const stat = await log.stat();
  if (!stat.isFile() || stat.uid !== process.getuid?.() || (stat.mode & 0o077)) {
    await log.close();
    throw new RuntimeError("Runtime log must be a private regular file owned by you.");
  }
  return log;
}

export async function startRuntime(timeout: number): Promise<RuntimeReport> {
  supported();
  const current = await readRuntimeRecord();
  if (current) {
    try { await verifyRuntime(current, Math.min(timeout, 1000)); return running(current, false); }
    catch (error) {
      // Never replace a credential when a live listener rejects it or stalls.
      if (!(error instanceof ControlError && error.code === "unreachable")) throw error;
    }
  }
  const endpoint = new URL(runtimeURL());
  // The app currently uses port 8321. Custom CLI ports are explicitly configured.
  const port = endpoint.port || "80";
  try {
    await requestJSON({ url: endpoint.origin + "/health", timeout: Math.min(timeout, 1000) });
    const appeared = await readRuntimeRecord();
    if (appeared) { await verifyRuntime(appeared, Math.min(timeout, 1000)); return running(appeared, false); }
    throw new RuntimeError("An unmanaged runtime is already listening. Stop its launcher before using potluck up.");
  } catch (error) {
    if (!(error instanceof ControlError && error.code === "unreachable")) throw error;
  }
  const command = runtimeCommand();
  const log = await openRuntimeLog();
  let child;
  let spawnFailed = false;
  const token = randomBytes(32).toString("hex");
  try {
    child = spawn(command.binary, [...command.args, "--port", port], {
      cwd: command.cwd, detached: true, stdio: ["ignore", log.fd, log.fd],
      env: { ...process.env, POTLUCK_DATA_DIR: dataDir(), POTLUCK_SHARED_RUNTIME: "1",
        POTLUCK_RUNTIME_OWNER: "cli", POTLUCK_SIDECAR_TOKEN: token,
        POTLUCK_BIND_HOST: process.env.POTLUCK_BIND_HOST || (endpoint.hostname === "[::1]" ? "::" : "0.0.0.0") },
    });
    child.on("error", () => { spawnFailed = true; });
    child.unref();
  } finally { await log.close(); }
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (spawnFailed) throw new RuntimeError("Runtime executable unavailable. Install potluck-local or set POTLUCK_RUNTIME_BIN. See potluck logs.");
    const record = await readRuntimeRecord();
    if (record) {
      try {
        await verifyRuntime(record, Math.min(500, Math.max(1, deadline - Date.now())));
        return running(record, record.token === token);
      } catch (error) {
        if (error instanceof RuntimeError) throw error;
      }
    }
    if ((child.exitCode !== null || child.signalCode !== null) && (!record || record.token === token)) {
      throw new RuntimeError("Runtime exited during startup. Run potluck logs for details.");
    }
    await delay(100);
  }
  throw new RuntimeError("Runtime is not ready within the timeout. It may still be starting; run potluck up again or potluck logs.");
}

export async function stopRuntime(timeout: number): Promise<RuntimeReport> {
  supported();
  const record = await readRuntimeRecord();
  if (!record) return { schemaVersion: 1, state: "stopped" };
  try { await verifyRuntime(record, Math.min(timeout, 1000)); }
  catch (error) {
    if (error instanceof ControlError && error.code === "unreachable") return { schemaVersion: 1, state: "stopped" };
    throw error;
  }
  if (record.owner !== "cli") throw new RuntimeError(`Runtime is owned by ${record.owner}. Stop it through that launcher.`);
  await requestJSON({ url: record.url + "/shutdown", token: record.token, method: "POST", timeout });
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const latest = await readRuntimeRecord();
    if (!latest || latest.instanceId !== record.instanceId) return { schemaVersion: 1, state: "stopped" };
    try { await verifyRuntime(latest, Math.min(500, Math.max(1, deadline - Date.now()))); }
    catch (error) {
      if (error instanceof ControlError && error.code === "unreachable") {
        // Closing the listener comes before model/background-task cleanup.
        // A stale record is only enough once its process has actually exited.
        try { process.kill(record.pid, 0); }
        catch (probe) {
          if ((probe as NodeJS.ErrnoException).code === "ESRCH") return { schemaVersion: 1, state: "stopped" };
        }
      }
    }
    await delay(100);
  }
  throw new RuntimeError("Runtime is still shutting down. Check potluck logs; no process was force-killed.");
}

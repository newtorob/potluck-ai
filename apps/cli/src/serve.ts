/** Foreground supervisor. The OS service manager handles crash backoff/restarts. */
import { spawn, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { ControlError, requestJSON, runtimeURL } from "./control.js";
import { dataDir } from "./paths.js";
import { openRuntimeLog, readRuntimeRecord, runtimeCommand, RuntimeError, verifyRuntime } from "./runtime.js";

export async function serveRuntime(): Promise<number> {
  if (process.platform === "win32" || process.getuid?.() === 0) {
    throw new RuntimeError("Run the runtime as your own user on macOS or Linux, without sudo.");
  }
  process.umask(0o077);
  const abort = new AbortController();
  let child: ChildProcess | undefined;
  const stop = () => { abort.abort(); child?.kill("SIGTERM"); };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
  try {
    // A desktop/CLI instance retains ownership. Never stop it or steal its token.
    // On its exit the service can take over, including when started by Homebrew.
    while (!abort.signal.aborted) {
      const record = await readRuntimeRecord();
      if (!record) break;
      try { await verifyRuntime(record, 1000); }
      catch (error) {
        if (!(error instanceof ControlError && error.code === "unreachable")) throw error;
        try { process.kill(record.pid, 0); }
        catch (probe) { if ((probe as NodeJS.ErrnoException).code === "ESRCH") break; throw probe; }
      }
      await delay(1000, undefined, { signal: abort.signal });
    }
    if (abort.signal.aborted) return 0;
    const endpoint = new URL(runtimeURL());
    try {
      await requestJSON({ url: endpoint.origin + "/health", timeout: 1000 });
      throw new RuntimeError("A runtime is already listening. Stop its launcher before starting this service.");
    } catch (error) {
      if (!(error instanceof ControlError && error.code === "unreachable")) throw error;
    }
    if (abort.signal.aborted) return 0;
    const command = runtimeCommand();
    const log = process.env.POTLUCK_HOMEBREW === "1" ? await openRuntimeLog() : undefined;
    try {
      if (abort.signal.aborted) return 0;
      child = spawn(command.binary, [...command.args, "--port", endpoint.port || "80"], {
        cwd: command.cwd, stdio: ["ignore", log?.fd ?? "inherit", log?.fd ?? "inherit"],
        env: { ...process.env, POTLUCK_DATA_DIR: dataDir(), POTLUCK_SHARED_RUNTIME: "1",
          POTLUCK_RUNTIME_OWNER: "standalone", POTLUCK_SIDECAR_TOKEN: randomBytes(32).toString("hex"),
          POTLUCK_BIND_HOST: process.env.POTLUCK_BIND_HOST || (endpoint.hostname === "[::1]" ? "::" : "0.0.0.0") },
      });
      const completed = new Promise<number>((resolve, reject) => {
        child!.once("error", () => reject(new RuntimeError("Runtime executable unavailable. Install the complete CLI/runtime package or set POTLUCK_RUNTIME_BIN.")));
        child!.once("exit", (code) => resolve(abort.signal.aborted ? 0 : (code ?? 1)));
      });
      return await completed;
    } finally { await log?.close(); }
  } catch (error) {
    if (abort.signal.aborted && (error as Error).name === "AbortError") return 0;
    throw error;
  } finally {
    process.off("SIGINT", stop);
    process.off("SIGTERM", stop);
  }
}

import { spawn } from "node:child_process";

/** Run a shell in its own process group so cancellation also reaches children. */
export function commandProcess(
  command: string,
  cwd: string,
  options: { signal?: AbortSignal; onStarted?: () => void; onData: (data: Buffer) => void },
): Promise<number> {
  const { signal } = options;
  signal?.throwIfAborted();
  return new Promise((resolve, reject) => {
    const child = spawn(command, { cwd, shell: true, detached: process.platform !== "win32" });
    let closed = false;
    let cancelled = false;
    let stopped = false;
    let exitCode = -1;
    let failure: Error | undefined;

    const settle = (): void => {
      if (!closed || (cancelled && !stopped)) return;
      signal?.removeEventListener("abort", abort);
      if (cancelled) reject(signal?.reason ?? new DOMException("Command cancelled", "AbortError"));
      else if (failure) reject(failure);
      else resolve(exitCode);
    };
    const killGroup = (kind: NodeJS.Signals): void => {
      if (!child.pid) return;
      try { process.kill(-child.pid, kind); }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ESRCH") child.kill(kind);
      }
    };
    const abort = (): void => {
      if (cancelled || closed) return;
      cancelled = true;
      if (!child.pid) { stopped = true; settle(); return; }
      if (process.platform === "win32") {
        // taskkill is a Windows utility; no user-controlled text enters its args.
        const killer = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
        const timer = setTimeout(() => { killer.kill(); child.kill(); }, 5_000);
        const done = (): void => { clearTimeout(timer); child.kill(); stopped = true; settle(); };
        killer.once("error", done);
        killer.once("close", done);
      } else {
        killGroup("SIGTERM");
        // A shell can exit while a descendant ignores TERM and holds its pipes.
        // Finish escalation even after the immediate child closes.
        setTimeout(() => { killGroup("SIGKILL"); stopped = true; settle(); }, 250);
      }
    };
    child.once("spawn", () => options.onStarted?.());
    child.stdout.on("data", options.onData);
    child.stderr.on("data", options.onData);
    child.once("error", error => { failure = error; });
    child.once("close", code => { closed = true; exitCode = code ?? -1; settle(); });
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
  });
}

import type { CliArgs } from "./args.js";
import { plainText } from "./logs.js";
import { readProjectModelLock, ModelLockError } from "./model-lock.js";
import { runtimeRequest } from "./runtime.js";

const errorHints = {
  split_engine_missing: "Install a split-capable Potluck runtime on this computer.",
  split_engine_mismatch: "Update both computers to the same split-capable Potluck runtime.",
  split_model_loaded: "Unload the local model with potluck models unload <model-id> before enabling a worker.",
  split_model_missing: "Install this project’s pinned model with potluck models sync.",
  split_pin_mismatch: "Installed model does not match potluck.lock. Run potluck models check, then potluck models sync.",
  split_peer_missing: "Worker is absent or ambiguous in the live mesh. Run potluck household list and use its mesh IP.",
  split_peer_unavailable: "On each selected worker, unload any model and run potluck split worker on. Check potluck split status on both computers.",
  split_busy: "Stop the current split with potluck split stop before starting another.",
  split_unavailable: "Split is unavailable. Run potluck split status on the head and selected workers for details.",
};

export async function manageSplit(args: CliArgs): Promise<Record<string, unknown>> {
  const action = args.action || "status";
  if (action === "status") return runtimeRequest("/split/status", { timeout: args.timeout, errorHints });
  if (action === "worker") return runtimeRequest("/split/worker", {
    method: "POST", body: { enabled: args.target === "on" }, timeout: args.timeout, errorHints,
  });
  if (action === "stop") return runtimeRequest("/split/stop", { method: "POST", timeout: args.timeout, errorHints });
  const lock = await readProjectModelLock(process.cwd());
  if (!lock) throw new ModelLockError("Run potluck models pin <installed-model> in this project first.");
  if (args.target !== lock.model.id) throw new ModelLockError("Split model must match potluck.lock. Deliberately pin the intended model first.");
  return runtimeRequest(`/split/${action}`, { method: "POST", timeout: args.timeout, errorHints,
    body: { model: lock.model, peers: args.peers, context: args.context ?? 2048 } });
}

export async function runSplit(args: CliArgs, out: (text: string) => void): Promise<number> {
  const report = await manageSplit(args);
  if (args.json) out(JSON.stringify(report) + "\n");
  else if (args.action === "plan") {
    const workers = report.workers as Array<{ node_name: string; backend: string }>;
    out(plainText(`Split plan: ${args.target}\nHead: this computer\nWorkers: ${workers.map(w => `${w.node_name} (${w.backend})`).join(", ")}\nContext: ${report.context}\n${report.note}\nRun potluck split start with the same arguments to load.\n`));
  } else {
    const engine = report.engine as { available: boolean; backend?: string };
    const session = report.session as { model: { id: string }; workers: Array<{ name: string; sentBytes: number; receivedBytes: number }> } | null;
    out(plainText(`Split: ${report.state}\nEngine: ${engine.available ? engine.backend : "missing; install a split-capable Potluck build"}\nWorker sharing: ${report.workerEnabled ? "on" : "off"}${report.workerBusy ? " (reserved)" : ""}\n${session ? `Model: ${session.model.id}\nWorkers: ${session.workers.map(w => w.name).join(", ")}\n` : ""}${report.error ? `${report.error}\n` : ""}${report.state === "loading" ? "Loading continues in the runtime. Use potluck split status, then potluck run --scope local when ready.\n" : ""}`));
  }
  return report.state === "failed" ? 1 : 0;
}

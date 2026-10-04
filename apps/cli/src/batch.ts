import { constants } from "node:fs";
import { open } from "node:fs/promises";
import { basename, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { type CliArgs, UsageError } from "./args.js";
import { ControlError } from "./control.js";
import { ModelLockError, readProjectModelLock } from "./model-lock.js";
import { runtimeRequest } from "./runtime.js";
import { plainText } from "./logs.js";

const MAX_FILE = 16 * 1024;
const MAX_TOTAL = 256 * 1024;
interface Piece {
  id: string; name: string; state: string; inputSha256: string; error: string | null;
  attempts: Array<{ worker: string; state: string }>;
}
interface BatchReport extends Record<string, unknown> {
  id: string; state: string; reason: string | null; scope: "local" | "mine";
  model: { id: string; sha256: string }; items: Piece[]; total: number; retries: number;
  counts: { completed: number; running: number; pending: number; failed: number };
}

/** Explicit files only. O_NONBLOCK avoids hanging on FIFOs; O_NOFOLLOW rejects leaf symlinks. */
export async function readBatchFiles(paths: string[]): Promise<Array<{ name: string; text: string }>> {
  if (!paths.length || paths.length > 64) throw new UsageError("Select 1–64 text files.");
  const seen = new Set<string>(); let total = 0;
  const files: Array<{ name: string; text: string }> = [];
  for (const path of paths) {
    const name = basename(path);
    if (name.length > 120 || /[\x00-\x1f\x7f\\]/.test(name)) throw new UsageError("Use printable file names up to 120 characters.");
    let file;
    try {
      file = await open(resolve(path), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      const before = await file.stat();
      if (!before.isFile() || before.size > MAX_FILE) throw new UsageError(`${name}: select a regular text file up to 16 KiB.`);
      const identity = `${before.dev}:${before.ino}`;
      if (seen.has(identity)) throw new UsageError(`${name}: the same file was selected more than once.`);
      seen.add(identity);
      const buffer = Buffer.alloc(MAX_FILE + 1);
      let length = 0;
      while (length < buffer.length) {
        const { bytesRead } = await file.read(buffer, length, buffer.length - length, null);
        if (!bytesRead) break;
        length += bytesRead;
      }
      const after = await file.stat();
      if (length > MAX_FILE || before.size !== after.size || before.mtimeMs !== after.mtimeMs || before.ctimeMs !== after.ctimeMs) throw new UsageError(`${name}: file changed while being read. Retry when it is stable.`);
      const text = new TextDecoder("utf-8", { fatal: true }).decode(buffer.subarray(0, length));
      if (!text.trim() || text.includes("\0")) throw new UsageError(`${name}: select nonempty UTF-8 text.`);
      total += length;
      if (total > MAX_TOTAL) throw new UsageError("Combined batch input must not exceed 256 KiB.");
      files.push({ name, text });
    } catch (error) {
      if (error instanceof UsageError) throw error;
      throw new UsageError(`${name}: cannot read as a regular UTF-8 file. Symlinks and binary files are not supported.`);
    } finally { await file?.close(); }
  }
  return files;
}

async function pin(): Promise<{ id: string; sha256: string }> {
  const lock = await readProjectModelLock(process.cwd());
  if (!lock) throw new ModelLockError("Batch jobs require potluck.lock. Run potluck models pin <model-id> in this project first.");
  return { id: lock.model.id, sha256: lock.model.sha256 };
}

async function request<T = BatchReport>(path: string, args: CliArgs, method: "GET" | "POST" | "DELETE" = "GET", body?: unknown): Promise<T> {
  try { return await runtimeRequest(path, { method, timeout: args.timeout, ...(body === undefined ? {} : { body }) }) as T; }
  catch (error) {
    if (error instanceof ControlError && error.status === 409) throw new ControlError("http_error", "Batch action conflicts with its state or saved policy. Check batch status; resume needs the original pin and scope. Cancel active work before deleting.", 409);
    if (error instanceof ControlError && error.status === 404) throw new ControlError("http_error", "Batch job or endpoint not found. Check the ID and that this runtime supports batch jobs.", 404);
    throw error;
  }
}

function human(report: BatchReport): string {
  const c = report.counts;
  let text = `Batch ${report.id}: ${report.state}\n${c.completed}/${report.total} completed · ${c.running} running · ${c.pending} pending · ${c.failed} failed · ${report.retries} retries\nModel: ${report.model.id} · scope: ${report.scope}\n`;
  const reasons: Record<string, string> = {
    no_eligible_workers: "Load the exact pinned model on an eligible computer with batch support, then resume this batch. Older household runtimes need an upgrade.",
    runtime_restarted: "Saved results were preserved. Resume to process unfinished files.",
    scheduler_error: "Scheduling stopped. Check the runtime before resuming.",
    piece_failed: "Some files failed. Read their status; completed results remain available.",
    user_cancelled: "Unfinished work was cancelled. Completed results remain available.",
  };
  if (report.reason) text += (reasons[report.reason] ?? "Check batch status before continuing.") + "\n";
  for (const item of report.items ?? []) {
    const last = item.attempts.at(-1);
    text += `  ${item.id}. ${item.name}: ${item.state}${last ? ` (${last.worker})` : ""}${item.error ? ` [${item.error}]` : ""}\n`;
  }
  return plainText(text);
}

export async function runBatch(args: CliArgs, out: (text: string) => void): Promise<number> {
  const path = `/batches${args.target ? `/${args.target}` : ""}`;
  if (args.action === "submit") {
    const model = await pin();
    const files = await readBatchFiles(args.files!);
    const id = randomUUID();
    let report: BatchReport;
    try { report = await request("/batches", args, "POST", { id, model, scope: args.scope, files, concurrency: args.jobs ?? 3, max_tokens: args.maxTokens ?? 512 }); }
    catch (error) {
      if (error instanceof ControlError && ["unreachable", "timeout", "invalid_response"].includes(error.code)) throw new ControlError(error.code, `Submission outcome is unknown. Check potluck batch status ${id} before submitting again.`);
      throw error;
    }
    out(args.json ? JSON.stringify(report) + "\n" : human(report) + `Inputs and results are stored privately on this computer.\nWatch: potluck batch status ${id} --watch\n`);
    return 0;
  }
  if (args.action === "list") {
    const report = await request<{ schemaVersion: number; jobs: BatchReport[] }>(path, args);
    out(args.json ? JSON.stringify(report) + "\n" : plainText(report.jobs.map(j => `${j.id}\t${j.state}\t${j.counts.completed}/${j.total}\t${j.model.id}\t${j.scope}`).join("\n") || "No batch jobs.") + "\n");
    return 0;
  }
  if (args.action === "delete") {
    const report = await request(path, args, "DELETE");
    out(args.json ? JSON.stringify(report) + "\n" : `Deleted batch ${args.target}.\n`);
    return 0;
  }
  if (args.action === "resume" || args.action === "cancel") {
    let body;
    if (args.action === "resume") {
      const model = await pin(), previous = await request(path, args);
      const scope = args.scope ?? previous.scope;
      if (model.id !== previous.model.id || model.sha256 !== previous.model.sha256 || scope !== previous.scope) throw new ModelLockError("Resume requires the original project model pin and scope. Saved inputs will be reused.");
      body = { model, scope };
    }
    const report = await request(`${path}/${args.action}`, args, "POST", body);
    out(args.json ? JSON.stringify(report) + "\n" : human(report));
    return 0;
  }
  if (args.action === "results") {
    const report = await request(path, args);
    const results = [];
    for (const item of report.items) results.push(await request<Record<string, unknown>>(`${path}/results/${encodeURIComponent(item.id)}`, args));
    const text = args.json ? JSON.stringify({ ...report, results }) + "\n" : human(report) + results.map(r => plainText(`\n${r.name} [${r.state}]\n${r.text ?? "No completed result."}\n`)).join("");
    if (args.output) {
      const file = await open(resolve(args.output), "wx", 0o600);
      try { await file.writeFile(text); await file.sync(); } finally { await file.close(); }
      out(args.json ? JSON.stringify({ schemaVersion: 1, output: resolve(args.output), state: report.state }) + "\n" : `Saved results to ${plainText(args.output)}.\n`);
    } else out(text);
    return report.state === "completed" ? 0 : 1;
  }
  const controller = new AbortController();
  const interrupt = () => controller.abort();
  process.on("SIGINT", interrupt);
  try {
    while (!controller.signal.aborted) {
      const report = await request(path, args);
      out(args.json ? JSON.stringify(report) + "\n" : human(report));
      if (!args.watch || !["queued", "running"].includes(report.state)) return ["failed", "paused", "cancelled"].includes(report.state) ? 1 : 0;
      try { await delay(args.interval ?? 1000, undefined, { signal: controller.signal }); } catch { break; }
    }
    return 130;
  } finally { process.off("SIGINT", interrupt); }
}

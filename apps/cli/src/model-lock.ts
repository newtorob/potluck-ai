/** Portable project intent. A runtime upgrade and global preferences cannot change it. */
import { constants } from "node:fs";
import { link, lstat, open, unlink } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { homedir } from "node:os";
import { randomUUID } from "node:crypto";
import type { CliArgs } from "./args.js";
import { ControlError } from "./control.js";
import { runtimeRequest } from "./runtime.js";

export interface LockedModel {
  id: string;
  format: "gguf";
  sha256: string;
  sizeBytes: number;
  source?: { url: string };
}
export interface ModelLock { schemaVersion: 1; model: LockedModel }
export interface ProjectModelLock extends ModelLock { path: string }
export class ModelLockError extends Error {}
const filename = "potluck.lock";
const sourcePattern = /^https:\/\/huggingface\.co\/[A-Za-z0-9_-]+\/[A-Za-z0-9._-]+\/resolve\/[A-Za-z0-9_-]+\/(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-][A-Za-z0-9._-]*\.gguf$/;
function record(value: unknown): value is Record<string, unknown> { return !!value && typeof value === "object" && !Array.isArray(value); }
function keys(value: Record<string, unknown>, allowed: string[]): boolean { return Object.keys(value).every(k => allowed.includes(k)); }

export function validateLock(value: unknown): ModelLock {
  const bad = () => new ModelLockError("Invalid or unsupported potluck.lock. Restore a valid lock or deliberately unpin and pin again; it cannot be ignored.");
  if (!record(value) || !keys(value, ["schemaVersion", "model"]) || value.schemaVersion !== 1 || !record(value.model)) throw bad();
  const m = value.model;
  if (!keys(m, ["id", "format", "sha256", "sizeBytes", "source"]) || typeof m.id !== "string"
      || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}$/.test(m.id) || m.format !== "gguf"
      || typeof m.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(m.sha256)
      || !Number.isSafeInteger(m.sizeBytes) || Number(m.sizeBytes) <= 0) throw bad();
  if (m.source !== undefined && (!record(m.source) || !keys(m.source, ["url"])
      || typeof m.source.url !== "string" || m.source.url.length > 2048 || !sourcePattern.test(m.source.url))) throw bad();
  return value as unknown as ModelLock;
}

async function exists(path: string): Promise<boolean> {
  try { await lstat(path); return true; }
  catch (err) { if ((err as NodeJS.ErrnoException).code === "ENOENT") return false; throw err; }
}

/** Nearest lock wins, stopping at a Git worktree root or the home directory. */
export async function lockLocation(cwd: string): Promise<{ path: string; exists: boolean }> {
  const start = resolve(cwd);
  let dir = start;
  while (true) {
    const path = join(dir, filename);
    if (await exists(path)) return { path, exists: true };
    if (await exists(join(dir, ".git"))) return { path, exists: false };
    const parent = dirname(dir);
    if (parent === dir || dir === homedir() || parent === homedir()) break;
    dir = parent;
  }
  return { path: join(start, filename), exists: false };
}

export async function readProjectModelLock(cwd: string): Promise<ProjectModelLock | undefined> {
  const location = await lockLocation(cwd);
  if (!location.exists) return undefined;
  try {
    const file = await open(location.path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const stat = await file.stat();
      if (!stat.isFile() || stat.size > 16384) throw new ModelLockError("potluck.lock must be a regular file under 16 KB.");
      const lock = validateLock(JSON.parse(await file.readFile("utf8")));
      return { ...lock, path: location.path };
    } finally { await file.close(); }
  } catch (error) {
    if (error instanceof ModelLockError) throw error;
    throw new ModelLockError(`Cannot read ${location.path}. The project lock must be valid JSON in a regular file; symlinks are not accepted.`);
  }
}

export function pinnedModel(lock: ProjectModelLock | undefined, requested?: string): string | undefined {
  if (lock && requested && requested !== lock.model.id) throw new ModelLockError(`This project pins ${lock.model.id}. To change it, run potluck models unpin, then potluck models pin <model-id>.`);
  return lock?.model.id ?? requested;
}

export async function manageModelLock(args: CliArgs, cwd = process.cwd()): Promise<Record<string, unknown>> {
  if (args.action === "unpin") {
    // Removing a broken lock is explicit and must not require parsing it first.
    const location = await lockLocation(cwd);
    if (location.exists) await unlink(location.path);
    return { schemaVersion: 1, action: "unpin", model: "project", status: "unpinned", path: location.path };
  }
  const lock = await readProjectModelLock(cwd);
  if (args.action === "lock") return { schemaVersion: 1, action: "lock", status: lock ? "pinned" : "unpinned",
    model: lock?.model.id ?? "none", sha256: lock?.model.sha256, path: lock?.path };
  if (args.action === "sync") {
    if (!lock) throw new ModelLockError("No potluck.lock found. Pin an installed model first, or check out the project's lockfile.");
    try {
      const result = await runtimeRequest(`/models/${encodeURIComponent(lock.model.id)}/install-locked`, {
        method: "POST", body: lock.model, timeout: args.timeout,
      });
      if (result.model_lock_version !== 1 || result.sha256 !== lock.model.sha256
          || !["verified", "downloading"].includes(String(result.status))) throw new ModelLockError("Runtime did not acknowledge the model lock. Upgrade the runtime before retrying.");
      return { schemaVersion: 1, action: "sync", model: lock.model.id, status: result.status,
        sha256: lock.model.sha256, path: lock.path,
        next: result.status === "downloading" ? `Run potluck models progress ${lock.model.id}, then sync again to verify.`
          : `Run potluck models load ${lock.model.id} before using the agent locally.` };
    } catch (error) {
      if (error instanceof ControlError && error.status === 409) throw new ModelLockError("Installed model conflicts with the lock, or a download is still running. Wait for downloads; move conflicting weights aside deliberately, then run potluck models sync again.");
      if (error instanceof ControlError && error.status === 400) throw new ModelLockError("This lock cannot be downloaded. Copy its exact GGUF into the local model directory, then run potluck models sync again.");
      if (error instanceof ControlError && error.status === 404) throw new ModelLockError("Runtime does not support project model locks. Upgrade it before syncing.");
      throw error;
    }
  }
  // pin verifies installed bytes through the authenticated local management API.
  if (lock) {
    pinnedModel(lock, args.target);
    return { schemaVersion: 1, action: "pin", status: "already_pinned", model: lock.model.id, sha256: lock.model.sha256, path: lock.path };
  }
  let identity: Record<string, unknown>;
  try { identity = await runtimeRequest(`/models/${encodeURIComponent(args.target!)}/identity`, { timeout: args.timeout }); }
  catch (error) {
    if (error instanceof ControlError && error.status === 404) throw new ModelLockError("Install the model before pinning. If it is already installed, upgrade the runtime for model-lock support.");
    throw error;
  }
  if (identity.model_lock_version !== 1) throw new ModelLockError("Upgrade the runtime for model-lock support.");
  const value = validateLock({ schemaVersion: 1, model: identity.model });
  if (value.model.id !== args.target) throw new ModelLockError("Runtime returned a different model identity; no lock was written.");
  const location = await lockLocation(cwd);
  // Exclusive creation also protects against another process pinning while hashing.
  const temporary = join(dirname(location.path), `.potluck-lock-${randomUUID()}.tmp`);
  const file = await open(temporary, "wx", 0o644);
  try {
    try { await file.writeFile(JSON.stringify(value, null, 2) + "\n"); await file.sync(); }
    finally { await file.close(); }
    await link(temporary, location.path); // atomic publication, fails if a lock appeared
  } finally { await unlink(temporary); }
  return { schemaVersion: 1, action: "pin", status: "pinned", model: value.model.id, sha256: value.model.sha256,
    path: location.path, next: "Commit potluck.lock with your project. Use potluck models sync on each machine." };
}

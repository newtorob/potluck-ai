/** Explicitly opted-in local transcripts. Resuming forks, so prior history survives. */
import { constants } from "node:fs";
import { lstat, mkdir, open, readdir, realpath, rename, unlink } from "node:fs/promises";
import { dirname, join } from "node:path";
import { randomUUID } from "node:crypto";
import type { ChatMessage } from "./gateway.js";
import type { Scope } from "./config.js";
import type { ProjectModelLock } from "./model-lock.js";
import { dataDir } from "./paths.js";
import { RuntimeError } from "./runtime.js";
const LIMIT = 8 * 1024 * 1024;
const validId = (id: string) => /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(id);
export interface SavedSession {
  schemaVersion: 1; id: string; parentId: string | null; createdAt: string; updatedAt: string;
  project: string; cwd: string; model: string; sha256: string; sizeBytes: number; scope: Scope;
  history: ChatMessage[];
}
async function directory(): Promise<string> {
  const path = join(dataDir(), "sessions");
  await mkdir(path, { recursive: true, mode: 0o700 });
  const stat = await lstat(path);
  if (!stat.isDirectory() || stat.uid !== process.getuid?.() || (stat.mode & 0o077)) throw new RuntimeError("Sessions require a private directory owned by you (chmod 700).");
  return path;
}
export async function loadSession(id: string): Promise<SavedSession> {
  if (!validId(id)) throw new RuntimeError("Use a session ID from potluck sessions list.");
  let file;
  try { file = await open(join(await directory(), id + ".json"), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK); }
  catch { throw new RuntimeError("Session is missing or cannot be safely opened."); }
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.uid !== process.getuid?.() || (stat.mode & 0o077) || stat.size > LIMIT) throw new Error("unsafe");
    const value = JSON.parse(await file.readFile("utf8")) as SavedSession;
    if (value.schemaVersion !== 1 || value.id !== id || typeof value.project !== "string" || typeof value.cwd !== "string"
      || typeof value.model !== "string" || !/^[a-f0-9]{64}$/.test(value.sha256) || !Number.isSafeInteger(value.sizeBytes) || value.sizeBytes <= 0
      || !["local", "mine"].includes(value.scope) || !Number.isFinite(Date.parse(value.createdAt)) || !Number.isFinite(Date.parse(value.updatedAt))
      || !Array.isArray(value.history) || !value.history.every(m => m && ["system", "user", "assistant"].includes(m.role) && typeof m.content === "string")) throw new Error("invalid");
    return value;
  } catch { throw new RuntimeError("Session is invalid, too large, or not private. It was not loaded."); }
  finally { await file.close(); }
}
export async function beginSession(cwd: string, lock: ProjectModelLock | undefined, scope: Scope, previous?: SavedSession): Promise<SavedSession> {
  if (!lock) throw new RuntimeError("Saved sessions require a project model pin. Run potluck models pin <installed-model-id> first.");
  const project = await realpath(dirname(lock.path)), location = await realpath(cwd);
  if (previous && (previous.project !== project || previous.cwd !== location)) throw new RuntimeError("This session belongs to another project or working directory. Resume from its original directory.");
  if (previous && (previous.model !== lock.model.id || previous.sha256 !== lock.model.sha256 || previous.sizeBytes !== lock.model.sizeBytes)) throw new RuntimeError("The project model pin changed since this session. Restore the original lock or start a new session.");
  if (previous && previous.scope !== scope) throw new RuntimeError("Resume requires the session's original routing scope. Start a new session to change it.");
  await directory();
  const now = new Date().toISOString();
  return { schemaVersion: 1, id: randomUUID(), parentId: previous?.id ?? null, createdAt: now, updatedAt: now,
    project, cwd: location, model: lock.model.id, sha256: lock.model.sha256, sizeBytes: lock.model.sizeBytes, scope,
    history: previous?.history.filter(m => m.role !== "system").map(m => ({ role: m.role, content: m.content })) ?? [] };
}
export async function saveSession(session: SavedSession, history: ChatMessage[]): Promise<void> {
  if (!validId(session.id)) throw new RuntimeError("Invalid session ID.");
  const value = { ...session, updatedAt: new Date().toISOString(), history };
  const body = JSON.stringify(value);
  if (Buffer.byteLength(body) > LIMIT) throw new RuntimeError("Session exceeds the 8 MiB history limit. The previous save is unchanged; start a new session.");
  const base = await directory(), temp = join(base, `.${randomUUID()}.tmp`);
  const file = await open(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try {
    await file.writeFile(body); await file.sync(); await file.close();
    await rename(temp, join(base, session.id + ".json"));
    session.updatedAt = value.updatedAt;
  } finally { await file.close(); await unlink(temp).catch(() => {}); }
}
export async function manageSessions(action: string, id?: string): Promise<Record<string, unknown>> {
  if (action === "delete") {
    if (!id || !validId(id)) throw new RuntimeError("Use sessions delete <session-id>.");
    await loadSession(id); await unlink(join(await directory(), id + ".json"));
    return { schemaVersion: 1, deleted: id };
  }
  const files = (await readdir(await directory())).filter(file => file.endsWith(".json") && validId(file.slice(0, -5)));
  const sessions = []; let unreadable = 0;
  for (const file of files) {
    try {
      const session = await loadSession(file.slice(0, -5));
      sessions.push({ id: session.id, parentId: session.parentId, project: session.project, cwd: session.cwd,
        model: session.model, scope: session.scope, updatedAt: session.updatedAt, messages: session.history.length });
    } catch { unreadable++; }
  }
  sessions.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  return { schemaVersion: 1, sessions, unreadable };
}

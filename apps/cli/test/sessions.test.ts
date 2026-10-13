import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { chmod, mkdtemp, mkdir, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { createServer } from "node:http";
import { beginSession, loadSession, manageSessions, saveSession } from "../src/sessions.js";
import type { ProjectModelLock } from "../src/model-lock.js";
let directory: string, project: string, lock: ProjectModelLock;
const history = [{ role: "system" as const, content: "old system" }, { role: "user" as const, content: "private prompt" }, { role: "assistant" as const, content: "private response" }];
beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), "potluck-sessions-")); project = join(directory, "project"); await mkdir(project);
  vi.stubEnv("POTLUCK_DATA_DIR", directory);
  lock = { schemaVersion: 1, path: join(project, "potluck.lock"), model: { id: "fixture", format: "gguf", sha256: "a".repeat(64), sizeBytes: 100 } };
});
afterEach(async () => { vi.unstubAllEnvs(); await rm(directory, { recursive: true, force: true }); });
it("saves privately, lists metadata only, and forks on resume while replacing old system context", async () => {
  const session = await beginSession(project, lock, "local"); await saveSession(session, history);
  const file = join(directory, "sessions", session.id + ".json"); const original = await readFile(file, "utf8");
  expect((await stat(file)).mode & 0o777).toBe(0o600);
  expect((await stat(join(directory, "sessions"))).mode & 0o777).toBe(0o700);
  const loaded = await loadSession(session.id), fork = await beginSession(project, lock, "local", loaded);
  expect(fork.id).not.toBe(session.id); expect(fork.parentId).toBe(session.id);
  expect(fork.history).toEqual(history.slice(1));
  await saveSession(fork, [...fork.history, { role: "user", content: "next" }]);
  expect(await readFile(file, "utf8")).toBe(original);
  const listing = JSON.stringify(await manageSessions("list"));
  expect(listing).not.toContain("private prompt"); expect(listing).not.toContain("private response");
  await manageSessions("delete", fork.id); await expect(loadSession(fork.id)).rejects.toThrow();
});
it("rejects changed project, working directory, pin, or routing scope", async () => {
  const saved = await beginSession(project, lock, "local");
  await expect(beginSession(project, undefined, "local")).rejects.toThrow("pin");
  await expect(beginSession(directory, lock, "local", saved)).rejects.toThrow("directory");
  await expect(beginSession(project, { ...lock, model: { ...lock.model, sha256: "b".repeat(64) } }, "local", saved)).rejects.toThrow("pin changed");
  await expect(beginSession(project, lock, "mine", saved)).rejects.toThrow("scope");
});
it("rejects unsafe files and keeps the previous save if a transcript exceeds its limit", async () => {
  const session = await beginSession(project, lock, "local"); await saveSession(session, history);
  await expect(saveSession(session, [{ role: "user", content: "x".repeat(8 * 1024 * 1024) }])).rejects.toThrow("limit");
  expect((await loadSession(session.id)).history).toEqual(history);
  const file = join(directory, "sessions", session.id + ".json");
  await chmod(file, 0o644); await expect(loadSession(session.id)).rejects.toThrow("private");
  await rm(file); await symlink(join(project, "elsewhere"), file); await expect(loadSession(session.id)).rejects.toThrow("safely");
  await expect(loadSession("../../elsewhere")).rejects.toThrow("session ID");
});
it("the installed coding agent saves and resumes history without replaying old actions", async () => {
  const requests: any[] = [];
  const server = createServer(async (req, res) => {
    if (req.url?.endsWith("capabilities")) { res.end('{"model_lock_version":1}'); return; }
    let text = ""; for await (const chunk of req) text += chunk;
    requests.push(JSON.parse(text));
    res.writeHead(200, { "Content-Type": "text/event-stream", "X-Potluck-Model-Lock": "1", "X-Potluck-Model-SHA256": lock.model.sha256 });
    res.end('data: {"choices":[{"delta":{"content":"Session answer."}}]}\n\ndata: [DONE]\n\n');
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  await writeFile(lock.path, JSON.stringify({ schemaVersion: 1, model: lock.model }));
  const env = { ...process.env, POTLUCK_GATEWAY_URL: `http://127.0.0.1:${(server.address() as any).port}/v1`, POTLUCK_GATEWAY_KEY: "key", SNAPSHOT_DIR: join(directory, "snapshots") };
  const run = (args: string[]) => promisify(execFile)(process.execPath, [resolve("dist/index.js"), ...args, "--no-verify"], { cwd: project, env, timeout: 10000 });
  try {
    const first = await run(["--save-session", "--scope", "local", "-p", "Remember the word banana"]);
    const id = /Session saved: ([a-f0-9-]+)/.exec(first.stderr)![1];
    expect((await loadSession(id)).history.some(m => m.content.includes("banana"))).toBe(true);
    await run(["--resume", id, "-p", "What was the word?"]);
    expect(requests).toHaveLength(2);
    expect(requests[1].messages.some((m: any) => m.content.includes("Remember the word banana"))).toBe(true);
    const listing = await manageSessions("list"); expect(listing.sessions).toHaveLength(2);
    await writeFile(lock.path, JSON.stringify({ schemaVersion: 1, model: { ...lock.model, sha256: "b".repeat(64) } }));
    await expect(run(["--resume", id, "-p", "Should fail"])).rejects.toThrow();
    expect(requests).toHaveLength(2);
  } finally { await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); }); }
}, 35000);

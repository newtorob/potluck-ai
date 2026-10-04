import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseArgs } from "../src/args.js";
import { manageModelLock, pinnedModel, readProjectModelLock, validateLock } from "../src/model-lock.js";
import { runtimeRequest } from "../src/runtime.js";
import { streamChat } from "../src/gateway.js";
vi.mock("../src/runtime.js", () => ({ runtimeRequest: vi.fn() }));
const hash = "a".repeat(64);
const lock = { schemaVersion: 1 as const, model: { id: "test", format: "gguf" as const, sha256: hash, sizeBytes: 100,
  source: { url: "https://huggingface.co/org/model/resolve/main/model-Q4.gguf" } } };
let dir: string;
beforeEach(async () => { dir = await mkdtemp(join(tmpdir(), "potluck-lock-test-")); vi.clearAllMocks(); });
afterEach(async () => { vi.unstubAllGlobals(); await rm(dir, { recursive: true, force: true }); });
const run = (action: string, cwd = dir) => manageModelLock(parseArgs(["models", ...action.split(" ")]), cwd);

describe("project model intent", () => {
  it("pins at the Git root, discovers in subdirectories, persists across catalog/runtime changes", async () => {
    await mkdir(join(dir, ".git"));
    const nested = join(dir, "src", "nested"); await mkdir(nested, { recursive: true });
    vi.mocked(runtimeRequest).mockResolvedValue({ model_lock_version: 1, model: lock.model });
    await run("pin test", nested);
    const path = join(dir, "potluck.lock");
    expect(JSON.parse(await readFile(path, "utf8"))).toEqual(lock);
    expect(await readProjectModelLock(nested)).toEqual({ ...lock, path });
    vi.mocked(runtimeRequest).mockResolvedValue({ model_lock_version: 1, model: { ...lock.model, sha256: "b".repeat(64) } });
    await run("pin test", nested);
    expect(JSON.parse(await readFile(path, "utf8"))).toEqual(lock);
    await expect(run("pin other", nested)).rejects.toThrow("pins test");
    expect(pinnedModel(await readProjectModelLock(dir))).toBe("test");
    await run("unpin", nested);
    expect(await readProjectModelLock(dir)).toBeUndefined();
  });
  it("does not inherit through nested Git roots or worktrees", async () => {
    await writeFile(join(dir, "potluck.lock"), JSON.stringify(lock));
    const nested = join(dir, "other"); await mkdir(nested);
    await writeFile(join(nested, ".git"), "gitdir: elsewhere");
    expect(await readProjectModelLock(nested)).toBeUndefined();
  });
  it("rejects malformed, oversized, symlinked and unsupported locks instead of falling back", async () => {
    const path = join(dir, "potluck.lock");
    for (const value of ["{bad", "x".repeat(17000), JSON.stringify({ ...lock, schemaVersion: 99 })]) {
      await writeFile(path, value);
      await expect(readProjectModelLock(dir)).rejects.toThrow();
    }
    await rm(path);
    await writeFile(join(dir, "external"), JSON.stringify(lock));
    await symlink(join(dir, "external"), path);
    await expect(readProjectModelLock(dir)).rejects.toThrow("symlinks");
  });
  it("sync uses the saved digest and source, never a refreshed catalog", async () => {
    await writeFile(join(dir, "potluck.lock"), JSON.stringify(lock));
    vi.mocked(runtimeRequest).mockResolvedValue({ model_lock_version: 1, status: "verified", sha256: hash });
    expect((await run("sync")).status).toBe("verified");
    expect(runtimeRequest).toHaveBeenCalledWith("/models/test/install-locked", expect.objectContaining({ body: lock.model }));
    vi.mocked(runtimeRequest).mockResolvedValue({ status: "already_installed" });
    await expect(run("sync")).rejects.toThrow("acknowledge");
  });
  it("does not write a lock from an old runtime or mismatched identity", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ model: lock.model });
    await expect(run("pin test")).rejects.toThrow("Upgrade");
    vi.mocked(runtimeRequest).mockResolvedValue({ model_lock_version: 1, model: { ...lock.model, id: "other" } });
    await expect(run("pin test")).rejects.toThrow("different model");
    expect(await readProjectModelLock(dir)).toBeUndefined();
  });
  it.each(["http://127.0.0.1/model.gguf", lock.model.source.url + "?token=secret", "https://huggingface.co/a/b/resolve/main/../../bad.gguf"])("rejects unsafe lock sources: %s", url => {
    expect(() => validateLock({ ...lock, model: { ...lock.model, source: { url } } })).toThrow();
  });
  it("parses lock commands and rejects meaningless arguments", () => {
    for (const action of ["pin test", "sync", "lock", "unpin"]) expect(parseArgs(["models", ...action.split(" ")]).command).toBe("models");
    for (const action of ["pin", "sync other", "unpin other", "lock other"]) expect(() => parseArgs(["models", ...action.split(" ")])).toThrow();
  });
});

describe("locked inference protocol", () => {
  const cfg = { baseURL: "http://127.0.0.1:8321/v1", apiKey: "test", enabled: true, modelLock: { ...lock, path: "/project/potluck.lock" } };
  const collect = async (model = "test", scope = "local") => {
    let result = "";
    for await (const token of streamChat(cfg, [{ role: "user", content: "private project" }], { model, scope })) result += token;
    return result;
  };
  it("checks support before sending project content to old gateways", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("{}", { status: 404 })); vi.stubGlobal("fetch", fetch);
    await expect(collect()).rejects.toThrow("Upgrade");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][1].body).toBeUndefined();
  });
  it("sends exact identity and requires a response acknowledgment before accepting tokens", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(Response.json({ model_lock_version: 1 }))
      .mockResolvedValueOnce(new Response('data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n', {
        headers: { "X-Potluck-Model-Lock": "1", "X-Potluck-Model-SHA256": hash },
      }));
    vi.stubGlobal("fetch", fetch);
    expect(await collect()).toBe("hello");
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toMatchObject({ model: "test", potluck_model_sha256: hash });
  });
  it.each([{}, { "X-Potluck-Model-Lock": "1", "X-Potluck-Model-SHA256": "b".repeat(64) }])("discards unacknowledged or mismatched output", headers => {
    const fetch = vi.fn().mockResolvedValueOnce(Response.json({ model_lock_version: 1 }))
      .mockResolvedValueOnce(new Response('data: {"choices":[{"delta":{"content":"unsafe"}}]}\n\n', { headers }));
    vi.stubGlobal("fetch", fetch);
    return expect(collect()).rejects.toThrow("No output was accepted");
  });
  it("rejects overrides and unsupported scope before making requests", async () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    await expect(collect("other")).rejects.toThrow("pins test");
    await expect(collect("test", "pool")).rejects.toThrow("scope");
    expect(fetch).not.toHaveBeenCalled();
  });
});

it("unpin explicitly removes an invalid lock without touching a symlink target", async () => {
  const file = join(dir, "potluck.lock"), target = join(dir, "external");
  await writeFile(file, "invalid JSON"); await run("unpin");
  expect(await readProjectModelLock(dir)).toBeUndefined();
  await writeFile(target, "untouched"); await symlink(target, file); await run("unpin");
  expect(await readFile(target, "utf8")).toBe("untouched");
});

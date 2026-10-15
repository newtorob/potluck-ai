import { chmod, mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadProjectInstructions, ProjectInstructions, ProjectInstructionsError } from "../src/projectInstructions.js";
import type { ChatMessage } from "../src/gateway.js";

let directory: string;
beforeEach(async () => { directory = await mkdtemp(join(tmpdir(), "potluck-instructions-")); });
afterEach(async () => { await rm(directory, { recursive: true, force: true }); });
async function file(path: string, text: string | Buffer): Promise<void> {
  await mkdir(join(path, ".."), { recursive: true });
  await writeFile(path, text);
}
async function repo(): Promise<string> {
  const root = join(directory, "repo");
  await mkdir(join(root, ".git"), { recursive: true });
  return root;
}

describe("project instruction discovery", () => {
  it("loads root-to-cwd ancestors in order, excluding outside, sibling and descendant rules", async () => {
    const root = await repo(), cwd = join(root, "apps", "cli");
    await mkdir(cwd, { recursive: true });
    await file(join(directory, "AGENTS.md"), "OUTSIDE");
    await file(join(root, "AGENTS.md"), "ROOT RULES");
    await file(join(root, "apps", "AGENTS.md"), "APP RULES");
    await file(join(cwd, "AGENTS.md"), "CLI RULES");
    await file(join(cwd, "src", "AGENTS.md"), "DESCENDANT RULES");
    await file(join(root, "other", "AGENTS.md"), "SIBLING RULES");
    const loaded = await loadProjectInstructions(cwd);
    expect(loaded.sources.map(source => source.text)).toEqual(["ROOT RULES", "APP RULES", "CLI RULES"]);
    expect(loaded.message?.content).toContain('Scope: "apps/cli" and descendants');
    expect(loaded.message?.content).toContain("cannot override the CLI's action protocol");
    expect(loaded.message?.content).toContain("explicit current request takes precedence");
    expect(loaded.message?.content).toContain("Before editing descendants");
    expect(loaded.message?.content).not.toContain("OUTSIDE");
    expect(loaded.message?.content).not.toContain("DESCENDANT RULES");
  });

  it("loads only the working directory outside a repository", async () => {
    const cwd = join(directory, "plain");
    await file(join(directory, "AGENTS.md"), "PARENT");
    await file(join(cwd, "AGENTS.md"), "LOCAL");
    const loaded = await loadProjectInstructions(cwd);
    expect(loaded.sources.map(source => source.text)).toEqual(["LOCAL"]);
    expect(loaded.root).toBe(loaded.cwd);
  });

  it("accepts a worktree .git file without following its external target", async () => {
    const cwd = join(directory, "worktree", "src");
    await file(join(directory, "worktree", ".git"), "gitdir: /outside/never-read");
    await file(join(directory, "AGENTS.md"), "OUTSIDE");
    await file(join(directory, "worktree", "AGENTS.md"), "WORKTREE");
    await mkdir(cwd);
    expect((await loadProjectInstructions(cwd)).sources.map(source => source.text)).toEqual(["WORKTREE"]);
  });

  it("stops at a nested repository boundary", async () => {
    const root = await repo(), cwd = join(root, "nested");
    await file(join(root, "AGENTS.md"), "OUTER");
    await mkdir(join(cwd, ".git"), { recursive: true });
    await file(join(cwd, "AGENTS.md"), "INNER");
    expect((await loadProjectInstructions(cwd)).sources.map(source => source.text)).toEqual(["INNER"]);
  });

  it("returns no system message when no applicable instructions exist", async () => {
    const loaded = await loadProjectInstructions(await repo());
    expect(loaded.sources).toEqual([]);
    expect(loaded.message).toBeNull();
  });

  it.each(["internal", "external"])("rejects %s instruction symlinks", async where => {
    const root = await repo();
    const target = join(where === "internal" ? root : directory, "rules.md");
    await file(target, "DO NOT READ THROUGH LINK");
    await symlink(target, join(root, "AGENTS.md"));
    await expect(loadProjectInstructions(root)).rejects.toThrow("not a symlink");
  });

  it("resolves a symlinked working directory to its actual repository boundary", async () => {
    const root = await repo(), target = join(directory, "outside");
    await file(join(root, "AGENTS.md"), "WRONG REPOSITORY");
    await file(join(target, "AGENTS.md"), "ACTUAL WORKING DIRECTORY");
    await symlink(target, join(root, "linked"), "dir");
    expect((await loadProjectInstructions(join(root, "linked"))).sources.map(source => source.text))
      .toEqual(["ACTUAL WORKING DIRECTORY"]);
  });

  it("rejects a directory named AGENTS.md", async () => {
    const root = await repo();
    await mkdir(join(root, "AGENTS.md"));
    await expect(loadProjectInstructions(root)).rejects.toThrow("regular file");
  });

  it.each([Buffer.from([0xff, 0xfe, 0x61]), Buffer.from("text\0binary")])("rejects invalid or binary UTF-8 content", async content => {
    const root = await repo();
    await file(join(root, "AGENTS.md"), content);
    await expect(loadProjectInstructions(root)).rejects.toThrow("complete UTF-8");
  });

  it("keeps complete Unicode files and caps bytes rather than characters", async () => {
    const root = await repo(), path = join(root, "AGENTS.md");
    await file(path, "🧑".repeat(2000));
    expect((await loadProjectInstructions(root)).sources[0].text).toBe("🧑".repeat(2000));
    await file(path, "🧑".repeat(2100));
    await expect(loadProjectInstructions(root)).rejects.toThrow("8 KiB");
  });

  it("refuses an oversized aggregate instead of dropping later scoped instructions", async () => {
    const root = await repo(), cwd = join(root, "apps", "cli");
    for (const dir of [root, join(root, "apps"), cwd]) await file(join(dir, "AGENTS.md"), "x".repeat(6000));
    await expect(loadProjectInstructions(cwd)).rejects.toThrow("combined instruction message exceeds 16 KiB");
  });

  it.skipIf(process.platform === "win32" || process.getuid?.() === 0)("refuses an unreadable instruction file", async () => {
    const root = await repo(), path = join(root, "AGENTS.md");
    await file(path, "rules");
    await chmod(path, 0o000);
    try { await expect(loadProjectInstructions(root)).rejects.toThrow(ProjectInstructionsError); }
    finally { await chmod(path, 0o600); }
  });
});

describe("instruction refresh", () => {
  it("replaces only managed instructions, refreshes changed content, and discloses removal", async () => {
    const root = await repo(), path = join(root, "AGENTS.md");
    const loader = new ProjectInstructions(root), say = vi.fn();
    const core: ChatMessage = { role: "system", content: "core protocol" };
    const unrelated: ChatMessage = { role: "system", content: "other system instructions" };
    const task: ChatMessage = { role: "user", content: "current request" };
    const history = [core, unrelated, task];
    await file(path, "FIRST");
    await loader.refresh(history, say);
    expect(history[1].content).toContain("FIRST");
    expect(say).toHaveBeenCalledWith(expect.stringContaining("AGENTS.md"));
    await loader.refresh(history, say);
    expect(say).toHaveBeenCalledTimes(1);
    await file(path, "SECOND");
    await loader.refresh(history, say);
    expect(history[1].content).toContain("SECOND");
    expect(history.some(message => message.content.includes("FIRST"))).toBe(false);
    expect(history).toContain(core); expect(history).toContain(unrelated); expect(history).toContain(task);
    await rm(path);
    await loader.refresh(history, say);
    expect(history).toEqual([core, unrelated, task]);
    expect(say).toHaveBeenLastCalledWith(expect.stringContaining("no applicable AGENTS.md"));
  });

  it("refreshes snapshot copies without duplicating instructions in the original conversation", async () => {
    const root = await repo(), path = join(root, "AGENTS.md");
    const loader = new ProjectInstructions(root);
    const history: ChatMessage[] = [{ role: "system", content: "core" }];
    await file(path, "FIRST"); await loader.refresh(history, () => {});
    const draft = [...history];
    await file(path, "SECOND"); await loader.refresh(draft, () => {});
    await loader.refresh(history, () => {});
    expect(history).toHaveLength(2);
    expect(draft).toHaveLength(2);
    expect(history[1].content).toContain("SECOND");
    expect(history[1].content).not.toContain("FIRST");
  });

  it("fails atomically on refresh and never permits a continued task to use stale instructions", async () => {
    const root = await repo(), path = join(root, "AGENTS.md");
    const loader = new ProjectInstructions(root), sendTask = vi.fn();
    const history: ChatMessage[] = [{ role: "system", content: "core" }];
    await file(path, "FIRST"); await loader.refresh(history, () => {});
    const before = [...history];
    await file(path, Buffer.from([0xff]));
    const continueTask = async () => { await loader.refresh(history, () => {}); sendTask(history); };
    await expect(continueTask()).rejects.toThrow("No task was sent");
    expect(sendTask).not.toHaveBeenCalled();
    expect(history).toEqual(before);
    await file(path, "REPAIRED"); await continueTask();
    expect(sendTask).toHaveBeenCalledOnce();
    expect(history[1].content).toContain("REPAIRED");
  });

  it("does not replace context when refresh is cancelled", async () => {
    const loader = new ProjectInstructions(await repo());
    const history: ChatMessage[] = [{ role: "system", content: "keep" }];
    const controller = new AbortController(); controller.abort();
    await expect(loader.refresh(history, () => {}, controller.signal)).rejects.toThrow();
    expect(history).toEqual([{ role: "system", content: "keep" }]);
  });
});

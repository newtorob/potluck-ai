import { mkdtemp, readFile, rm, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { runTask } from "../src/agent.js";
import { runTool } from "../src/tools.js";

const stream = vi.fn();
vi.mock("../src/gateway.js", () => ({ streamChat: (...args: unknown[]) => stream(...args) }));
let cwd: string;
const confirm = vi.fn(async () => true);
const action = (tool: string, args: object) => "```action\n" + JSON.stringify({ tool, args }) + "\n```";
const read = { path: "code.txt" };
const write = { path: "code.txt", content: "agent change\n" };
const edit = { path: "code.txt", old_string: "original", new_string: "agent change" };

beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "potluck-fresh-read-"));
  stream.mockReset(); confirm.mockClear();
});
afterEach(async () => { await rm(cwd, { recursive: true, force: true }); });
const deps = () => ({ cwd, confirm, readPaths: new Set<string>(), readVersions: new Map<string, string>() });

describe("whole-file replacement evidence", () => {
  it.each(["edited!!\n", "original\nnew editor work\n"])("refuses a stale full read before approval (%j)", async (external) => {
    const context = deps();
    const path = join(cwd, "code.txt");
    await writeFile(path, "original\n");
    await runTool("read_file", read, context);
    await writeFile(path, external);
    const result = await runTool("write_file", write, context);
    expect(result).toMatch(/^error:.*changed since.*read/);
    expect(confirm).not.toHaveBeenCalled();
    expect(await readFile(path, "utf8")).toBe(external);
    expect(context.readPaths.size).toBe(0);
    expect(context.readVersions.size).toBe(0);
  });

  it("allows a new whole-file proposal after rereading current content", async () => {
    const context = deps();
    const path = join(cwd, "code.txt");
    await writeFile(path, "original\n");
    await runTool("read_file", read, context);
    await writeFile(path, "editor work\n");
    await runTool("write_file", write, context);
    await runTool("read_file", read, context);
    const result = await runTool("write_file", { ...write, content: "editor work\nagent change\n" }, context);
    expect(result).toMatch(/^wrote/);
    expect(confirm).toHaveBeenCalledOnce();
    expect(await readFile(path, "utf8")).toBe("editor work\nagent change\n");
  });

  it("does not recreate a previously read file deleted between model responses", async () => {
    const context = deps();
    const path = join(cwd, "code.txt");
    await writeFile(path, "original\n");
    await runTool("read_file", read, context);
    await unlink(path);
    expect(await runTool("write_file", write, context)).toMatch(/^error:.*changed since.*read/);
    expect(confirm).not.toHaveBeenCalled();
    await expect(readFile(path)).rejects.toMatchObject({ code: "ENOENT" });
  });

  it("permits targeted edits to the latest content and preserves unrelated changes", async () => {
    const context = deps();
    const path = join(cwd, "code.txt");
    await writeFile(path, "original\n");
    await runTool("read_file", read, context);
    await writeFile(path, "original\neditor work\n");
    expect(await runTool("edit_file", edit, context)).toMatch(/^edited/);
    expect(await readFile(path, "utf8")).toBe("agent change\neditor work\n");
    expect(context.readPaths.size).toBe(0);
    expect(context.readVersions.size).toBe(0);
    expect(await runTool("write_file", { ...write, content: "stale rewrite\n" }, context)).toContain("without reading it first");
    expect(confirm).toHaveBeenCalledOnce();
  });

  it("records authored file contents for a later informed replacement", async () => {
    const context = deps();
    expect(await runTool("write_file", write, context)).toMatch(/^wrote/);
    expect(context.readVersions.size).toBe(1);
    expect(await runTool("write_file", { ...write, content: "revised agent change\n" }, context)).toMatch(/^wrote/);
    expect(await readFile(join(cwd, "code.txt"), "utf8")).toBe("revised agent change\n");
  });

  it("does not confuse unchanged contents with a file timestamp change", async () => {
    const context = deps();
    const path = join(cwd, "code.txt");
    await writeFile(path, "original\n");
    await runTool("read_file", read, context);
    await writeFile(path, "original\n");
    expect(await runTool("write_file", write, context)).toMatch(/^wrote/);
  });

  it("revokes the saved version on a partial read", async () => {
    const context = deps();
    await writeFile(join(cwd, "code.txt"), "first\nsecond\n");
    await runTool("read_file", read, context);
    expect(context.readVersions.size).toBe(1);
    await runTool("read_file", { ...read, limit: 1 }, context);
    expect(context.readVersions.size).toBe(0);
    expect(await runTool("write_file", write, context)).toContain("without reading it first");
    expect(confirm).not.toHaveBeenCalled();
  });

  it("wires freshness through the coding loop between model responses", async () => {
    const path = join(cwd, "code.txt");
    await writeFile(path, "original\n");
    let turn = 0;
    stream.mockImplementation(() => (async function* () {
      if (turn++ === 0) yield action("read_file", read);
      else {
        await writeFile(path, "editor work\n");
        yield action("write_file", write);
      }
    })());
    const onToolResult = vi.fn();
    await runTask([{ role: "system", content: "Use the file tools." }], "Update code.txt", {
      cfg: { baseURL: "http://127.0.0.1:9999", apiKey: "fixture", enabled: true },
      model: "m", cwd, confirm, maxSteps: 2,
      callbacks: { onToken: () => {}, onAction: () => {}, onTurnEnd: () => {}, onToolResult },
    });
    expect(onToolResult).toHaveBeenLastCalledWith(expect.stringMatching(/^error:.*changed since.*read/));
    expect(confirm).not.toHaveBeenCalled();
    expect(await readFile(path, "utf8")).toBe("editor work\n");
  });
});

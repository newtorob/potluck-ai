import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { runTask } from "../src/agent.js";

const action = (tool: string, args: Record<string, unknown>) =>
  "```action\n" + JSON.stringify({ tool, args }) + "\n```";
const edit = (old_string: string, new_string: string) =>
  action("edit_file", { path: "state.txt", old_string, new_string });
const read = action("read_file", { path: "state.txt" });
const diagnostic = action("run_command", { command: "echo inspected" });
let cwd: string;

beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "potluck-command-repeat-"));
  await writeFile(join(cwd, "state.txt"), "original");
});
afterEach(async () => {
  vi.restoreAllMocks();
  await rm(cwd, { recursive: true, force: true });
});

function harness(replies: string[]) {
  let requests = 0;
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
    const content = replies[requests++] ?? "Everything is complete.";
    return new Response(`data: ${JSON.stringify({ choices: [{ delta: { content }, finish_reason: "stop" }] })}\n\ndata: [DONE]\n\n`, {
      headers: { "Content-Type": "text/event-stream" },
    });
  });
  const results: string[] = [];
  return {
    results, requestCount: () => requests,
    run: () => runTask([], "Repair state.txt.", {
      cfg: { baseURL: "http://fixture.invalid/v1", apiKey: "fixture", enabled: true },
      model: "fixture", cwd, contextWindowTokens: 32768, confirm: async () => true,
      callbacks: { onToken: vi.fn(), onTurnEnd: vi.fn(), onAction: vi.fn(),
        onToolResult: result => results.push(result) },
    }),
  };
}

describe("repeated actions separated by successful commands", () => {
  it.each([
    { repeated: edit("absent", "fixed"), reason: "repeated_tool_failure" },
    { repeated: read, reason: "stalled" },
    { repeated: action("write_file", { path: "state.txt", content: "original" }), reason: "repeated_tool_failure" },
  ])("does not let exit-0 diagnostics erase unchanged action history: $reason", async ({ repeated, reason }) => {
    const h = harness([repeated, diagnostic, repeated, diagnostic, repeated,
      action("write_file", { path: "unwanted.txt", content: "should not run" })]);
    expect(await h.run()).toEqual({ status: "incomplete", reason });
    expect(h.requestCount()).toBe(5);
    expect(h.results[2]).toContain("RECOVERY:");
    await expect(readFile(join(cwd, "unwanted.txt"))).rejects.toMatchObject({ code: "ENOENT" });
    expect(await readFile(join(cwd, "state.txt"), "utf8")).toBe("original");
  });

  it("recognizes changed actual feedback after a command repairs the file", async () => {
    await writeFile(join(cwd, "repair.cjs"), 'require("node:fs").writeFileSync("state.txt", "intermediate");');
    const failed = edit("absent", "fixed");
    const h = harness([failed, failed, action("run_command", { command: "node repair.cjs" }),
      failed, failed, edit("intermediate", "fixed"), "Everything is complete."]);
    expect(await h.run()).toEqual({ status: "finished" });
    expect(await readFile(join(cwd, "state.txt"), "utf8")).toBe("fixed");
    expect(h.requestCount()).toBe(7);
  });

  it("resets repeated observations after a real file edit", async () => {
    const h = harness([read, diagnostic, read, edit("original", "fixed"),
      read, diagnostic, read, "Everything is complete."]);
    expect(await h.run()).toEqual({ status: "finished" });
    expect(await readFile(join(cwd, "state.txt"), "utf8")).toBe("fixed");
  });

  it("does not infer that repeated successful commands made no changes from stdout alone", async () => {
    await writeFile(join(cwd, "counter.txt"), "0");
    await writeFile(join(cwd, "increment.cjs"), [
      'const fs = require("node:fs");',
      'fs.writeFileSync("counter.txt", String(Number(fs.readFileSync("counter.txt", "utf8")) + 1));',
    ].join("\n"));
    const command = action("run_command", { command: "node increment.cjs" });
    const h = harness([command, command, command, "Everything is complete."]);
    expect(await h.run()).toEqual({ status: "finished" });
    expect(await readFile(join(cwd, "counter.txt"), "utf8")).toBe("3");
  });
});

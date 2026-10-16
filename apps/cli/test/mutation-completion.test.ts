import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { runTask, type AgentOptions } from "../src/agent.js";
import type { ChatMessage } from "../src/gateway.js";

const action = (tool: string, args: Record<string, unknown>) =>
  "```action\n" + JSON.stringify({ tool, args }) + "\n```";
const edit = (path: string, old_string: string, new_string: string) =>
  action("edit_file", { path, old_string, new_string });
let cwd: string;

beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "potluck-mutation-completion-"));
  await writeFile(join(cwd, "one.txt"), "original");
  await writeFile(join(cwd, "two.txt"), "original");
});
afterEach(async () => {
  vi.restoreAllMocks();
  await rm(cwd, { recursive: true, force: true });
});

function harness(replies: string[], extra: Partial<AgentOptions> = {}) {
  const requests: ChatMessage[][] = [];
  let turn = 0;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
    requests.push(JSON.parse(String(init?.body)).messages);
    const content = replies[turn++] ?? "Everything is complete.";
    return new Response(`data: ${JSON.stringify({ choices: [{ delta: { content }, finish_reason: "stop" }] })}\n\ndata: [DONE]\n\n`, {
      headers: { "Content-Type": "text/event-stream" },
    });
  });
  const results: string[] = [];
  const history: ChatMessage[] = [];
  const opts: AgentOptions = {
    cfg: { baseURL: "http://fixture.invalid/v1", apiKey: "fixture", enabled: true },
    model: "fixture", cwd, contextWindowTokens: 32768,
    confirm: vi.fn(async () => true),
    callbacks: { onToken: vi.fn(), onTurnEnd: vi.fn(), onAction: vi.fn(),
      onNotice: vi.fn(), onToolResult: result => results.push(result) },
    ...extra,
  };
  return { requests, results, history, opts,
    run: () => runTask(history, "Repair the files and preserve unrelated contents.", opts) };
}

describe("completion after file changes", () => {
  it.each([false, true])("keeps a failed edit incomplete with a passing check configured: %s", async withCheck => {
    const check = vi.fn(async () => ({ code: 0, output: "unrelated checks pass" }));
    const h = harness([edit("one.txt", "absent", "fixed"), "Everything is complete."],
      { verifyCommand: withCheck ? "fixed-check" : null, runVerify: check });
    expect(await h.run()).toEqual({ status: "incomplete", reason: "mutation_failed" });
    expect(await readFile(join(cwd, "one.txt"), "utf8")).toBe("original");
    expect(h.results.some(result => result.includes("file change still failed"))).toBe(true);
    expect(check).toHaveBeenCalledTimes(withCheck ? 1 : 0);
    expect(h.requests).toHaveLength(4);
  });

  it("does not let reads, a successful command, or a previous edit hide a later failed file change", async () => {
    const h = harness([
      edit("one.txt", "original", "fixed"), edit("two.txt", "absent", "fixed"),
      action("read_file", { path: "one.txt" }),
      action("run_command", { command: "echo inspected" }), "Everything is complete.",
    ], { verifyCommand: "fixed-check", runVerify: async () => ({ code: 0, output: "passed" }) });
    expect(await h.run()).toEqual({ status: "incomplete", reason: "mutation_failed" });
    expect(await readFile(join(cwd, "one.txt"), "utf8")).toBe("fixed");
    expect(await readFile(join(cwd, "two.txt"), "utf8")).toBe("original");
  });

  it("allows a corrected edit after a premature completion response", async () => {
    const h = harness([edit("one.txt", "absent", "fixed"), "Everything is complete.",
      edit("one.txt", "original", "fixed"), "Everything is complete."]);
    expect(await h.run()).toEqual({ status: "finished" });
    expect(await readFile(join(cwd, "one.txt"), "utf8")).toBe("fixed");
    expect(h.requests[2].at(-1)?.content).toContain("file change still failed");
  });

  it("allows an actual correction in another file after discovering the wrong target", async () => {
    const h = harness([edit("wrong.txt", "original", "fixed"),
      action("read_file", { path: "two.txt" }), edit("two.txt", "original", "fixed"),
      "Everything is complete."]);
    expect(await h.run()).toEqual({ status: "finished" });
    expect(await readFile(join(cwd, "two.txt"), "utf8")).toBe("fixed");
  });

  it("keeps a refused whole-file overwrite incomplete", async () => {
    const h = harness([action("write_file", { path: "one.txt", content: "replacement" }),
      "Everything is complete."]);
    expect(await h.run()).toEqual({ status: "incomplete", reason: "mutation_failed" });
    expect(h.opts.confirm).not.toHaveBeenCalled();
    expect(await readFile(join(cwd, "one.txt"), "utf8")).toBe("original");
  });

  it("retains the failed-change status after the original error leaves model context", async () => {
    await writeFile(join(cwd, "large.txt"), "retained project content\n".repeat(100));
    const h = harness([edit("one.txt", "absent", "fixed"),
      action("read_file", { path: "large.txt" }), "Everything is complete."],
      { charBudget: 1000 });
    expect(await h.run()).toEqual({ status: "incomplete", reason: "mutation_failed" });
    expect(h.requests[2].some(message => message.content.includes("FILE CHANGE STILL FAILED"))).toBe(true);
    expect(h.requests[2].some(message => message.content.includes("Tool output truncated"))).toBe(true);
  });

  it.each([
    action("read_file", { path: "absent.txt" }),
    action("run_command", { command: "exit 7" }),
  ])("allows an unsuccessful exploratory operation without assuming a failed requested change", async operation => {
    const h = harness([operation, "The requested explanation is complete."]);
    expect(await h.run()).toEqual({ status: "finished" });
    expect(h.requests).toHaveLength(2);
  });

  it.each([
    edit("one.txt", "original", "fixed"),
    action("write_file", { path: "new.txt", content: "fixed" }),
    action("run_command", { command: "echo should-not-run" }),
  ])("stops immediately when an approved-action request is declined", async declined => {
    const h = harness([declined, action("run_command", { command: "echo alternative" }),
      "Everything is complete."], { confirm: vi.fn(async () => false) });
    expect(await h.run()).toEqual({ status: "incomplete", reason: "action_declined" });
    expect(h.requests).toHaveLength(1);
    expect(h.opts.confirm).toHaveBeenCalledOnce();
    expect(h.results).toEqual(["user declined"]);
    expect(h.history.at(-1)?.content).toBe("TOOL_RESULT: user declined");
    expect(await readFile(join(cwd, "one.txt"), "utf8")).toBe("original");
  });
});

import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseAction, runTask, type AgentOptions } from "../src/agent.js";
import type { ChatMessage } from "../src/gateway.js";
import { runTool } from "../src/tools.js";

const stream = vi.fn();
vi.mock("../src/gateway.js", () => ({ streamChat: (...args: unknown[]) => stream(...args) }));
const action = (tool: string, args: Record<string, unknown>) => "```action\n" + JSON.stringify({ tool, args }) + "\n```";
let cwd: string;
beforeEach(async () => { cwd = await mkdtemp(join(tmpdir(), "potluck-recovery-")); stream.mockReset(); });
afterEach(async () => { await rm(cwd, { recursive: true, force: true }); });

function script(turns: string[]) {
  let index = 0;
  stream.mockImplementation(() => (async function* () { yield turns[index++] ?? "Done."; })());
}
function options(extra: Partial<AgentOptions> = {}): AgentOptions {
  return { cfg: { baseURL: "unused", apiKey: "test", enabled: true }, model: "pinned", cwd,
    confirm: vi.fn(async () => true), maxSteps: 16,
    callbacks: { onToken: vi.fn(), onTurnEnd: vi.fn(), onAction: vi.fn(), onToolResult: vi.fn(), onNotice: vi.fn() },
    ...extra };
}

describe("tool protocol recovery", () => {
  it("recovers the observed missing outer brace without changing any argument bytes", () => {
    const args = { path: "sample.py", old_string: "def f():\n    return 'a'\n", new_string: "def f():\n    return 'b'\n" };
    const raw = JSON.stringify({ tool: "edit_file", args }).slice(0, -1);
    expect(parseAction("```action\n" + raw + "\n```")).toMatchObject({
      kind: "action", action: { tool: "edit_file", args }, recovery: expect.stringContaining("outer }"),
    });
  });

  it.each([
    '{"tool":"edit_file","args":{"path":"x","old_string":"a","new_string":"b"',
    '{"tool":"write_file","args":{"path":"x","content":"unterminated}',
    '{"tool":"read_file","args":',
    '{"tool":"read_file","args":{"path":"x"},"extra":{}',
  ])("never guesses missing arguments or inner delimiters: %s", raw => {
    expect(parseAction("```action\n" + raw + "\n```").kind).toBe("error");
  });

  it.each([
    { tool: "edit_file", args: { path: "x", old_string: "a", new_string: "b", replace_all: "false" } },
    { tool: "write_file", args: { path: "x", contents: "typo" } },
    { tool: "read_file", args: "x" },
    { tool: "read_file", args: null },
    { tool: "read_file", args: [] },
    { tool: "invented", args: {} },
    { tool: "read_file", args: { path: "x" }, arguments: { path: "y" } },
    { tool: "read_file", args: { path: "x" }, path: "y" },
  ])("rejects invalid tool schemas before execution: %j", raw => {
    const parsed = parseAction("```action\n" + JSON.stringify(raw) + "\n```");
    expect(parsed.kind).toBe("error");
  });

  it("rejects malformed schemas even when tool is the last field in bare or mislabeled JSON", () => {
    const raw = '{"args":{"path":false},"tool":"read_file"}';
    expect(parseAction(raw).kind).toBe("error");
    expect(parseAction("```json\n" + raw + "\n```").kind).toBe("error");
  });

  it.each([
    '```action\n{"tool":"write_file","args":{"path":"x","content":"wrong"}',
    '```json\n{"tool":"write_file","args":\n```',
    '{"tool":"write_file","args":',
  ])("treats truncated tool intent as an error rather than final prose: %s", text => {
    expect(parseAction(text).kind).toBe("error");
  });

  it("preserves literal markdown fences inside JSON strings", () => {
    const args = { path: "example.md", content: "```ts\nconst a = 1;\n```" };
    expect(parseAction(action("write_file", args))).toMatchObject({ kind: "action", action: { tool: "write_file", args } });
  });

  it.each(["action", "json", "tool", ""])("recovers complete tool JSON with a missing %s fence closer", label => {
    const args = { path: "x", old_string: "a", new_string: "b" };
    expect(parseAction(`\`\`\`${label}\n` + JSON.stringify({ tool: "edit_file", args }))).toMatchObject({
      kind: "action", action: { tool: "edit_file", args }, recovery: expect.stringContaining("missing closing fence"),
    });
  });

  it("accepts a closing fence followed by same-line prose, discarding that drift", () => {
    expect(parseAction(action("read_file", { path: "x" }) + "Some trailing explanation.")).toMatchObject({
      kind: "action", action: { tool: "read_file", args: { path: "x" } },
    });
  });

  it("does not mistake an action-fence literal in bare JSON content for an opener", () => {
    const args = { path: "x", content: "```action\nexample\n```" };
    expect(parseAction(JSON.stringify({ tool: "write_file", args }))).toMatchObject({ kind: "action", action: { tool: "write_file", args } });
  });

  it("never rescues a nested tool object out of unfinished arguments", () => {
    const raw = '```action\n{"tool":"write_file","args":{"path":"x","content": {"tool":"run_command","args":{"command":"wrong"}}';
    expect(parseAction(raw).kind).toBe("error");
  });

  it("still waits for stream completion and approval for a recovered edit", async () => {
    const text = action("write_file", { path: "new.txt", content: "new" }).replace('}}\n```', '}\n```');
    let completed = false;
    stream.mockImplementationOnce(() => (async function* () { yield text; completed = true; })());
    stream.mockImplementation(() => (async function* () { yield "Done."; })());
    const confirm = vi.fn(async () => { expect(completed).toBe(true); return false; });
    const opts = options({ confirm });
    await runTask([], "write", opts);
    expect(confirm).toHaveBeenCalledOnce();
    await expect(readFile(join(cwd, "new.txt"))).rejects.toMatchObject({ code: "ENOENT" });
    expect(opts.callbacks.onToolResult).toHaveBeenCalledWith(expect.stringContaining("Recovered one missing outer"));
  });

  it("never executes a repaired action if the transport fails after the fence", async () => {
    stream.mockImplementation(() => (async function* () {
      yield action("write_file", { path: "new.txt", content: "new" }).replace('}}\n```', '}\n```');
      throw new Error("stream incomplete");
    })());
    const opts = options();
    await expect(runTask([], "write", opts)).rejects.toThrow("stream incomplete");
    expect(opts.confirm).not.toHaveBeenCalled();
    await expect(readFile(join(cwd, "new.txt"))).rejects.toMatchObject({ code: "ENOENT" });
  });

  it("gives specific parser feedback and bounds consecutive invalid responses", async () => {
    const broken = '```action\n{"tool":"edit_file","args":{path: nope}}\n```';
    script([broken, broken, broken, action("write_file", { path: "wrong", content: "wrong" })]);
    const opts = options();
    const history: ChatMessage[] = [];
    expect(await runTask(history, "repair", opts)).toEqual({ status: "incomplete", reason: "invalid_action" });
    expect(stream).toHaveBeenCalledTimes(3);
    expect(opts.callbacks.onAction).not.toHaveBeenCalled();
    expect(history.at(-1)?.content).toContain("position");
    expect(history.at(-1)?.content).toContain("Nothing was executed");
  });
});

describe("progress and verification recovery", () => {
  it("turns an unverified final explanation into a repair using the configured check", async () => {
    await writeFile(join(cwd, "x"), "bad");
    script(["Here is how to fix it.", action("edit_file", { path: "x", old_string: "bad", new_string: "fixed" }), "Done."]);
    const runVerify = vi.fn(async () => ({ code: (await readFile(join(cwd, "x"), "utf8")) === "fixed" ? 0 : 1, output: "x must be fixed" }));
    const history: ChatMessage[] = [];
    expect(await runTask(history, "repair", options({ verifyCommand: "fixed-check", runVerify }))).toEqual({ status: "finished" });
    expect(runVerify).toHaveBeenCalledTimes(2);
    expect(history[2].content).toContain("x must be fixed");
  });

  it("does not trust an unedited completion when the configured verification fails", async () => {
    script(["Done.", "Done.", "Done."]);
    const runVerify = vi.fn(async () => ({ code: 1, output: "not fixed" }));
    expect(await runTask([], "repair", options({ verifyCommand: "fixed-check", runVerify }))).toEqual({ status: "incomplete", reason: "verification_failed" });
    expect(runVerify).toHaveBeenCalledOnce();
  });

  it("allows a checked no-change completion when the configured check already passes", async () => {
    script(["Already correct."]);
    const runVerify = vi.fn(async () => ({ code: 0, output: "passed" }));
    expect(await runTask([], "check", options({ verifyCommand: "fixed-check", runVerify }))).toEqual({ status: "finished" });
    expect(runVerify).toHaveBeenCalledOnce();
  });

  it("does not report success if cancellation arrives during the final verification", async () => {
    script(["Already correct."]);
    const controller = new AbortController();
    const runVerify = vi.fn(async () => {
      controller.abort(new Error("cancelled during verify"));
      return { code: 0, output: "passed" };
    });
    const opts = options({ verifyCommand: "fixed-check", runVerify, signal: controller.signal });
    opts.callbacks.onVerify = vi.fn();
    await expect(runTask([], "check", opts)).rejects.toThrow("cancelled during verify");
    expect(opts.callbacks.onVerify).not.toHaveBeenCalled();
  });
  it("breaks an edit/read cycle even with reordered argument keys", async () => {
    await writeFile(join(cwd, "x"), "original");
    script([
      action("edit_file", { path: "x", old_string: "missing", new_string: "fixed" }),
      action("read_file", { path: "x" }),
      action("edit_file", { new_string: "fixed", old_string: "missing", path: "x" }),
      action("read_file", { path: "x" }),
      action("edit_file", { path: "x", old_string: "missing", new_string: "fixed" }),
    ]);
    const history: ChatMessage[] = [];
    expect(await runTask(history, "repair", options())).toEqual({ status: "incomplete", reason: "repeated_tool_failure" });
    expect(stream).toHaveBeenCalledTimes(5);
    expect(history.some(m => m.content.includes("RECOVERY:"))).toBe(true);
  });

  it("stops repeated unchanged reads but allows rereads after a successful edit", async () => {
    await writeFile(join(cwd, "x"), "original");
    const read = action("read_file", { path: "x" });
    script([read, read, action("edit_file", { path: "x", old_string: "original", new_string: "fixed" }), read, read, "Done."]);
    expect(await runTask([], "repair", options())).toEqual({ status: "finished" });
    script([read, read, read, "Done."]);
    expect(await runTask([], "repair", options())).toEqual({ status: "incomplete", reason: "stalled" });
  });

  it("does not count changed read results as a stuck loop", async () => {
    let index = 0;
    stream.mockImplementation(() => (async function* () {
      await writeFile(join(cwd, "x"), `external change ${index}`);
      yield index++ < 4 ? action("read_file", { path: "x" }) : "Done.";
    })());
    expect(await runTask([], "read", options())).toEqual({ status: "finished" });
  });

  it("rejects same-content overwrites without confirmation, verification or progress", async () => {
    await writeFile(join(cwd, "x"), "original");
    const write = action("write_file", { path: "x", content: "original" });
    script([write, action("read_file", { path: "x" }), write, action("read_file", { path: "x" }), write]);
    const opts = options({ verifyCommand: "fixed", runVerify: vi.fn() });
    expect(await runTask([], "repair", opts)).toEqual({ status: "incomplete", reason: "repeated_tool_failure" });
    expect(opts.confirm).not.toHaveBeenCalled();
    expect(opts.runVerify).not.toHaveBeenCalled();
    expect(await readFile(join(cwd, "x"), "utf8")).toBe("original");
  });

  it("rejects edits that become no-ops after whitespace matching", async () => {
    await writeFile(join(cwd, "x"), "hello  \n");
    const confirm = vi.fn(async () => true);
    expect(await runTool("edit_file", { path: "x", old_string: "hello\n", new_string: "hello  \n" }, { cwd, confirm })).toMatch(/^error:.*nothing changed/);
    expect(confirm).not.toHaveBeenCalled();
  });

  it("keeps failing verification through intervening reads and history trimming, then repairs a second file", async () => {
    await writeFile(join(cwd, "a"), "bad"); await writeFile(join(cwd, "b"), "bad");
    script([
      action("edit_file", { path: "a", old_string: "bad", new_string: "fixed" }),
      action("read_file", { path: "b" }),
      "All fixed.",
      action("edit_file", { path: "b", old_string: "bad", new_string: "fixed" }),
      "Done.",
    ]);
    const runVerify = vi.fn(async () => ({ code: (await readFile(join(cwd, "b"), "utf8")) === "fixed" ? 0 : 1, output: "b remains broken" }));
    const history: ChatMessage[] = [{ role: "system", content: "system" }];
    const opts = options({ charBudget: 600, contextWindowTokens: 8192, verifyCommand: "fixed-check", runVerify });
    expect(await runTask(history, "repair both files", opts)).toEqual({ status: "finished" });
    expect(runVerify).toHaveBeenCalledTimes(2);
    for (const index of [2, 3]) {
      const sent = stream.mock.calls[index][1] as ChatMessage[];
      expect(sent.at(-1)?.content).toContain("b remains broken");
      expect(sent.some(m => m.content === "repair both files")).toBe(true);
      expect(sent.reduce((sum, message) => sum + message.content.length, 0)).toBeLessThanOrEqual(600);
    }
    expect(stream.mock.calls.some(([, sent]) => (sent as ChatMessage[])
      .some(message => message.content.includes("trimmed to fit")))).toBe(true);
    expect(opts.callbacks.onNotice).toHaveBeenCalledWith(expect.stringContaining("Earlier context or long tool output was trimmed"));
  });

  it("enforces the original four-failure verify bound and executes no fifth edit", async () => {
    script(Array.from({ length: 6 }, (_, i) => action("write_file", { path: "x", content: `attempt ${i}` })));
    const runVerify = vi.fn(async () => ({ code: 1, output: "still broken" }));
    const opts = options({ verifyCommand: "fixed-check", runVerify });
    expect(await runTask([], "repair", opts)).toEqual({ status: "incomplete", reason: "verification_failed" });
    expect(stream).toHaveBeenCalledTimes(4);
    expect(runVerify).toHaveBeenCalledTimes(4);
    expect(await readFile(join(cwd, "x"), "utf8")).toBe("attempt 3");
  });
});

import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runTask, systemPrompt, NATIVE_TOOLS } from "../src/agent.js";
import { prepareContext, estimateContextTokens } from "../src/context.js";
import { parseArgs } from "../src/args.js";
import type { ChatMessage, StreamOptions } from "../src/gateway.js";
import type { NativeToolCall } from "../src/native-tools.js";
const stream = vi.fn();
vi.mock("../src/gateway.js", () => ({ streamChat: (...args: unknown[]) => stream(...args) }));
let cwd: string;
beforeEach(async () => { stream.mockReset(); cwd = await mkdtemp(join(tmpdir(), "potluck-native-")); });
afterEach(async () => { await rm(cwd, { recursive: true, force: true }); });
const call = (name: string, args: unknown, id = "call_1"): NativeToolCall => ({ id, type: "function", function: { name, arguments: JSON.stringify(args) } });

it("stops overlapping canonical rereads without executing a later edit", async () => {
  await writeFile(join(cwd, "a.py"), "one\ntwo\nthree\nfour\n");
  script([[call("read_file", { path: "a.py" })],
    [call("read_file", { path: join(cwd, "a.py"), offset: 1, limit: 3 }, "r2")],
    [call("read_file", { path: "./a.py", offset: 2, limit: 2 }, "r3")],
    [call("edit_file", { path: "a.py", old_string: "one", new_string: "bad" }, "edit")]]);
  const opts = options();
  expect(await runTask([], "inspect a.py", opts)).toEqual({ status: "incomplete", reason: "stalled" });
  expect(stream).toHaveBeenCalledTimes(3); expect(opts.confirm).not.toHaveBeenCalled();
  expect(opts.callbacks.onToolResult.mock.calls[1][0]).toContain("already present in your current context");
  expect(await readFile(join(cwd, "a.py"), "utf8")).toContain("one");
});

it("retains a named function after a newer read of the same file during measured compaction", async () => {
  await writeFile(join(cwd, "a.py"), ("# unrelated " + "x".repeat(500) + "\n").repeat(20) + "def calculate(x):\n    return x + 1\n");
  await writeFile(join(cwd, "b.py"), "other = 1\n");
  script([[call("read_file", { path: join(cwd, "a.py"), offset: 21, limit: 2 })],
    [call("read_file", { path: "./a.py", offset: 1, limit: 10 }, "r2")],
    [call("read_file", { path: "b.py" }, "r3")], "done"]);
  const history: ChatMessage[] = [{ role: "system", content: "rules" }];
  await runTask(history, "Inspect calculate in a.py", options({ maxSteps: 4, charBudget: 1900,
    measureTokens: async (messages: ChatMessage[]) => Math.ceil(JSON.stringify(messages).length / 3) + 350 }));
  const last = stream.mock.calls[3][1] as ChatMessage[];
  expect(last.some(m => m.content.includes("def calculate(x):"))).toBe(true);
  expect(last.some(m => m.content.includes("# unrelated"))).toBe(false);
  expect(last.some(m => m.content.includes("1 model turns remain"))).toBe(true);
  expect(last.some(m => m.content === "Inspect calculate in a.py")).toBe(true);
  expect(history.some(m => m.content.includes("TURN BUDGET"))).toBe(false);
});

it("sends a decreasing runtime budget without changing the original user request", async () => {
  await writeFile(join(cwd, "a.py"), "value = 1\n");
  script([[call("read_file", { path: "a.py" })], "done"]);
  const history: ChatMessage[] = [];
  await runTask(history, "Inspect a.py", options({ maxSteps: 4 }));
  for (const [index, request] of stream.mock.calls.entries()) {
    const messages = request[1] as ChatMessage[];
    expect(messages.some(m => m.role === "user" && m.content.startsWith("POTLUCK RUNTIME CONTROL:") && m.content.includes(`${4 - index} model turns remain`))).toBe(true);
    expect(messages.some(m => m.role === "user" && m.content === "Inspect a.py")).toBe(true);
  }
  expect(history[0]).toEqual({ role: "user", content: "Inspect a.py" });
});
function script(turns: (NativeToolCall[] | string)[]) {
  let index = 0;
  stream.mockImplementation((_cfg: unknown, _messages: ChatMessage[], opts: StreamOptions) => (async function* () {
    const turn = turns[index++] ?? "Done";
    if (typeof turn === "string") yield turn;
    else opts.onToolCalls!(turn);
  })());
}
function options(overrides = {}) {
  return { cfg: { baseURL: "x", apiKey: "k", enabled: true }, model: "m", cwd,
    nativeTools: true, contextWindowTokens: 16384, reasoningBudget: 256, confirm: vi.fn(async () => true),
    callbacks: { onToken: vi.fn(), onTurnEnd: vi.fn(), onAction: vi.fn(), onToolResult: vi.fn() }, ...overrides };
}
it("repairs a semantic mistake through correlated native calls and a fixed regression check", async () => {
  await writeFile(join(cwd, "scale.py"), "return x ** outer\n");
  script([[call("read_file", { path: "scale.py" })], [call("edit_file", { path: "scale.py", old_string: "x ** outer", new_string: "x ** (inner * outer)" }, "call_2")], "Fixed"]);
  const history: ChatMessage[] = [{ role: "system", content: systemPrompt(cwd, "fixed", null, true) }];
  const runVerify = vi.fn(async () => ({ code: 0, output: "Nonidentity and independently varying exponents pass." }));
  const opts = options({ verifyCommand: "fixed", runVerify });
  expect(await runTask(history, "Preserve both exponents", opts)).toEqual({ status: "finished" });
  expect(await readFile(join(cwd, "scale.py"), "utf8")).toBe("return x ** (inner * outer)\n");
  expect(opts.confirm).toHaveBeenCalledTimes(1);
  expect(runVerify).toHaveBeenCalledWith("fixed", cwd, undefined);
  expect(history.filter(m => m.role === "tool").map(m => m.tool_call_id)).toEqual(["call_1", "call_2"]);
  expect(stream.mock.calls[0][2]).toMatchObject({ tools: NATIVE_TOOLS, reasoningBudget: 256, requireSuccessfulFinish: true });
});
it("rejects parallel calls as a batch and sends an error for each ID before retry", async () => {
  script([[call("write_file", { path: "a", content: "unsafe" }), call("list_dir", {}, "call_2")], "done"]);
  const opts = options(), history: ChatMessage[] = [];
  await runTask(history, "work", opts);
  expect(opts.callbacks.onAction).not.toHaveBeenCalled();
  expect(history.filter(m => m.role === "tool").map(m => m.tool_call_id)).toEqual(["call_1", "call_2"]);
  expect(history[2].content).toContain("Parallel calls were not executed");
});
it.each(["{incomplete", '{"path":"a","content":"x","extra":true}', 'null'])("does not execute invalid function arguments %s", async args => {
  script([[{ ...call("write_file", {}), function: { name: "write_file", arguments: args } }], "done"]);
  const opts = options(), history: ChatMessage[] = [];
  await runTask(history, "work", opts);
  expect(opts.callbacks.onAction).not.toHaveBeenCalled();
  expect(history[2]).toMatchObject({ role: "tool", tool_call_id: "call_1" });
  expect(history[2].content).toContain("error:");
});
it("stops on a declined native write, without a fallback action", async () => {
  script([[call("write_file", { path: "a", content: "x" })]]);
  const opts = options({ confirm: vi.fn(async () => false) });
  expect(await runTask([], "work", opts)).toEqual({ status: "incomplete", reason: "action_declined" });
  expect(stream).toHaveBeenCalledTimes(1);
});
it.each(["<tool_call><function=write_file>bad</function></tool_call>", '```action\n{"tool":"write_file","args":{"path":"a","content":"x"}}\n```'])("does not execute text when native calls were requested", async text => {
  script([text, text, text]);
  const opts = options();
  expect(await runTask([], "work", opts)).toEqual({ status: "incomplete", reason: "invalid_action" });
  expect(opts.callbacks.onAction).not.toHaveBeenCalled();
});
it("never applies a call from a failed stream", async () => {
  stream.mockImplementation((_cfg, _history, opts) => (async function* () {
    opts.onToolCalls([call("write_file", { path: "a", content: "x" })]);
    yield ""; throw new Error("incomplete stream");
  })());
  const opts = options();
  await expect(runTask([], "work", opts)).rejects.toThrow("incomplete stream");
  expect(opts.callbacks.onAction).not.toHaveBeenCalled();
});
it("budgets native definitions and call arguments, retaining complete correlated pairs", () => {
  const history: ChatMessage[] = [
    { role: "system", content: "s" }, { role: "user", content: "task" },
    { role: "assistant", content: "", tool_calls: [call("read_file", { path: "a" })] },
    { role: "tool", tool_call_id: "call_1", content: "TOOL_RESULT: " + "x".repeat(8000) },
  ];
  const context = prepareContext(history, { contextWindowTokens: 4096, toolDefinitionTokens: 900 });
  expect(estimateContextTokens(context.messages) + context.maxOutputTokens + 900).toBeLessThanOrEqual(4096);
  expect(context.messages.at(-2)?.tool_calls).toEqual(history[2].tool_calls);
  expect(context.messages.at(-1)?.tool_call_id).toBe("call_1");
  expect(context.messages.at(-1)?.content).toContain("partial view");
  expect(() => prepareContext(history, { contextWindowTokens: 4096, toolDefinitionTokens: 4000 })).toThrow();
});
it("parses explicit agent options and rejects invalid or management combinations", () => {
  expect(parseArgs(["--native-tools", "--reasoning-budget", "0", "-p", "fix"])).toMatchObject({ nativeTools: true, reasoningBudget: 0 });
  for (const value of ["-1", "1.5", "true", "65536"]) expect(() => parseArgs(["--reasoning-budget", value])).toThrow();
  expect(() => parseArgs(["status", "--native-tools"])).toThrow();
  expect(() => parseArgs(["status", "--reasoning-budget", "0"])).toThrow();
});
it("keeps three small file reads available with native tools in an 8K window", () => {
  const history: ChatMessage[] = [
    { role: "system", content: systemPrompt("/workspace", "python3 -m unittest discover -s tests -v", null, true) },
    { role: "system", content: "Workspace files: helpers.py, service.py, tests/test_service.py" },
    { role: "user", content: "Repair the helper and caller, preserve boundaries, and verify all existing behavior. ".repeat(5) },
  ];
  for (const [index, file] of ["helpers.py", "service.py", "tests/test_service.py"].entries()) {
    history.push({ role: "assistant", content: "", tool_calls: [call("read_file", { path: file }, `read_${index}`)] },
      { role: "tool", tool_call_id: `read_${index}`, content: `TOOL_RESULT: FILE ${file}\n` + "def example(value): return value + 1\n".repeat(10) });
  }
  const context = prepareContext(history, { contextWindowTokens: 8192, maxOutputTokens: 2048,
    currentTaskIndex: 2, toolDefinitionTokens: Buffer.byteLength(JSON.stringify(NATIVE_TOOLS)) + 128 });
  for (const message of history.filter(m => m.role === "tool")) expect(context.messages).toContain(message);
});

it('sends the current failure once while preserving full file reads and the original request',async()=>{
  await writeFile(join(cwd,'a.py'),'value = 1\n');
  await writeFile(join(cwd,'b.py'),'other = 2\n');
  script([[call('read_file',{path:'a.py'})],[call('edit_file',{path:'a.py',old_string:'value = 1',new_string:'value = 2'},'edit')],
    [call('read_file',{path:'a.py'},'read_again')],[call('read_file',{path:'b.py'},'read_b')],
    [call('write_file',{path:'a.py',content:'value = 3\n'},'write')],'done']);
  const opts=options({verifyCommand:'fixed',runVerify:async()=>({code:(await readFile(join(cwd,'a.py'),'utf8')).includes('3')?0:1,output:'AssertionError: expected 3 got 2'}),
    measureTokens:async(messages:ChatMessage[])=>Math.ceil(JSON.stringify(messages).length/3)});
  const history:ChatMessage[]=[];
  expect(await runTask(history,'Set a.py value to 3 without changing b.py',opts)).toEqual({status:'finished'});
  const beforeWrite=stream.mock.calls[4][1] as ChatMessage[];
  expect(beforeWrite.map(m=>m.content).join('\n').match(/AssertionError: expected 3 got 2/g)).toHaveLength(1);
  expect(history.map(m=>m.content).join('\n').match(/AssertionError: expected 3 got 2/g)!.length).toBeGreaterThan(1);
  expect(beforeWrite.some(m=>m.content==='Set a.py value to 3 without changing b.py')).toBe(true);
  expect(await readFile(join(cwd,'a.py'),'utf8')).toBe('value = 3\n');
});

it('keeps the full verifier output locally but only its bounded excerpt in model requests', async () => {
  await writeFile(join(cwd,'a.py'),'value = 1\n');
  const output='FAIL first\nAssertionError: wrong value\n'+'progress line\n'.repeat(500)+'LOCAL_TRANSCRIPT_DETAIL\n'+'more progress\n'.repeat(500)+'FAILED';
  script([[call('edit_file',{path:'a.py',old_string:'value = 1',new_string:'value = 2'})],
    [call('read_file',{path:'a.py'},'read')],'done']);
  const history:ChatMessage[]=[];
  await runTask(history,'repair a.py',options({maxSteps:3,verifyCommand:'fixed',runVerify:async()=>({code:1,output}),
    measureTokens:async(messages:ChatMessage[])=>Math.ceil(JSON.stringify(messages).length/3)}));
  expect(history.some(m=>m.content.includes(output))).toBe(true);
  const sent=stream.mock.calls[2][1] as ChatMessage[];
  expect(sent.some(m=>m.content.includes('LOCAL_TRANSCRIPT_DETAIL'))).toBe(false);
  expect(sent.map(m=>m.content).join('\n').match(/AssertionError: wrong value/g)).toHaveLength(1);
});

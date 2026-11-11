import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runTask, NATIVE_TOOLS } from "../src/agent.js";
import type { ChatMessage, StreamOptions } from "../src/gateway.js";
import type { NativeToolCall } from "../src/native-tools.js";
import { parseArgs } from "../src/args.js";
import { runVerify } from "../src/tools.js";

const stream = vi.fn();
vi.mock("../src/gateway.js", () => ({ streamChat: (...args: unknown[]) => stream(...args) }));
let cwd: string;
beforeEach(async () => {
  stream.mockReset(); cwd = await mkdtemp(join(tmpdir(), "potluck-bounded-"));
  for (const name of ["a", "b", "c", "d"]) await writeFile(join(cwd, name), "value = 1\n");
});
afterEach(async () => { await rm(cwd, { recursive: true, force: true }); });
const action = (name: string, args: unknown): NativeToolCall => ({ id: name, type: "function", function: { name, arguments: JSON.stringify(args) } });
const reads = () => ["a", "b", "c", "d"].map(path => action("read_file", { path }));
const edit = (value = 2) => action("edit_file", { path: "a", old_string: "value = 1", new_string: `value = ${value}` });
function script(turns: (NativeToolCall | string)[]) {
  let i = 0;
  stream.mockImplementation((_cfg: unknown, _messages: ChatMessage[], opts: StreamOptions) => (async function* () {
    const turn = turns[i++] ?? "Done";
    if (typeof turn === "string") yield turn;
    else opts.onToolCalls!([{ ...turn, id: `call_${i}` }]);
  })());
}
function options(overrides = {}) {
  return { cfg: { baseURL: "x", apiKey: "k", enabled: true }, model: "m", cwd,
    nativeTools: true, boundedRepair: true, contextWindowTokens: 16384, maxSteps: 12,
    confirm: vi.fn(async () => true), verifyCommand: "fixed", runVerify: vi.fn(async () => ({ code: 0, output: "pass" })),
    callbacks: { onToken: vi.fn(), onTurnEnd: vi.fn(), onAction: vi.fn(), onToolResult: vi.fn(), onNotice: vi.fn() }, ...overrides };
}

it("switches from four inspections to edit tools, checks the real change, and counts exactly the sent prompt/tools", async () => {
  script([...reads(), edit(), "Fixed"]);
  const measured: unknown[][] = [];
  const opts = options({ measureTokens: async (...args: unknown[]) => { measured.push(args); return 2000; } });
  const history: ChatMessage[] = [{ role: "system", content: "unchanged system" }];
  expect(await runTask(history, "Repair a", opts)).toEqual({ status: "finished" });
  expect(await readFile(join(cwd, "a"), "utf8")).toBe("value = 2\n");
  expect(opts.confirm).toHaveBeenCalledTimes(1); expect(opts.runVerify).toHaveBeenCalledTimes(1);
  for (let i = 0; i < stream.mock.calls.length; i++) {
    const [,messages,request] = stream.mock.calls[i];
    expect(messages[0]).toEqual(history[0]);
    expect(messages.at(-1)).toMatchObject({ role: "user", content: expect.stringContaining("POTLUCK RUNTIME CONTROL") });
    expect(measured[i]).toEqual([messages, request.maxTokens, request.tools]);
  }
  expect(stream.mock.calls[4][2].tools.map((x: any) => x.function.name)).toEqual(["edit_file", "write_file"]);
  expect(stream.mock.calls[5][2].tools).toEqual(NATIVE_TOOLS);
  expect(history.some(m => m.content.includes("POTLUCK RUNTIME CONTROL"))).toBe(false);
});

it("does not execute unavailable inspections or a shell bypass, and closes correlated calls", async () => {
  script([...reads(), action("run_command", { command: "touch bypass" }), action("read_file", { path: "missing" }), edit()]);
  const opts = options(), history: ChatMessage[] = [];
  expect(await runTask(history, "Repair a", opts)).toEqual({ status: "incomplete", reason: "investigation_limit" });
  expect(opts.callbacks.onAction).toHaveBeenCalledTimes(4); expect(opts.confirm).not.toHaveBeenCalled();
  expect(history.filter(m => m.role === "tool").slice(-2).map(m => m.tool_call_id)).toEqual(["call_5", "call_6"]);
  expect(history.at(-1)?.content).toContain("NOT executed");
  expect(await readFile(join(cwd, "a"), "utf8")).toBe("value = 1\n");
});

it("an unchanged or failed edit cannot refill the inspection allowance", async () => {
  script([...reads(), edit(1), action("read_file", { path: "missing" }), action("read_file", { path: "another" })]);
  const opts = options();
  expect(await runTask([], "Repair a", opts)).toEqual({ status: "incomplete", reason: "investigation_limit" });
  expect(stream.mock.calls[5][2].tools.map((x: any) => x.function.name)).toEqual(["edit_file", "write_file"]);
  expect(opts.runVerify).not.toHaveBeenCalled();
});

it("allows two inspections after a real change and carries fixed failure through the correction", async () => {
  script([...reads(), edit(), action("read_file", { path: "a" }), action("read_file", { path: "b" }),
    action("edit_file", { path: "a", old_string: "value = 2", new_string: "value = 3" }), "Fixed"]);
  const verify = vi.fn().mockResolvedValueOnce({ code: 1, output: "expected 3" }).mockResolvedValue({ code: 0, output: "pass" });
  const opts = options({ runVerify: verify });
  expect(await runTask([], "Repair a", opts)).toEqual({ status: "finished" });
  expect(verify).toHaveBeenCalledTimes(2);
  expect(stream.mock.calls[7][2].tools.map((x: any) => x.function.name)).toEqual(["edit_file", "write_file"]);
  expect(stream.mock.calls[7][1].some((m: ChatMessage) => m.content.includes("expected 3"))).toBe(true);
});

it("reports a no-change final answer as incomplete even with a passing verifier", async () => {
  script([...reads(), "I need the missing requirement before editing."]);
  const opts = options();
  expect(await runTask([], "Repair a", opts)).toEqual({ status: "incomplete", reason: "no_change" });
  expect(opts.runVerify).not.toHaveBeenCalled(); expect(opts.confirm).not.toHaveBeenCalled();
});

it("does not accept a final claim while the fixed check still fails", async () => {
  script([edit(), "Done"]);
  expect(await runTask([], "Repair a", options({ runVerify: async () => ({ code: 1, output: "still wrong" }) })))
    .toEqual({ status: "incomplete", reason: "verification_failed" });
});

it("preserves the user's refusal at the decision stage", async () => {
  script([...reads(), edit(), edit(3)]);
  const opts = options({ confirm: vi.fn(async () => false) });
  expect(await runTask([], "Repair a", opts)).toEqual({ status: "incomplete", reason: "action_declined" });
  expect(opts.confirm).toHaveBeenCalledTimes(1); expect(stream).toHaveBeenCalledTimes(5);
});

it("requires a fixed check and fails before a request if mandatory control cannot fit", async () => {
  await expect(runTask([], "Repair a", options({ verifyCommand: null }))).rejects.toThrow("requires a fixed");
  expect(await runTask([], "Repair a", options({ charBudget: 100 }))).toEqual({ status: "incomplete", reason: "context_limit" });
  expect(stream).not.toHaveBeenCalled();
});

it("leaves ordinary investigation and completion unrestricted", async () => {
  script([...reads(), action("read_file", { path: "missing" }), "Report"]);
  expect(await runTask([], "Review source", options({ boundedRepair: false }))).toEqual({ status: "finished" });
  expect(stream.mock.calls[4][2].tools).toEqual(NATIVE_TOOLS);
});

it("parses the explicit agent mode and rejects incompatible commands", () => {
  expect(parseArgs(["--bounded-repair", "--verify", "check", "-p", "fix"])).toMatchObject({ boundedRepair: true });
  expect(() => parseArgs(["--bounded-repair", "--no-verify", "-p", "fix"])).toThrow("requires verification");
  expect(() => parseArgs(["status", "--bounded-repair"])).toThrow("Agent options");
});

it("pins the current failure through long intervening reads and corrects independently varying inputs", async () => {
  await writeFile(join(cwd, "calc.js"), "function area(w, h) { return w * w; }\nmodule.exports = area;\n");
  await writeFile(join(cwd, "noise.txt"), "unrelated data\n".repeat(600));
  await writeFile(join(cwd, "check.cjs"), "const area = require('./calc.js'); const assert = require('node:assert/strict'); for (const [w,h] of [[3,7],[5,2],[0,4]]) assert.equal(area(w,h), w*h, 'independent dimensions must be preserved'); console.log('all dimensions pass');");
  script([action("read_file", { path: "calc.js" }),
    action("edit_file", { path: "calc.js", old_string: "w * w", new_string: "w + h" }),
    action("read_file", { path: "noise.txt" }), action("read_file", { path: "calc.js" }),
    action("edit_file", { path: "calc.js", old_string: "w + h", new_string: "w * h" }), "Fixed"]);
  const history: ChatMessage[] = [];
  const check = vi.fn((command: string, dir: string) => runVerify(command, dir));
  expect(await runTask(history, "Fix area for independent width and height", options({
    contextWindowTokens: 2000, maxOutputTokens: 300, verifyCommand: "node check.cjs", runVerify: check,
    measureTokens: async (messages: ChatMessage[]) => Math.ceil(JSON.stringify(messages).length / 3),
  }))).toEqual({ status: "finished" });
  expect(check).toHaveBeenCalledTimes(2);
  for (const call of stream.mock.calls.slice(2, 5)) {
    const control = call[1].at(-1).content;
    expect(control).toContain("CURRENT FIXED-CHECK FAILURE");
    expect(control).toContain("independent dimensions must be preserved");
    expect(control).toContain("10 !== 21");
  }
  expect(stream.mock.calls[5][1].at(-1).content).not.toContain("CURRENT FIXED-CHECK FAILURE");
  expect(history.some(m => m.content.includes("POTLUCK RUNTIME CONTROL"))).toBe(false);
  expect(await readFile(join(cwd, "check.cjs"), "utf8")).toContain("[[3,7],[5,2],[0,4]]");
});

it("stops before another model request when current failure evidence cannot fit", async () => {
  script([edit(), "Pretend fixed"]);
  const check = vi.fn(async () => ({ code: 1, output: "AssertionError: " + "indispensable failure detail ".repeat(100) }));
  expect(await runTask([], "Repair a", options({ runVerify: check, contextWindowTokens: 2000, maxOutputTokens: 300,
    measureTokens: async (messages: ChatMessage[]) => messages.some(m => m.content.includes("CURRENT FIXED-CHECK FAILURE")) ? 5000 : 100,
  }))).toEqual({ status: "incomplete", reason: "context_limit" });
  expect(stream).toHaveBeenCalledTimes(1);
});

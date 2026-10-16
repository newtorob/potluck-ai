import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { runTask, type AgentCallbacks, type AgentOptions } from "../src/agent.js";
import type { ChatMessage } from "../src/gateway.js";
import { runVerify, type VerifyOutcome } from "../src/tools.js";

const check = "node fixed-check.cjs";
const action = (tool: string, args: Record<string, unknown>) =>
  "```action\n" + JSON.stringify({ tool, args }) + "\n```";
const edit = (old_string: string, new_string: string) => action("edit_file", { path: "state.txt", old_string, new_string });
let cwd: string;
let commandId: number;

beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "potluck-command-verification-"));
  commandId = 0;
  await writeFile(join(cwd, "state.txt"), "original");
  await writeFile(join(cwd, "fixed-check.cjs"), [
    'const state = require("node:fs").readFileSync("state.txt", "utf8");',
    "process.stdout.write(state);",
    'process.exit(state === "good" ? 0 : 1);',
  ].join("\n"));
});
afterEach(async () => {
  vi.restoreAllMocks();
  await rm(cwd, { recursive: true, force: true });
});

async function command(content: string, code = 0): Promise<string> {
  const file = `command-${++commandId}.cjs`;
  await writeFile(join(cwd, file), [
    `require("node:fs").writeFileSync("state.txt", ${JSON.stringify(content)});`,
    `process.exit(${code});`,
  ].join("\n"));
  return action("run_command", { command: `node ${file}` });
}

function harness(replies: string[], extra: Partial<AgentOptions> = {}) {
  const requests: Array<{ model: string; messages: ChatMessage[] }> = [];
  let index = 0;
  // Exercise the actual gateway SSE parser; no network or live model is used.
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
    requests.push(JSON.parse(String(init?.body)));
    const content = replies[index++] ?? "The task is complete.";
    return new Response(`data: ${JSON.stringify({ choices: [{ index: 0, delta: { content }, finish_reason: "stop" }] })}\n\ndata: [DONE]\n\n`, {
      headers: { "Content-Type": "text/event-stream" },
    });
  });
  const results: string[] = [];
  const outcomes: VerifyOutcome[] = [];
  const callbacks: AgentCallbacks = {
    onToken: vi.fn(), onTurnEnd: vi.fn(), onAction: vi.fn(), onNotice: vi.fn(),
    onToolResult: result => results.push(result),
  };
  const verify = vi.fn(async (command: string, directory: string) => {
    const result = await runVerify(command, directory);
    outcomes.push(result);
    return result;
  });
  const opts: AgentOptions = {
    cfg: { baseURL: "http://fixture.invalid/v1", apiKey: "fixture-key", enabled: true },
    model: "fixture-coder", scope: "local", cwd, contextWindowTokens: 32768,
    confirm: vi.fn(async () => true), callbacks, verifyCommand: check, runVerify: verify,
    ...extra,
  };
  const history: ChatMessage[] = [];
  const run = () => runTask(history, "Repair state.txt so it contains good and passes the fixed check.", opts);
  return { run, opts, history, requests, results, outcomes, verify };
}

describe("fixed verification after approved shell commands", () => {
  it.each([0, 7])("does not reuse a passing check after a command mutates a file and exits %i", async code => {
    const h = harness([edit("original", "good"), await command("broken", code), "The task is complete."]);
    expect(await h.run()).toEqual({ status: "incomplete", reason: "verification_failed" });
    expect(await readFile(join(cwd, "state.txt"), "utf8")).toBe("broken");
    expect(h.outcomes.map(outcome => outcome.code)).toEqual([0, 1]);
    expect(h.opts.confirm).toHaveBeenCalledTimes(2);
    expect(h.verify.mock.calls.every(([actual]) => actual === check)).toBe(true);
    const feedback = h.results.find(result => result.startsWith(`exit ${code}\n`));
    expect(feedback).toMatch(/pending|not.*verified|recheck/i);
    expect(feedback).not.toContain(`VERIFY (\`${check}\`): passed.`);
  });

  it("accepts a real command repair after refreshing a previously failed check", async () => {
    const h = harness([edit("original", "broken"), await command("good"), "The task is complete."]);
    expect(await h.run()).toEqual({ status: "finished" });
    expect(await readFile(join(cwd, "state.txt"), "utf8")).toBe("good");
    expect(h.outcomes.map(outcome => outcome.code)).toEqual([1, 0]);
    expect(h.opts.confirm).toHaveBeenCalledTimes(2);
    const feedback = h.results.find(result => result.startsWith("exit 0\n"));
    expect(feedback).toMatch(/pending|not.*verified|recheck/i);
    expect(feedback).not.toContain("FAILED");
  });

  it("defers the pending check until final rather than rerunning it after every command", async () => {
    const h = harness([edit("original", "good"), await command("intermediate"),
      await command("good"), "The task is complete."]);
    expect(await h.run()).toEqual({ status: "finished" });
    expect(h.outcomes.map(outcome => outcome.output)).toEqual(["good", "good"]);
    expect(h.verify).toHaveBeenCalledTimes(2);
  });

  it("an intervening file edit refreshes a pending check without an extra final check", async () => {
    const h = harness([edit("original", "broken"), await command("intermediate"),
      edit("intermediate", "good"), "The task is complete."]);
    expect(await h.run()).toEqual({ status: "finished" });
    expect(h.outcomes.map(outcome => outcome.code)).toEqual([1, 0]);
    expect(h.verify).toHaveBeenCalledTimes(2);
  });

  it("declining a command stops incomplete, executes nothing, and preserves the current check", async () => {
    const confirm = vi.fn(async (request: { tool: string }) => request.tool !== "run_command");
    const h = harness([edit("original", "good"), await command("broken"), "The task is complete."], { confirm });
    expect(await h.run()).toEqual({ status: "incomplete", reason: "action_declined" });
    expect(await readFile(join(cwd, "state.txt"), "utf8")).toBe("good");
    expect(h.verify).toHaveBeenCalledOnce();
    expect(h.requests).toHaveLength(2);
    expect(h.results.some(result => result.startsWith("user declined"))).toBe(true);
  });

  it("read-only tools preserve the current check without redundant verification", async () => {
    const h = harness([edit("original", "good"), action("read_file", { path: "state.txt" }),
      action("list_dir", {}), action("search_files", { query: "good", path: "state.txt" }),
      action("glob", { pattern: "*.txt" }), "The task is complete."]);
    expect(await h.run()).toEqual({ status: "finished" });
    expect(h.verify).toHaveBeenCalledOnce();
    expect(h.results.some(result => result.startsWith("exit "))).toBe(false);
    expect(h.results.some(result => result.startsWith("error:"))).toBe(false);
  });

  it("keeps the four actual failure limit across command invalidations", async () => {
    const replies: string[] = [];
    let previous = "original";
    for (let attempt = 1; attempt <= 4; attempt++) {
      replies.push(edit(previous, `broken-${attempt}`));
      if (attempt < 4) {
        previous = `command-state-${attempt}`;
        replies.push(await command(previous));
      }
    }
    replies.push(action("write_file", { path: "should-not-exist.txt", content: "wrong" }));
    const h = harness(replies);
    expect(await h.run()).toEqual({ status: "incomplete", reason: "verification_failed" });
    expect(h.outcomes.map(outcome => outcome.code)).toEqual([1, 1, 1, 1]);
    expect(h.opts.confirm).toHaveBeenCalledTimes(7);
    await expect(readFile(join(cwd, "should-not-exist.txt"))).rejects.toMatchObject({ code: "ENOENT" });
  });

  it("stops at four actual failures reached through deferred command checks", async () => {
    const replies: string[] = [];
    for (let attempt = 1; attempt <= 4; attempt++) {
      replies.push(await command(`broken-${attempt}`), "The task is complete.");
    }
    replies.push(action("write_file", { path: "should-not-exist.txt", content: "wrong" }));
    const h = harness(replies);
    expect(await h.run()).toEqual({ status: "incomplete", reason: "verification_failed" });
    expect(h.outcomes.map(outcome => outcome.code)).toEqual([1, 1, 1, 1]);
    expect(h.opts.confirm).toHaveBeenCalledTimes(4);
    await expect(readFile(join(cwd, "should-not-exist.txt"))).rejects.toMatchObject({ code: "ENOENT" });
  });

  it("reports verification pending when repeated nonzero commands stop the task", async () => {
    const failedCommand = await command("broken", 7);
    const h = harness([edit("original", "good"), failedCommand, failedCommand, failedCommand]);
    expect(await h.run()).toEqual({ status: "incomplete", reason: "stalled" });
    expect(h.verify).toHaveBeenCalledOnce();
    expect(h.opts.confirm).toHaveBeenCalledTimes(4);
    expect(await readFile(join(cwd, "state.txt"), "utf8")).toBe("broken");
    const feedback = h.results.at(-1);
    expect(feedback).toMatch(/pending|not.*verified|recheck/i);
    expect(feedback).not.toContain(`VERIFY (\`${check}\`): passed.`);
    expect(feedback).not.toContain("FAILED");
  });

  it("does not start a fresh check when cancellation arrives during an approved command", async () => {
    const controller = new AbortController();
    const h = harness([edit("original", "good"), await command("broken"), "The task is complete."], {
      signal: controller.signal,
    });
    h.opts.callbacks.onAction = action => {
      if (action.tool === "run_command") {
        setTimeout(() => controller.abort(new Error("cancelled during command")), 0);
      }
    };
    await expect(h.run()).rejects.toThrow("cancelled during command");
    expect(await readFile(join(cwd, "state.txt"), "utf8")).toBe("broken");
    expect(h.verify).toHaveBeenCalledOnce();
    expect(h.opts.confirm).toHaveBeenCalledTimes(2);
  });

  it("cancellation during the final refreshed check cannot report completion", async () => {
    const controller = new AbortController();
    let checks = 0;
    const verify = vi.fn(async (command: string, directory: string) => {
      const outcome = await runVerify(command, directory);
      if (++checks === 2) controller.abort(new Error("cancelled during refreshed verification"));
      return outcome;
    });
    const h = harness([edit("original", "good"), await command("good"), "The task is complete."], {
      signal: controller.signal, runVerify: verify,
    });
    await expect(h.run()).rejects.toThrow("cancelled during refreshed verification");
    expect(verify).toHaveBeenCalledTimes(2);
    expect(h.opts.confirm).toHaveBeenCalledTimes(2);
  });

  it("does not introduce checks when verification is explicitly disabled", async () => {
    const h = harness([await command("good"), "The task is complete."], { verifyCommand: null });
    expect(await h.run()).toEqual({ status: "finished" });
    expect(h.verify).not.toHaveBeenCalled();
    expect(await readFile(join(cwd, "state.txt"), "utf8")).toBe("good");
  });
});

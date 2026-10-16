import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  formatVerifyResult,
  parseAction,
  runTask,
  shouldVerify,
  systemPrompt,
  trimHistory,
} from "../src/agent.js";
import { ContextBudgetError, estimateContextTokens } from "../src/context.js";
import type { GatewayConfig } from "../src/config.js";
import type { ChatMessage } from "../src/gateway.js";

// Replace the gateway's streamChat with a scriptable async generator so the loop
// can be driven without a live gateway.
const streamChatMock = vi.fn();
vi.mock("../src/gateway.js", () => ({
  streamChat: (...args: unknown[]) => streamChatMock(...args),
}));

/** Feed one assistant message per turn; later turns repeat a final answer. */
function scriptTurns(turns: string[]): void {
  let i = 0;
  streamChatMock.mockImplementation(() => {
    const text = turns[i] ?? "done.";
    i++;
    return (async function* () {
      yield text;
    })();
  });
}

function action(tool: string, args: Record<string, unknown>): string {
  return ["```action", JSON.stringify({ tool, args }), "```"].join("\n");
}

const CFG: GatewayConfig = { baseURL: "x", apiKey: "k", enabled: true };

describe("parseAction", () => {
  it("accepts positive line ranges and rejects invalid read_file offsets/limits", () => {
    expect(parseAction(action("read_file", { path: "a.ts", offset: 201, limit: 50 })).kind).toBe("action");
    for (const args of [{ offset: 0 }, { offset: -1 }, { limit: 1.5 }, { limit: "50" }]) {
      expect(parseAction(action("read_file", { path: "a.ts", ...args })).kind).toBe("error");
    }
  });
  it("extracts the first action block and parses its JSON", () => {
    const text = [
      "Let me read that file first.",
      "```action",
      '{"tool": "read_file", "args": {"path": "src/foo.ts"}}',
      "```",
    ].join("\n");

    const result = parseAction(text);
    expect(result.kind).toBe("action");
    if (result.kind !== "action") throw new Error("expected action");
    expect(result.action.tool).toBe("read_file");
    expect(result.action.args).toEqual({ path: "src/foo.ts" });
  });

  it("uses only the FIRST action block when several appear", () => {
    const text = [
      "```action",
      '{"tool": "list_dir", "args": {"path": "."}}',
      "```",
      "and then",
      "```action",
      '{"tool": "read_file", "args": {"path": "b.ts"}}',
      "```",
    ].join("\n");

    const result = parseAction(text);
    if (result.kind !== "action") throw new Error("expected action");
    expect(result.action.tool).toBe("list_dir");
  });

  it("treats a message with no action block as a final answer", () => {
    const result = parseAction("All done. I updated the config and ran the tests.");
    expect(result.kind).toBe("final");
    if (result.kind !== "final") throw new Error("expected final");
    expect(result.text).toContain("All done");
  });

  it("returns an error result for malformed JSON so the loop can retry", () => {
    const text = ["```action", '{"tool": "read_file", "args": {path: oops}}', "```"].join(
      "\n",
    );
    const result = parseAction(text);
    expect(result.kind).toBe("error");
    if (result.kind !== "error") throw new Error("expected error");
    expect(result.message).toMatch(/invalid JSON/i);
  });

  it("errors when the action object has no string tool field", () => {
    const text = ["```action", '{"args": {"path": "x"}}', "```"].join("\n");
    const result = parseAction(text);
    expect(result.kind).toBe("error");
  });

  it("defaults args to an empty object when omitted", () => {
    const text = ["```action", '{"tool": "list_dir"}', "```"].join("\n");
    const result = parseAction(text);
    if (result.kind !== "action") throw new Error("expected action");
    expect(result.action.args).toEqual({});
  });

  it("accepts inlined top-level args when a weak model omits the args object", () => {
    const text = ["```action", '{"tool": "run_command", "command": "ls src"}', "```"].join(
      "\n",
    );
    const result = parseAction(text);
    if (result.kind !== "action") throw new Error("expected action");
    expect(result.action.tool).toBe("run_command");
    expect(result.action.args).toEqual({ command: "ls src" });
  });

  it("accepts `arguments` as an alias for `args`", () => {
    const text = [
      "```action",
      '{"tool": "read_file", "arguments": {"path": "a.ts"}}',
      "```",
    ].join("\n");
    const result = parseAction(text);
    if (result.kind !== "action") throw new Error("expected action");
    expect(result.action.args).toEqual({ path: "a.ts" });
  });

  it("falls back to a relaxed parse for single-quoted values with embedded quotes", () => {
    const text = [
      "```action",
      `{"tool": "edit_file", "args": {"path": "src/index.ts", "old_string": '    if (line === "/exit" || line === "/quit") break;', "new_string": '    if (line === "/exit" || line === "/quit" || line === "/tools") break;'}}`,
      "```",
    ].join("\n");
    const result = parseAction(text);
    if (result.kind !== "action") throw new Error("expected action");
    expect(result.action.tool).toBe("edit_file");
    expect(result.action.args).toEqual({
      path: "src/index.ts",
      old_string: '    if (line === "/exit" || line === "/quit") break;',
      new_string:
        '    if (line === "/exit" || line === "/quit" || line === "/tools") break;',
    });
  });

  it("relaxed-parses unquoted keys and trailing commas", () => {
    const text = ["```action", "{tool: 'read_file', args: {path: 'a.ts',},}", "```"].join(
      "\n",
    );
    const result = parseAction(text);
    if (result.kind !== "action") throw new Error("expected action");
    expect(result.action.tool).toBe("read_file");
    expect(result.action.args).toEqual({ path: "a.ts" });
  });

  it("relaxed-parses inlined top-level args written with single quotes", () => {
    const text = ["```action", "{tool: 'run_command', command: 'ls src'}", "```"].join(
      "\n",
    );
    const result = parseAction(text);
    if (result.kind !== "action") throw new Error("expected action");
    expect(result.action.tool).toBe("run_command");
    expect(result.action.args).toEqual({ command: "ls src" });
  });

  it("still returns an error when both strict and relaxed parses fail", () => {
    for (const broken of ['{"tool":', "{a: }", "{unterminated: 'oops}"]) {
      const text = ["```action", broken, "```"].join("\n");
      const result = parseAction(text);
      expect(result.kind).toBe("error");
      if (result.kind !== "error") throw new Error("expected error");
      expect(result.message).toMatch(/invalid JSON/i);
    }
  });

  it("parses a mislabeled ```json fence as an action (not a final answer)", () => {
    const text = ["```json", '{"tool": "read_file", "args": {"path": "a.ts"}}', "```"].join(
      "\n",
    );
    const result = parseAction(text);
    if (result.kind !== "action") throw new Error("expected action");
    expect(result.action.tool).toBe("read_file");
  });

  it("parses an UNLABELED fenced tool object as an action", () => {
    const text = ["```", '{"tool": "list_dir", "args": {"path": "."}}', "```"].join("\n");
    const result = parseAction(text);
    if (result.kind !== "action") throw new Error("expected action");
    expect(result.action.tool).toBe("list_dir");
  });

  it("parses a bare top-level tool object with no fence at all", () => {
    const result = parseAction('{"tool": "read_file", "args": {"path": "x.ts"}}');
    if (result.kind !== "action") throw new Error("expected action");
    expect(result.action.args).toEqual({ path: "x.ts" });
  });

  it("does NOT mistake a prose code fence in a final answer for an action", () => {
    const text = [
      "Here is the change I made:",
      "```ts",
      "export const x = 1;",
      "```",
      "That completes the task.",
    ].join("\n");
    const result = parseAction(text);
    expect(result.kind).toBe("final");
  });
});

describe("trimHistory", () => {
  it("returns the history unchanged when under budget", () => {
    const h: ChatMessage[] = [
      { role: "system", content: "sys" },
      { role: "user", content: "hi" },
    ];
    expect(trimHistory(h, 1000)).toBe(h);
  });

  it("keeps the system prompt + recent turns and inserts a marker when over budget", () => {
    const big = "x".repeat(100);
    const h: ChatMessage[] = [
      { role: "system", content: "SYS" },
      { role: "user", content: big },
      { role: "assistant", content: big },
      { role: "user", content: "recent" },
    ];
    const trimmed = trimHistory(h, 180); // system + marker + recent answer/task fit
    expect(trimmed[0].content).toBe("SYS");
    expect(trimmed[1].content).toMatch(/trimmed to fit/);
    expect(trimmed[trimmed.length - 1].content).toBe("recent");
    expect(trimmed).not.toContain(h[1]);
  });

  it("refuses a task that cannot fit instead of silently exceeding the budget", () => {
    const h: ChatMessage[] = [
      { role: "system", content: "S" },
      { role: "user", content: "y".repeat(500) },
    ];
    expect(() => trimHistory(h, 10)).toThrow(ContextBudgetError);
  });

  it("pins the task so the goal survives even when it is the oldest user turn", () => {
    const h: ChatMessage[] = [
      { role: "system", content: "S" },
      { role: "user", content: "THE TASK: refactor foo" }, // the goal
      { role: "assistant", content: "a".repeat(200) },
      { role: "user", content: "TOOL_RESULT: " + "b".repeat(200) },
      { role: "assistant", content: "c".repeat(200) },
      { role: "user", content: "TOOL_RESULT: latest" },
    ];
    const trimmed = trimHistory(h, 300);
    expect(trimmed.some((m) => m.content.includes("THE TASK"))).toBe(true);
    expect(trimmed[trimmed.length - 1].content).toContain("latest");
    expect(trimmed).toContain(h[4]);
    expect(trimmed.some((m) => /trimmed to fit/.test(m.content))).toBe(true);
  });
});

describe("shouldVerify", () => {
  it("is true after a successful edit/write", () => {
    expect(shouldVerify("edit_file", "edited f.ts")).toBe(true);
    expect(shouldVerify("write_file", "wrote 10 bytes to f.ts")).toBe(true);
  });

  it("is false for read-only tools and run_command", () => {
    expect(shouldVerify("read_file", "contents")).toBe(false);
    expect(shouldVerify("list_dir", "a\nb")).toBe(false);
    expect(shouldVerify("search_files", "f:1: hit")).toBe(false);
    expect(shouldVerify("glob", "a.ts")).toBe(false);
    expect(shouldVerify("run_command", "exit 0\nok")).toBe(false);
  });

  it("is false when the edit failed or was declined (nothing changed)", () => {
    expect(shouldVerify("edit_file", "error: old_string not found")).toBe(false);
    expect(shouldVerify("write_file", "user declined")).toBe(false);
  });
});

describe("formatVerifyResult", () => {
  it("formats a pass", () => {
    expect(formatVerifyResult("pnpm build", { code: 0, output: "" })).toBe(
      "\nVERIFY (`pnpm build`): passed.",
    );
  });

  it("formats a failure with the exit code and capped output", () => {
    const s = formatVerifyResult("pnpm build", { code: 2, output: "TS2322 boom" });
    expect(s).toContain("VERIFY (`pnpm build`) FAILED (exit 2):");
    expect(s).toContain("TS2322 boom");
  });
});

describe("runTask verify loop", () => {
  let cwd = "";

  async function tmp(): Promise<string> {
    cwd = await mkdtemp(join(tmpdir(), "potluck-agent-"));
    return cwd;
  }

  afterEach(async () => {
    streamChatMock.mockReset();
    if (cwd) await rm(cwd, { recursive: true, force: true });
    cwd = "";
  });

  it("drains a tool turn to completion without accepting post-action output", async () => {
    const dir = await tmp();
    await writeFile(join(dir, "f.ts"), "const a = 1;\n");
    const edit = action("edit_file", {
      path: "f.ts", old_string: "a = 1", new_string: "a = 2",
    });
    let completed = false;
    streamChatMock.mockImplementationOnce(() => (async function* () {
      yield edit.slice(0, -3);
      yield edit.slice(-3) + "\nINVENTED TOOL OUTPUT";
      yield action("write_file", { path: "unwanted.ts", content: "wrong" });
      completed = true;
    })());
    streamChatMock.mockImplementationOnce(() => (async function* () { yield "Done."; })());
    const tokens: string[] = [];
    const history: ChatMessage[] = [];
    const verify = vi.fn(async () => {
      expect(completed).toBe(true);
      expect(await readFile(join(dir, "f.ts"), "utf8")).toBe("const a = 2;\n");
      return { code: 0, output: "passed" };
    });
    await runTask(history, "bump", {
      cfg: CFG, model: "m", cwd: dir, verifyCommand: "fixed-check", runVerify: verify,
      confirm: async () => { expect(completed).toBe(true); return true; },
      callbacks: {
        onToken: t => tokens.push(t), onTurnEnd: () => {},
        onAction: () => { expect(completed).toBe(true); }, onToolResult: () => {},
      },
    });
    expect(tokens.join("")).toBe(edit + "Done.");
    expect(history.filter(m => m.role === "assistant").map(m => m.content)).toEqual([edit, "Done."]);
    expect(verify).toHaveBeenCalledOnce();
    expect(streamChatMock.mock.calls[0][2]).toMatchObject({ requireDone: true, stop: ["\nTOOL_RESULT:"] });
    await expect(readFile(join(dir, "unwanted.ts"))).rejects.toMatchObject({ code: "ENOENT" });
  });

  it("does not execute a complete-looking action from an interrupted response", async () => {
    const dir = await tmp();
    await writeFile(join(dir, "f.ts"), "const a = 1;\n");
    streamChatMock.mockImplementationOnce(() => (async function* () {
      yield action("edit_file", { path: "f.ts", old_string: "a = 1", new_string: "a = 2" });
      throw new Error("Gateway stream ended before completion");
    })());
    const confirm = vi.fn(async () => true);
    const onAction = vi.fn();
    const onTurnEnd = vi.fn();
    const history: ChatMessage[] = [];
    await expect(runTask(history, "bump", {
      cfg: CFG, model: "m", cwd: dir, confirm,
      callbacks: { onToken: () => {}, onTurnEnd, onAction, onToolResult: () => {} },
    })).rejects.toThrow("before completion");
    expect(confirm).not.toHaveBeenCalled();
    expect(onAction).not.toHaveBeenCalled();
    expect(onTurnEnd).not.toHaveBeenCalled();
    expect(history).toEqual([{ role: "user", content: "bump" }]);
    expect(await readFile(join(dir, "f.ts"), "utf8")).toBe("const a = 1;\n");
  });

  it("runs verify after a successful edit and feeds a FAILURE back", async () => {
    const dir = await tmp();
    await writeFile(join(dir, "f.ts"), "const a = 1;\n", "utf8");
    scriptTurns([
      action("edit_file", {
        path: "f.ts",
        old_string: "const a = 1;",
        new_string: "const a = 2;",
      }),
      "All set.",
    ]);

    const calls: Array<[string, string]> = [];
    const results: string[] = [];
    const verifies: Array<{ cmd: string; ok: boolean }> = [];
    const history: ChatMessage[] = [{ role: "system", content: "sys" }];

    await runTask(history, "bump a", {
      cfg: CFG,
      model: "m",
      cwd: dir,
      confirm: async () => true,
      verifyCommand: "fake-verify",
      runVerify: async (cmd, c) => {
        calls.push([cmd, c]);
        return { code: 1, output: "f.ts(1): error TS2322 boom" };
      },
      callbacks: {
        onToken: () => {},
        onTurnEnd: () => {},
        onAction: () => {},
        onToolResult: (r) => results.push(r),
        onVerify: (cmd, ok) => verifies.push({ cmd, ok }),
      },
    });

    expect(calls).toEqual([["fake-verify", dir]]);
    expect(verifies).toEqual([{ cmd: "fake-verify", ok: false }]);
    const fed = results.find((r) => r.includes("VERIFY"));
    expect(fed).toContain("VERIFY (`fake-verify`) FAILED (exit 1):");
    expect(fed).toContain("boom");
    // The failure was also folded into the TOOL_RESULT fed back to the model.
    expect(
      history.some(
        (m) => m.role === "user" && m.content.includes("VERIFY (`fake-verify`) FAILED"),
      ),
    ).toBe(true);
  });

  it("appends a passed line when verify succeeds", async () => {
    const dir = await tmp();
    await writeFile(join(dir, "f.ts"), "const a = 1;\n", "utf8");
    scriptTurns([
      action("edit_file", {
        path: "f.ts",
        old_string: "const a = 1;",
        new_string: "const a = 2;",
      }),
      "done",
    ]);

    const results: string[] = [];
    await runTask([{ role: "system", content: "s" }], "bump", {
      cfg: CFG,
      model: "m",
      cwd: dir,
      confirm: async () => true,
      verifyCommand: "fake-verify",
      runVerify: async () => ({ code: 0, output: "" }),
      callbacks: {
        onToken: () => {},
        onTurnEnd: () => {},
        onAction: () => {},
        onToolResult: (r) => results.push(r),
      },
    });

    expect(results.find((r) => r.includes("VERIFY"))).toContain(
      "VERIFY (`fake-verify`): passed.",
    );
  });

  it("does not verify the read itself, but checks an otherwise unverified final response", async () => {
    const dir = await tmp();
    await writeFile(join(dir, "r.txt"), "hello", "utf8");
    scriptTurns([action("read_file", { path: "r.txt" }), "done"]);

    const spy = vi.fn(async () => ({ code: 0, output: "" }));
    await runTask([{ role: "system", content: "s" }], "read it", {
      cfg: CFG,
      model: "m",
      cwd: dir,
      confirm: async () => true,
      verifyCommand: "fake-verify",
      runVerify: spy,
      callbacks: {
        onToken: () => {},
        onTurnEnd: () => {},
        onAction: () => {},
        onToolResult: () => {},
      },
    });

    expect(spy).toHaveBeenCalledOnce();
  });

  it("defers one fresh check across diagnostic commands and keeps pending status through compaction", async () => {
    const dir = await tmp();
    const verify = vi.fn(async () => ({ code: 0, output: "passed" }));
    scriptTurns([
      action("write_file", { path: "value.txt", content: "ready" }),
      action("run_command", { command: "echo diagnostic" }),
      action("read_file", { path: "value.txt" }),
      action("run_command", { command: "echo another-diagnostic" }),
      "done",
    ]);
    const feedback: string[] = [];
    const result = await runTask([{ role: "system", content: "s" }], "repair this", {
      cfg: CFG, model: "m", cwd: dir, confirm: async () => true,
      verifyCommand: "fixed-check", runVerify: verify, charBudget: 1000,
      callbacks: {
        onToken: () => {}, onTurnEnd: () => {}, onAction: () => {},
        onToolResult: text => { feedback.push(text); expect(verify).toHaveBeenCalledOnce(); },
      },
    });
    expect(result).toEqual({ status: "finished" });
    expect(verify).toHaveBeenCalledTimes(2);
    expect(feedback.slice(1)).toHaveLength(3);
    for (const text of feedback.slice(1)) {
      expect(text).toContain("VERIFY pending");
      expect(text).not.toContain("passed.");
    }
    const finalRequest = streamChatMock.mock.calls.at(-1)![1] as ChatMessage[];
    expect(finalRequest.some(message => /trimmed to fit/.test(message.content))).toBe(true);
    expect(finalRequest.at(-1)?.content).toContain("FIXED CHECK PENDING");
    expect(finalRequest.at(-1)?.content).not.toContain("STILL FAILS");
  });

  it("does not start the fixed check when its start callback cancels the task", async () => {
    const dir = await tmp();
    const controller = new AbortController();
    const verify = vi.fn(async () => ({ code: 0, output: "passed" }));
    scriptTurns(["done"]);
    await expect(runTask([], "repair this", {
      cfg: CFG, model: "m", cwd: dir, confirm: async () => true,
      verifyCommand: "fixed-check", runVerify: verify, signal: controller.signal,
      callbacks: {
        onToken: () => {}, onTurnEnd: () => {}, onAction: () => {}, onToolResult: () => {},
        onVerifyStart: () => controller.abort(new Error("cancelled before fixed check")),
      },
    })).rejects.toThrow("cancelled before fixed check");
    expect(verify).not.toHaveBeenCalled();
  });

  it("stops the task at the unchanged verify failure cap instead of editing blindly", async () => {
    const dir = await tmp();
    scriptTurns(Array.from({ length: 5 }, (_, i) => action("write_file", { path: "out.txt", content: `attempt ${i}` })));

    const spy = vi.fn(async () => ({ code: 1, output: "boom" }));
    const notices: string[] = [];
    const result = await runTask([{ role: "system", content: "s" }], "spam writes", {
      cfg: CFG,
      model: "m",
      cwd: dir,
      confirm: async () => true,
      verifyCommand: "fake-verify",
      maxVerifyFailures: 4,
      runVerify: spy,
      callbacks: {
        onToken: () => {},
        onTurnEnd: () => {},
        onAction: () => {},
        onToolResult: () => {},
        onNotice: (m) => {
          notices.push(m);
        },
      },
    });

    // The fifth write is never executed; the same four-failure cap still holds.
    expect(spy).toHaveBeenCalledTimes(4);
    expect(await readFile(join(dir, "out.txt"), "utf8")).toBe("attempt 3");
    expect(notices.some(m => /stopping before further unverified edits/.test(m))).toBe(true);
    expect(result).toEqual({ status: "incomplete", reason: "verification_failed" });
  });

  it("never runs verify when no verify command is configured", async () => {
    const dir = await tmp();
    await writeFile(join(dir, "f.ts"), "const a = 1;\n", "utf8");
    scriptTurns([
      action("edit_file", {
        path: "f.ts",
        old_string: "const a = 1;",
        new_string: "const a = 2;",
      }),
      "done",
    ]);

    const spy = vi.fn(async () => ({ code: 0, output: "" }));
    await runTask([{ role: "system", content: "s" }], "bump", {
      cfg: CFG,
      model: "m",
      cwd: dir,
      confirm: async () => true,
      verifyCommand: null,
      runVerify: spy,
      callbacks: {
        onToken: () => {},
        onTurnEnd: () => {},
        onAction: () => {},
        onToolResult: () => {},
      },
    });

    expect(spy).not.toHaveBeenCalled();
  });
});

describe("runTask repeat-detection", () => {
  const dirs: string[] = [];
  afterEach(async () => {
    await Promise.all(dirs.splice(0).map((d) => rm(d, { recursive: true, force: true })));
  });
  async function tmp(): Promise<string> {
    const d = await mkdtemp(join(tmpdir(), "potluck-cli-repeat-"));
    dirs.push(d);
    return d;
  }

  it("aborts when the identical action keeps failing, instead of looping to maxSteps", async () => {
    const dir = await tmp();
    await writeFile(join(dir, "f.ts"), "const a = 1;\n", "utf8");
    // The model is stuck: it re-emits the SAME edit whose old_string never matches.
    scriptTurns(
      Array.from({ length: 10 }, () =>
        action("edit_file", {
          path: "f.ts",
          old_string: "does-not-exist",
          new_string: "whatever",
        }),
      ),
    );

    const results: string[] = [];
    let notice = "";
    await runTask([{ role: "system", content: "s" }], "edit it", {
      cfg: CFG,
      model: "m",
      cwd: dir,
      confirm: async () => true,
      maxSteps: 24,
      callbacks: {
        onToken: () => {},
        onTurnEnd: () => {},
        onAction: () => {},
        onToolResult: (r) => results.push(r),
        onNotice: (m) => {
          notice = m;
        },
      },
    });

    // Bailed after 3 identical failures, not 24 steps.
    expect(results.length).toBe(3);
    expect(notice).toMatch(/failed identically/i);
    expect(results.every((r) => r.startsWith("error:"))).toBe(true);
  });
});

describe("runTask context safety", () => {
  const dirs: string[] = [];
  const callbacks = {
    onToken: () => {}, onTurnEnd: () => {}, onAction: () => {}, onToolResult: () => {},
  };
  beforeEach(() => streamChatMock.mockReset());
  afterEach(async () => {
    streamChatMock.mockReset();
    await Promise.all(dirs.splice(0).map(dir => rm(dir, { recursive: true, force: true })));
  });
  async function tmp(): Promise<string> {
    const dir = await mkdtemp(join(tmpdir(), "potluck-cli-context-"));
    dirs.push(dir);
    return dir;
  }

  it("refuses oversized pinned instructions before streaming or running a tool", async () => {
    const notice = vi.fn();
    scriptTurns([action("write_file", { path: "new.ts", content: "unexpected" })]);
    const result = await runTask([{ role: "system", content: "x".repeat(8192) }], "fix this", {
      cfg: CFG, model: "m", cwd: await tmp(), confirm: async () => true,
      contextWindowTokens: 8192, callbacks: { ...callbacks, onNotice: notice },
    });
    expect(result).toEqual({ status: "incomplete", reason: "context_limit" });
    expect(streamChatMock).not.toHaveBeenCalled();
    expect(notice).toHaveBeenCalledWith(expect.stringContaining("larger context window"));
  });

  it("keeps the exact current task when its text starts with the tool-feedback prefix", async () => {
    const task = "TOOL_RESULT: This is quoted output. Explain what it means.";
    const history: ChatMessage[] = [
      { role: "system", content: "system" },
      { role: "user", content: "old task".repeat(2000) },
      { role: "assistant", content: "old answer" },
    ];
    streamChatMock.mockImplementation((_cfg, messages: ChatMessage[]) => {
      expect(messages.at(-1)).toEqual({ role: "user", content: task });
      expect(messages).not.toContain(history[1]);
      return (async function* () { yield "Explanation."; })();
    });
    expect(await runTask(history, task, {
      cfg: CFG, model: "m", cwd: await tmp(), confirm: async () => true,
      contextWindowTokens: 4096, callbacks,
    })).toEqual({ status: "finished" });
    expect(streamChatMock).toHaveBeenCalledOnce();
  });

  it("does not let a context-truncated full read authorize whole-file replacement", async () => {
    const dir = await tmp();
    const original = "export const original = 1;\n".repeat(80);
    await writeFile(join(dir, "large.ts"), original);
    scriptTurns([
      action("read_file", { path: "large.ts" }),
      action("write_file", { path: "large.ts", content: "unrelated functions lost\n" }),
      "I cannot replace a partial view.",
    ]);
    const confirm = vi.fn(async () => true);
    const results: string[] = [];
    const history: ChatMessage[] = [{ role: "system", content: "s" }];
    await runTask(history, "fix this", {
      cfg: CFG, model: "m", cwd: dir, confirm, charBudget: 700,
      contextWindowTokens: 8192,
      callbacks: { ...callbacks, onToolResult: result => results.push(result) },
    });
    expect(streamChatMock.mock.calls[1][1].at(-1).content).toContain("Tool output truncated");
    expect(results[1]).toContain("without reading it first");
    expect(confirm).not.toHaveBeenCalled();
    expect(await readFile(join(dir, "large.ts"), "utf8")).toBe(original);
    expect(history[3].content).toContain(original);
  });

  it("allows a fresh small full read after earlier context has been compacted", async () => {
    const dir = await tmp();
    await writeFile(join(dir, "large.ts"), "export const original = 1;\n".repeat(80));
    await writeFile(join(dir, "small.ts"), "const a = 1;\n");
    scriptTurns([
      action("read_file", { path: "large.ts" }),
      action("read_file", { path: "small.ts" }),
      action("write_file", { path: "small.ts", content: "const a = 2;\n" }),
      "done",
    ]);
    const confirm = vi.fn(async () => true);
    await runTask([{ role: "system", content: "s" }], "fix this", {
      cfg: CFG, model: "m", cwd: dir, confirm, charBudget: 700,
      contextWindowTokens: 8192, callbacks,
    });
    expect(confirm).toHaveBeenCalledOnce();
    expect(await readFile(join(dir, "small.ts"), "utf8")).toBe("const a = 2;\n");
  });

  it("retains failed-verification enforcement after large Unicode feedback is excerpted", async () => {
    const dir = await tmp();
    const requests: { messages: ChatMessage[]; options: { maxTokens: number; requireDone: boolean } }[] = [];
    streamChatMock.mockImplementation((_cfg, messages, options) => {
      requests.push({ messages: structuredClone(messages), options });
      const response = requests.length === 1
        ? action("write_file", { path: "new.ts", content: "incorrect\n" }) : "Everything passed.";
      return (async function* () { yield response; })();
    });
    const result = await runTask([{ role: "system", content: systemPrompt(dir, "npm test") }], "Fix the bug", {
      cfg: CFG, model: "m", cwd: dir, confirm: async () => true,
      contextWindowTokens: 8192, verifyCommand: "npm test",
      runVerify: async () => ({ code: 1, output: "错误🧑🏽‍💻".repeat(4000) }), callbacks,
    });
    expect(result).toEqual({ status: "incomplete", reason: "verification_failed" });
    expect(streamChatMock.mock.calls.length).toBeGreaterThan(1);
    for (const { messages, options } of requests) {
      expect(estimateContextTokens(messages) + options.maxTokens).toBeLessThanOrEqual(8192);
      expect(options).toMatchObject({ maxTokens: 2048, requireDone: true });
    }
  });
});

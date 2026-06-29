import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  formatVerifyResult,
  parseAction,
  runTask,
  shouldVerify,
  trimHistory,
} from "../src/agent.js";
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
    const trimmed = trimHistory(h, 60); // system + recent fit; the two big turns drop
    expect(trimmed[0].content).toBe("SYS");
    expect(trimmed[1].content).toMatch(/trimmed to fit/);
    expect(trimmed[trimmed.length - 1].content).toBe("recent");
    expect(trimmed.length).toBeLessThan(h.length);
  });

  it("always keeps at least the most recent message", () => {
    const h: ChatMessage[] = [
      { role: "system", content: "S" },
      { role: "user", content: "y".repeat(500) },
    ];
    const trimmed = trimHistory(h, 10);
    expect(trimmed[trimmed.length - 1].content).toBe("y".repeat(500));
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

  it("does not run verify after a read-only tool", async () => {
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

    expect(spy).not.toHaveBeenCalled();
  });

  it("stops auto-verify after the consecutive-failure cap", async () => {
    const dir = await tmp();
    const write = action("write_file", { path: "out.txt", content: "x" });
    scriptTurns([write, write, write, write, write, "done"]);

    const spy = vi.fn(async () => ({ code: 1, output: "boom" }));
    let notice = "";
    await runTask([{ role: "system", content: "s" }], "spam writes", {
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
          notice = m;
        },
      },
    });

    // 5 successful writes occur, but verify stops after 4 failures in a row.
    expect(spy).toHaveBeenCalledTimes(4);
    expect(notice).toMatch(/stopping auto-verify/);
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

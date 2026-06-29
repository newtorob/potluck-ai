import { describe, expect, it } from "vitest";

import { parseAction, trimHistory } from "../src/agent.js";
import type { ChatMessage } from "../src/gateway.js";

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

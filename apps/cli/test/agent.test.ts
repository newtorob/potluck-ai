import { describe, expect, it } from "vitest";

import { parseAction } from "../src/agent.js";

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
});

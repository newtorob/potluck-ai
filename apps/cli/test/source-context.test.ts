import { expect, it } from "vitest";
import { SourceContext, turnBudgetGuidance } from "../src/source-context.js";
import { prepareContext } from "../src/context.js";
import type { ChatMessage } from "../src/gateway.js";
import type { FileReadObservation } from "../src/tools.js";

const message = (content: string): ChatMessage => ({ role: "user", content: "TOOL_RESULT: " + content });
const read = (startLine = 1, content = "one\ntwo\nthree\nfour\nfive\n", path = "/repo/a.py"): FileReadObservation =>
  ({ path, startLine, content, truncatedLine: false });

it("keeps the named function ahead of newer unrelated ranges in the same canonical file", () => {
  const sources = new SourceContext("/repo", "Fix calculate in src/a.py");
  const target = message("def calculate(x):\n    return x\n");
  sources.remember(read(300, "def calculate(x):\n    return x\n", "/repo/src/a.py"), target);
  for (let index = 0; index < 40; index++) sources.remember(read(500 + index, "unrelated\n", "/repo/src/a.py"), message("other"));
  expect(sources.priorities()[0]).toBe(target);
  expect(sources.priorities()).toHaveLength(6);
});

it("matches relative paths in the request against canonical read paths", () => {
  const sources = new SourceContext("/repo", "Repair src/a.py");
  const target = message("target");
  sources.remember(read(1, "target\n", "/repo/src/a.py"), target);
  sources.remember(read(1, "noise\n", "/repo/other.py"), message("noise"));
  expect(sources.priorities()[0]).toBe(target);
});

it("recognizes unchanged subsets across different read bounds", () => {
  const sources = new SourceContext("/repo", "fix a.py");
  const first = message("source"); sources.remember(read(), first);
  const visible = new Set([first]);
  expect(sources.assess(read(2, "two\nthree\n"), visible)).toMatchObject({ stalled: false, feedback: expect.stringContaining("every returned line") });
  expect(sources.assess(read(3, "three\nfour\n"), visible).stalled).toBe(true);
  expect(sources.assess(read(1, "one\n"), visible).stalled).toBe(true);
});

it("does not stop a useful extension of a mostly overlapping read", () => {
  const sources = new SourceContext("/repo", "fix a.py"), first = message("source");
  sources.remember(read(), first);
  for (let i = 0; i < 4; i++) expect(sources.assess(read(1, read().content + "six\n"), new Set([first])))
    .toMatchObject({ stalled: false, feedback: expect.stringContaining("5 of 6") });
});

it("permits rereading evidence removed or excerpted by compaction", () => {
  const sources = new SourceContext("/repo", "fix"), first = message("full source");
  sources.remember(read(), first);
  for (let i = 0; i < 4; i++) expect(sources.assess(read(), new Set([{ ...first, content: "excerpt" }])))
    .toEqual({ stalled: false, feedback: "" });
});

it("invalidates old file ranges when an overlapping source line changes", () => {
  const sources = new SourceContext("/repo", "fix"), first = message("old");
  sources.remember(read(), first);
  expect(sources.assess(read(2, "CHANGED\n"), new Set([first]))).toEqual({ feedback: "", stalled: false });
  expect(sources.priorities()).toEqual([]);
});

it("does not confuse identical text at different positions or in different files", () => {
  const sources = new SourceContext("/repo", "fix"), first = message("source");
  sources.remember(read(), first);
  expect(sources.assess(read(20), new Set([first])).feedback).toBe("");
  expect(sources.assess(read(1, read().content, "/repo/b.py"), new Set([first])).feedback).toBe("");
});

it("does not use a truncated line as proof of a redundant read", () => {
  const sources = new SourceContext("/repo", "fix"), first = message("source");
  sources.remember({ ...read(), truncatedLine: true }, first);
  expect(sources.assess(read(), new Set([first])).feedback).toBe("");
});

it("clears stale source and repeat state after mutations or shell execution", () => {
  const sources = new SourceContext("/repo", "fix"), first = message("source");
  sources.remember(read(), first);
  sources.assess(read(), new Set([first])); sources.invalidate("/repo/a.py");
  expect(sources.priorities()).toEqual([]);
  sources.remember(read(), first); sources.clear();
  expect(sources.assess(read(), new Set([first])).feedback).toBe("");
});

it("shows a decreasing budget without encouraging unchecked guesses", () => {
  expect(turnBudgetGuidance(12, 12, true)).toContain("12 model turns remain");
  expect(turnBudgetGuidance(6, 12, true)).toContain("automatically runs the fixed check");
  expect(turnBudgetGuidance(1, 12, false)).toContain("Do not make speculative edits");
});

it("honors source priorities with conservative counting and preserves action/result pairs", () => {
  const history: ChatMessage[] = [{ role: "system", content: "rules" }, { role: "user", content: "task" },
    { role: "assistant", content: "read target" }, message("target\n".repeat(60)),
    { role: "assistant", content: "read noise" }, message("noise\n".repeat(250)),
    { role: "assistant", content: "read newest" }, message("latest")];
  const result = prepareContext(history, { contextWindowTokens: 1600, maxOutputTokens: 300, priorityMessages: [history[3]], currentTaskIndex: 1 });
  expect(result.messages).toContain(history[3]); expect(result.messages).toContain(history[2]);
  expect(result.messages).not.toContain(history[5]); expect(result.messages).toContain(history[7]);
});

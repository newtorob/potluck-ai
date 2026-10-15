import { describe, expect, it } from "vitest";
import { systemPrompt } from "../src/agent.js";
import { ContextBudgetError, estimateContextTokens, prepareContext, trimHistory } from "../src/context.js";
import type { ChatMessage } from "../src/gateway.js";

function exchange(label: string, content: string): ChatMessage[] {
  return [
    { role: "assistant", content: `\`\`\`action\n{"tool":"read_file","args":{"path":"${label}"}}\n\`\`\`` },
    { role: "user", content: `TOOL_RESULT: FILE "${label}":\n${content}` },
  ];
}

describe("bounded model context", () => {
  it("fits the production prompt, verification and a real tool round trip in the 4K fallback", () => {
    const history: ChatMessage[] = [
      { role: "system", content: systemPrompt("/Users/rob/Documents/potluck", "npm test") },
      { role: "user", content: "Fix the addition bug and preserve the existing error handling." },
      ...exchange("math.ts", "export const add = (a: number, b: number) => a - b;"),
    ];
    const prepared = prepareContext(history);
    expect(prepared.usedFallback).toBe(true);
    expect(prepared.maxOutputTokens).toBe(1024);
    expect(prepared.messages).toBe(history);
    expect(estimateContextTokens(prepared.messages) + prepared.maxOutputTokens).toBeLessThanOrEqual(4096);
  });

  it.each([
    ["ASCII code", "function add(a,b){return a+b;}\n".repeat(2300)],
    ["Unicode", "变量🧑🏽‍💻追加変更\n".repeat(6000)],
  ])("bounds a 64K+ latest %s result with real prompts and an 8K runtime", (_label, content) => {
    const system = { role: "system" as const, content: systemPrompt("/project", "npm test") };
    const task = { role: "user" as const, content: "Find and correct the bug without removing unrelated functions." };
    const history = [system, task, ...exchange("large.ts", content + "TAIL: actual result")];
    const original = structuredClone(history);
    const prepared = prepareContext(history, { contextWindowTokens: 8192 });
    expect(estimateContextTokens(prepared.messages) + prepared.maxOutputTokens).toBeLessThanOrEqual(8192);
    expect(prepared.messages).toContain(system);
    expect(prepared.messages).toContain(task);
    expect(prepared.messages).toContain(history[2]);
    expect(prepared.messages.at(-1)?.content).toContain("Tool output truncated");
    expect(prepared.messages.at(-1)?.content).toContain("TAIL: actual result");
    expect(prepared.messages.at(-1)?.content.isWellFormed()).toBe(true);
    expect(history).toEqual(original);
  });

  it("never exceeds the legacy 48K cap with the reproduced 64K latest result", () => {
    const history: ChatMessage[] = [
      { role: "system", content: "system" },
      { role: "user", content: "current task" },
      ...exchange("large.ts", "x".repeat(64_000)),
    ];
    const view = trimHistory(history);
    expect(view.reduce((sum, message) => sum + message.content.length, 0)).toBeLessThanOrEqual(48_000);
    expect(view.at(-1)?.content).toContain("partial view");
    expect(view).toContain(history[2]);
  });

  it("removes old exchanges together while preserving the current task and latest action", () => {
    const history: ChatMessage[] = [
      { role: "system", content: "system" },
      { role: "user", content: "current task" },
      ...exchange("old.ts", "old evidence".repeat(1000)),
      ...exchange("recent.ts", "recent evidence"),
      ...exchange("newest.ts", "newest evidence"),
    ];
    const view = prepareContext(history, { contextWindowTokens: 8192 }).messages;
    expect(view).not.toContain(history[2]);
    expect(view).not.toContain(history[3]);
    expect(view).toContain(history[1]);
    for (let index = 0; index < view.length; index++) {
      if (view[index].content.startsWith("TOOL_RESULT:")) expect(view[index - 1].role).toBe("assistant");
    }
    expect(view.slice(-4)).toEqual(history.slice(-4));
  });

  it.each(["system", "user", "assistant"] as const)("refuses oversized %s content without silently cutting it", role => {
    const history: ChatMessage[] = [
      { role: "system", content: "system" },
      { role: "user", content: "current task" },
      ...exchange("file.ts", "actual result"),
    ];
    const index = role === "system" ? 0 : role === "user" ? 1 : 2;
    history[index].content = "x".repeat(8192);
    expect(() => prepareContext(history, { contextWindowTokens: 8192 })).toThrow(ContextBudgetError);
    expect(history[index].content).toHaveLength(8192);
  });

  it("counts every system message, including a repository map or project brief", () => {
    const history: ChatMessage[] = [
      { role: "system", content: "core instructions" },
      { role: "system", content: "project rules".repeat(1000) },
      { role: "user", content: "fix bug" },
    ];
    expect(() => prepareContext(history, { contextWindowTokens: 8192 })).toThrow(ContextBudgetError);
  });

  it.each(["Continue that fix.", "Summarize this work for a handoff."])("preserves recent evidence before a new task: %s", request => {
    const history: ChatMessage[] = [
      { role: "system", content: "core instructions" },
      { role: "user", content: "old request".repeat(1000) },
      ...exchange("old.ts", "old evidence".repeat(1000)),
      { role: "user", content: "fix math.ts" },
      ...exchange("math.ts", "actual recent code"),
      { role: "assistant", content: "I located the incorrect subtraction." },
      { role: "user", content: request },
    ];
    const view = prepareContext(history, { contextWindowTokens: 8192 }).messages;
    expect(view.slice(-5)).toEqual(history.slice(-5));
    expect(view.at(-1)).toBe(history.at(-1));
    expect(view).not.toContain(history[2]);
    expect(view).not.toContain(history[3]);
  });

  it("drops oversized historical prose without blocking a fitting new request", () => {
    const history: ChatMessage[] = [
      { role: "system", content: "Instructions" },
      { role: "user", content: "old task" },
      ...exchange("package.json", '{"name":"potluck"}'),
      { role: "assistant", content: "A".repeat(10_000) },
      { role: "user", content: "New task: read package.json" },
    ];
    const prepared = prepareContext(history, { contextWindowTokens: 4096 });
    expect(prepared.messages).not.toContain(history[4]);
    expect(prepared.messages).toContain(history[2]);
    expect(prepared.messages).toContain(history[3]);
    expect(prepared.messages.at(-1)).toBe(history[5]);
    expect(estimateContextTokens(prepared.messages) + prepared.maxOutputTokens).toBeLessThanOrEqual(4096);
  });

  it("drops an oversized historical action and its result together", () => {
    const history: ChatMessage[] = [
      { role: "system", content: "Instructions" },
      { role: "user", content: "old task" },
      { role: "assistant", content: "A".repeat(10_000) },
      { role: "user", content: "TOOL_RESULT: historical result" },
      { role: "user", content: "New task: read package.json" },
    ];
    const view = prepareContext(history, { contextWindowTokens: 4096 }).messages;
    expect(view).not.toContain(history[2]);
    expect(view).not.toContain(history[3]);
    expect(view.at(-1)).toBe(history[4]);
  });

  it("pins an explicitly identified task even when it begins with literal TOOL_RESULT text", () => {
    const history: ChatMessage[] = [
      { role: "system", content: "Instructions" },
      { role: "user", content: "old task".repeat(2000) },
      { role: "assistant", content: "old answer" },
      { role: "user", content: "TOOL_RESULT: This is quoted text. Explain why this output is wrong." },
      ...exchange("log.txt", "actual tool output".repeat(2000)),
    ];
    const prepared = prepareContext(history, { contextWindowTokens: 4096, currentTaskIndex: 3 });
    expect(prepared.messages).toContain(history[3]);
    expect(prepared.messages).not.toContain(history[1]);
    expect(prepared.messages.at(-1)?.content).toContain("Tool output truncated");
    expect(estimateContextTokens(prepared.messages) + prepared.maxOutputTokens).toBeLessThanOrEqual(4096);
  });

  it("reserves output allowance, caps an excessive override, and enforces the character override", () => {
    const history: ChatMessage[] = [
      { role: "system", content: "system" },
      { role: "user", content: "current task" },
      ...exchange("large.ts", "x".repeat(1000)),
    ];
    const prepared = prepareContext(history, { contextWindowTokens: 8192, maxOutputTokens: 50_000, charBudget: 700 });
    expect(prepared.maxOutputTokens).toBe(2048);
    expect(prepared.messages.reduce((sum, message) => sum + message.content.length, 0)).toBeLessThanOrEqual(700);
    expect(prepareContext(history, { contextWindowTokens: 8192, maxOutputTokens: 512 }).maxOutputTokens).toBe(512);
  });

  it.each([undefined, 0, -1, NaN, Infinity, 8192.5])("uses the 4K fallback for an unavailable or invalid runtime limit: %s", contextWindowTokens => {
    expect(prepareContext([{ role: "user", content: "hello" }], { contextWindowTokens }).usedFallback).toBe(true);
  });
});

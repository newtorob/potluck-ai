import { createServer, type Server } from "node:http";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { runTask, type AgentOptions } from "../src/agent.js";
import type { ChatMessage } from "../src/gateway.js";

interface Turn {
  content: string;
  finishReason?: string;
  metadata?: Record<string, unknown>;
  doneDelayMs?: number;
}

let cwd: string;
let server: Server;
let baseURL: string;
let turns: Turn[];
let requests: number;
let doneSent: number;
let earlyDisconnects: number;

function action(tool: string, args: Record<string, unknown>, closeFence = true): string {
  return "```action\n" + JSON.stringify({ tool, args }) + (closeFence ? "\n```" : "");
}

beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "potluck-truncated-generation-"));
  turns = [];
  requests = 0;
  doneSent = 0;
  earlyDisconnects = 0;
  server = createServer(async (req, res) => {
    if (req.method !== "POST" || req.url !== "/v1/chat/completions") {
      res.writeHead(404).end();
      return;
    }
    for await (const _ of req) { /* Drain the request before writing the stream. */ }
    const turn = turns[requests++] ?? { content: "Done.", finishReason: "stop" };
    let sentDone = false;
    res.once("close", () => { if (!sentDone) earlyDisconnects++; });
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const delta = (content: string) => JSON.stringify({ choices: [{ index: 0, delta: { content }, finish_reason: null }] });
    // Content arrives before the terminal metadata, including a complete-looking action.
    const middle = Math.floor(turn.content.length / 2);
    res.write(`data: ${delta(turn.content.slice(0, middle))}\n\n`);
    res.write(`data: ${delta(turn.content.slice(middle))}\n\n`);
    if (turn.finishReason !== undefined) {
      res.write(`data: ${JSON.stringify({ choices: [{ index: 0, delta: {}, finish_reason: turn.finishReason }],
        ...(turn.metadata ? { potluck: turn.metadata } : {}) })}\n\n`);
    }
    if (turn.doneDelayMs) await new Promise<void>(resolve => {
      const timer = setTimeout(resolve, turn.doneDelayMs);
      res.once("close", () => { clearTimeout(timer); resolve(); });
    });
    if (res.destroyed) return;
    sentDone = true;
    doneSent++;
    res.end("data: [DONE]\n\n");
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  baseURL = `http://127.0.0.1:${(server.address() as import("node:net").AddressInfo).port}/v1`;
});

afterEach(async () => {
  if (server?.listening) {
    await new Promise<void>(resolve => {
      server.close(() => resolve());
      server.closeAllConnections();
    });
  }
  if (cwd) await rm(cwd, { recursive: true, force: true });
});

function options(): AgentOptions {
  return {
    cfg: { baseURL, apiKey: "fixture-key", enabled: true }, model: "fixture", scope: "local", cwd,
    contextWindowTokens: 8192, maxSteps: 4,
    confirm: vi.fn(async () => true),
    callbacks: { onToken: vi.fn(), onTurnEnd: vi.fn(), onAction: vi.fn(), onToolResult: vi.fn(), onNotice: vi.fn() },
  };
}

describe("generation completion before coding actions", () => {
  it.each([true, false])("never approves or writes a length-limited JSON action (closing fence: %s)", async closeFence => {
    turns.push({ content: action("write_file", { path: "new.ts", content: "export const partial = true;\n" }, closeFence), finishReason: "length" });
    const opts = options();
    const history: ChatMessage[] = [];
    const outcome = await runTask(history, "Create the complete module.", opts).then(result => result, error => error);

    expect.soft(opts.confirm).not.toHaveBeenCalled();
    expect.soft(opts.callbacks.onAction).not.toHaveBeenCalled();
    expect.soft(await readFile(join(cwd, "new.ts"), "utf8").catch(() => undefined)).toBeUndefined();
    expect.soft(history.filter(message => message.role === "assistant")).toEqual([]);
    expect(outcome).toBeInstanceOf(Error);
    expect(outcome.message).toMatch(/limit|truncat|length/i);
  });

  it.each(["content_filter", "tool_calls", undefined])("does not execute text actions without a successful finish (%s)", async finishReason => {
    turns.push({ content: action("write_file", { path: "blocked.txt", content: "must not be written" }), finishReason });
    const opts = options();
    const outcome = await runTask([], "Write a file.", opts).then(result => result, error => error);

    expect.soft(opts.confirm).not.toHaveBeenCalled();
    expect.soft(opts.callbacks.onAction).not.toHaveBeenCalled();
    expect.soft(await readFile(join(cwd, "blocked.txt"), "utf8").catch(() => undefined)).toBeUndefined();
    expect(outcome).toBeInstanceOf(Error);
  });

  it("does not mark cut-off final prose complete or run final verification", async () => {
    turns.push({ content: "The repair is complete. The remaining details are", finishReason: "length" });
    const opts = options();
    opts.verifyCommand = "fixture-check";
    opts.runVerify = vi.fn(async () => ({ code: 0, output: "passed" }));
    const history: ChatMessage[] = [];
    const outcome = await runTask(history, "Explain the completed repair.", opts).then(result => result, error => error);

    expect.soft(opts.callbacks.onTurnEnd).not.toHaveBeenCalled();
    expect.soft(opts.runVerify).not.toHaveBeenCalled();
    expect.soft(history.filter(message => message.role === "assistant")).toEqual([]);
    expect(outcome).toBeInstanceOf(Error);
  });

  it("preserves an existing fully read file when its replacement is cut off", async () => {
    const original = "export const keep = 1;\nexport const alsoKeep = 2;\n";
    await writeFile(join(cwd, "existing.ts"), original);
    turns.push(
      { content: action("read_file", { path: "existing.ts" }), finishReason: "stop" },
      { content: action("write_file", { path: "existing.ts", content: "export const keep = 3;\n" }, false), finishReason: "length" },
    );
    const opts = options();
    const outcome = await runTask([], "Update the module while preserving its other exports.", opts).then(result => result, error => error);

    expect.soft(opts.confirm).not.toHaveBeenCalled();
    expect.soft(opts.callbacks.onAction).toHaveBeenCalledTimes(1);
    expect.soft(opts.callbacks.onAction).toHaveBeenCalledWith({ tool: "read_file", args: { path: "existing.ts" } });
    expect.soft(await readFile(join(cwd, "existing.ts"), "utf8")).toBe(original);
    expect(outcome).toBeInstanceOf(Error);
  });

  it("drains an unsuccessful generation through delayed DONE before refusing the action", async () => {
    turns.push({ content: action("write_file", { path: "delayed.txt", content: "partial" }),
      finishReason: "length", doneDelayMs: 100 });
    const opts = options();
    const outcome = await runTask([], "Write a complete file.", opts).then(result => result, error => error);

    expect(outcome).toBeInstanceOf(Error);
    expect(doneSent).toBe(1);
    expect(earlyDisconnects).toBe(0);
    expect(opts.confirm).not.toHaveBeenCalled();
    expect(opts.callbacks.onAction).not.toHaveBeenCalled();
    await expect(readFile(join(cwd, "delayed.txt"))).rejects.toMatchObject({ code: "ENOENT" });
  });

  it("rejects a legacy household stop inferred only from DONE", async () => {
    turns.push({ content: action("write_file", { path: "inferred.txt", content: "unconfirmed" }),
      finishReason: "stop", metadata: { finish_reason_inferred: true } });
    const opts = options();
    const outcome = await runTask([], "Write the complete result.", opts).then(result => result, error => error);

    expect.soft(opts.confirm).not.toHaveBeenCalled();
    expect.soft(opts.callbacks.onAction).not.toHaveBeenCalled();
    expect.soft(await readFile(join(cwd, "inferred.txt"), "utf8").catch(() => undefined)).toBeUndefined();
    expect(outcome).toBeInstanceOf(Error);
  });

  it.each([true, false])("executes an approved complete stop response and finishes normally (closing fence: %s)", async closeFence => {
    const content = "export const complete = true;\n";
    turns.push({ content: action("write_file", { path: "complete.ts", content }, closeFence), finishReason: "stop",
      metadata: { finish_reason: "stop", served_scope: "mine", output_tokens: 15, duration_ms: 50 } });
    const opts = options();
    expect(await runTask([], "Create the module.", opts)).toEqual({ status: "finished" });
    expect(opts.confirm).toHaveBeenCalledOnce();
    expect(opts.callbacks.onAction).toHaveBeenCalledWith({ tool: "write_file", args: { path: "complete.ts", content } });
    expect(await readFile(join(cwd, "complete.ts"), "utf8")).toBe(content);
    expect(requests).toBe(2);
  });
});

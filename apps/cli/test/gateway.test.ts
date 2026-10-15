import { afterEach, describe, expect, it, vi } from "vitest";

import { modelContextWindow, streamChat, type ChatMessage, type StreamOptions } from "../src/gateway.js";

const cfg = { baseURL: "http://127.0.0.1:8321/v1", apiKey: "k", enabled: true };
const messages: ChatMessage[] = [{ role: "user", content: "hi" }];

function sseResponse(frames: string[]): Response {
  const enc = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(enc.encode(f));
      controller.close();
    },
  });
  return new Response(stream, { status: 200 });
}

function jsonFrame(chunk: unknown): string {
  return `data: ${JSON.stringify(chunk)}\n\n`;
}

async function consumeStream(opts: Partial<StreamOptions> = {}): Promise<string> {
  let output = "";
  for await (const token of streamChat(cfg, messages, { model: "m", ...opts })) output += token;
  return output;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("modelContextWindow", () => {
  it("checks the selected model and scope without sending project content", async () => {
    const fetcher = vi.fn(async () => Response.json({ model: "my model", scope: "mine", context_window: 8192 }));
    vi.stubGlobal("fetch", fetcher);
    expect(await modelContextWindow(cfg, "my model", "mine")).toBe(8192);
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(new URL(url).searchParams.get("model")).toBe("my model");
    expect(new URL(url).searchParams.get("scope")).toBe("mine");
    expect(init.body).toBeUndefined();
    expect(init.redirect).toBe("error");
    expect(init.headers).toMatchObject({ authorization: "Bearer k" });
  });

  it("leaves old gateways and missing windows explicitly unknown", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Not found", { status: 404 })));
    expect(await modelContextWindow(cfg, "m", "local")).toBeUndefined();
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ model: "m", scope: "local", context_window: null })));
    expect(await modelContextWindow(cfg, "m", "local")).toBeUndefined();
  });

  it("rejects another model's capacity and propagates authentication failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ model: "other", scope: "local", context_window: 32768 })));
    await expect(modelContextWindow(cfg, "m", "local")).rejects.toThrow("different model or scope");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Unauthorized", { status: 401 })));
    await expect(modelContextWindow(cfg, "m", "local")).rejects.toThrow("401");
  });

  it("rejects non-owned scopes before contacting the gateway", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    await expect(modelContextWindow(cfg, "m", "pool")).rejects.toThrow("local or mine");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not mistake a missing household model for an old endpoint", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "model 'm' is not loaded here or on a reachable household peer" }, { status: 404 })));
    await expect(modelContextWindow(cfg, "m", "mine")).rejects.toThrow("not loaded");
  });

  it("forwards the exact project digest and refuses a different model before fetch", async () => {
    const locked = { ...cfg, modelLock: { schemaVersion: 1 as const, path: "potluck.lock", model: {
      id: "m", format: "gguf" as const, sha256: "a".repeat(64), sizeBytes: 100,
    } } };
    const fetcher = vi.fn(async () => Response.json({ model: "m", scope: "mine", context_window: 8192 }));
    vi.stubGlobal("fetch", fetcher);
    expect(await modelContextWindow(locked, "m", "mine")).toBe(8192);
    const url = (fetcher.mock.calls[0] as unknown as [string])[0];
    expect(new URL(url).searchParams.get("potluck_model_sha256")).toBe("a".repeat(64));
    fetcher.mockClear();
    await expect(modelContextWindow(locked, "other", "mine")).rejects.toThrow("pins m");
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("streamChat", () => {
  it("rejects a disconnected response even when its text contains a complete action", async () => {
    const text = '```action\n{"tool":"write_file","args":{"path":"x","content":"y"}}\n```';
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([
      `data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n\n`,
    ])));
    const consume = async () => {
      for await (const _ of streamChat(cfg, messages, { model: "m", requireDone: true })) { /* drain */ }
    };
    await expect(consume()).rejects.toThrow(/ended before completion/);
  });

  it("sends the X-Potluck-Scope header and yields content deltas", async () => {
    const seen: { url: string; headers: Record<string, string> }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        seen.push({ url, headers: init.headers as Record<string, string> });
        return sseResponse([
          'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
          'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
          "data: [DONE]\n\n",
        ]);
      }),
    );

    let out = "";
    for await (const tok of streamChat(cfg, messages, { model: "m", scope: "mine" })) {
      out += tok;
    }

    expect(out).toBe("hello");
    expect(seen).toHaveLength(1);
    expect(seen[0].headers["x-potluck-scope"]).toBe("mine");
    expect(seen[0].headers.authorization).toBe("Bearer k");
  });

  it("omits the scope header when no scope is given", async () => {
    const seen: Record<string, string>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        seen.push(init.headers as Record<string, string>);
        return sseResponse(["data: [DONE]\n\n"]);
      }),
    );

    for await (const _ of streamChat(cfg, messages, { model: "m" })) {
      // drain
    }

    expect(seen[0]["x-potluck-scope"]).toBeUndefined();
  });
});

describe("streamChat successful coding completion", () => {
  const content = '```action\n{"tool":"write_file","args":{"path":"x","content":"y"}}';
  const contentFrame = jsonFrame({ choices: [{ index: 0, delta: { content }, finish_reason: null }] });
  const doneFrame = "data: [DONE]\n\n";
  const terminal = (reason: unknown) => jsonFrame({ choices: [{ index: 0, delta: {}, finish_reason: reason }] });
  const strict = { requireSuccessfulFinish: true };

  it.each(["length", "content_filter", "tool_calls", "function_call", "error", "unknown"])(
    "rejects %s even when the text is a recoverable action and DONE arrives", async reason => {
      vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal(reason), doneFrame])));
      await expect(consumeStream(strict)).rejects.toThrow(reason === "length" ? "model hit its output limit" : `finish_reason: ${reason}`);
    },
  );

  it.each([undefined, null])("requires an explicit stop, not only DONE (%s)", async reason => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal(reason), doneFrame])));
    await expect(consumeStream(strict)).rejects.toThrow("missing finish_reason: stop");
  });

  it("requires DONE even when stop arrives", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal("stop")])));
    await expect(consumeStream(strict)).rejects.toThrow("ended before completion");
  });

  it("accepts reasoning deltas, a final content delta with stop, and trailing usage", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([
      jsonFrame({ choices: [{ index: 0, delta: { role: "assistant", content: null }, finish_reason: null }] }),
      jsonFrame({ choices: [{ index: 0, delta: { reasoning_content: "thinking" }, finish_reason: null }] }),
      contentFrame,
      jsonFrame({ choices: [{ index: 0, delta: { content: "\n```" }, finish_reason: "stop" }], potluck: { scope: "local", finish_reason: "stop" } }),
      jsonFrame({ choices: [], usage: { completion_tokens: 20 } }),
      doneFrame,
    ])));
    await expect(consumeStream(strict)).resolves.toBe(content + "\n```");
  });

  it("accepts a single unindexed choice with an explicit successful finish", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([
      jsonFrame({ choices: [{ delta: { content: "Complete." }, finish_reason: "stop" }] }), doneFrame,
    ])));
    await expect(consumeStream(strict)).resolves.toBe("Complete.");
  });

  it.each([true, false])("rejects an inferred stop with upgrade guidance, even on usage-only chunks (%s)", async usageOnly => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([
      contentFrame,
      ...(usageOnly ? [terminal("stop")] : []),
      jsonFrame({ choices: usageOnly ? [] : [{ index: 0, delta: {}, finish_reason: "stop" }], potluck: { finish_reason_inferred: true } }),
      doneFrame,
    ])));
    await expect(consumeStream(strict)).rejects.toThrow("Update Potluck on the serving machine and retry");
  });

  it.each(["stop", "length"])("drains past %s and honors a subsequent stream error", async reason => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([
      contentFrame, terminal(reason), jsonFrame({ error: { message: "late inference failure" } }), doneFrame,
    ])));
    await expect(consumeStream(strict)).rejects.toThrow("late inference failure");
  });

  it("does not let a later stop erase an earlier unsuccessful finish", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal("length"), terminal("stop"), doneFrame])));
    await expect(consumeStream(strict)).rejects.toThrow("model hit its output limit");
  });

  it.each([
    { choices: [{ index: 0, delta: { content: "extra" }, finish_reason: null }] },
    { choices: [{ index: 0, delta: { reasoning_content: "extra" }, finish_reason: null }] },
    { choices: [{ index: 0, delta: {}, finish_reason: "stop" }] },
    { choices: [{ index: 0, delta: {}, finish_reason: "length" }] },
  ])("rejects output or another terminal after stop: %j", async afterStop => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal("stop"), jsonFrame(afterStop), doneFrame])));
    await expect(consumeStream(strict)).rejects.toThrow("after the generation had finished");
  });

  it.each([
    null,
    [],
    {},
    { choices: {} },
    { choices: [null] },
    { choices: [{ index: 1, delta: {}, finish_reason: "stop" }] },
    { choices: [{ index: "0", delta: {}, finish_reason: "stop" }] },
    { choices: [{ index: 0, delta: {}, finish_reason: "stop" }, { index: 1, delta: {}, finish_reason: "stop" }] },
    { choices: [{ index: 0, delta: "bad", finish_reason: "stop" }] },
    { choices: [{ index: 0, delta: { content: {} }, finish_reason: "stop" }] },
    { choices: [{ index: 0, delta: { reasoning_content: [] }, finish_reason: "stop" }] },
    { choices: [{ index: 0, delta: {}, finish_reason: false }] },
    { choices: [{ index: 0, delta: {}, finish_reason: "" }] },
  ])("rejects malformed completion state: %j", async malformed => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, jsonFrame(malformed), doneFrame])));
    await expect(consumeStream(strict)).rejects.toThrow(/invalid/);
  });

  it("rejects malformed JSON after an otherwise valid stop", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal("stop"), "data: {\n\n", doneFrame])));
    await expect(consumeStream(strict)).rejects.toThrow("invalid stream frame");
  });

  it.each(["tool_calls", "function_call"])("rejects native %s deltas even if stop follows", async key => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([
      contentFrame, jsonFrame({ choices: [{ index: 0, delta: { [key]: {} }, finish_reason: null }] }), terminal("stop"), doneFrame,
    ])));
    await expect(consumeStream(strict)).rejects.toThrow("native tool calls");
  });

  it.each([undefined, "length", "tool_calls"])("preserves requireDone-only callers' finish handling (%s)", async reason => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal(reason), doneFrame])));
    await expect(consumeStream({ requireDone: true })).resolves.toBe(content);
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";

import { streamChat, type ChatMessage } from "../src/gateway.js";

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

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("streamChat", () => {
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

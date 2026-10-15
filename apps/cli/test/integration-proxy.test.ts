import { createServer } from "node:http";
import { afterEach, expect, it } from "vitest";
import { startIntegrationProxy } from "../src/integration-proxy.js";
import { parseArgs } from "../src/args.js";
const hash = "a".repeat(64);
const cleanup: Array<() => Promise<void>> = [];
const budget = { contextWindowTokens: 8192, maxInputTokens: 5632, maxOutputTokens: 2048 };
afterEach(async () => { for (const close of cleanup.splice(0).reverse()) await close(); });
async function fixture(acknowledge = true, limits?: typeof budget) {
  const calls: Array<{ headers: import("node:http").IncomingHttpHeaders; body: any }> = [];
  const contextCalls: Array<{ url: URL; headers: import("node:http").IncomingHttpHeaders }> = [];
  const context = { window: 8192 as number | null, status: 200, model: "test", scope: "local" };
  const server = createServer(async (req, res) => {
    const url = new URL(req.url!, "http://localhost");
    if (url.pathname === "/v1/potluck/context") {
      contextCalls.push({ url, headers: req.headers });
      res.writeHead(context.status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(context.status === 200
        ? { model: context.model, scope: context.scope, context_window: context.window }
        : { detail: "private-upstream-error-and-key" })); return;
    }
    let body = ""; for await (const part of req) body += part;
    calls.push({ headers: req.headers, body: JSON.parse(body) });
    res.setHeader("Content-Type", "text/event-stream");
    if (acknowledge) { res.setHeader("X-Potluck-Model-Lock", "1"); res.setHeader("X-Potluck-Model-SHA256", hash); }
    res.end('data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n');
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  cleanup.push(() => new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); }));
  const port = (server.address() as import("node:net").AddressInfo).port;
  const proxy = await startIntegrationProxy({ baseURL: `http://127.0.0.1:${port}/v1`, apiKey: "actual-gateway-secret", enabled: true,
    modelLock: { path: "/project/potluck.lock", schemaVersion: 1, model: { id: "test", format: "gguf", sizeBytes: 10, sha256: hash } } }, "test", "local", limits);
  cleanup.push(proxy.close);
  const post = (body: unknown, key = proxy.key) => fetch(proxy.baseURL + "/chat/completions", {
    method: "POST", headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json", "X-Potluck-Scope": "pool" }, body: JSON.stringify(body),
  });
  return { proxy, calls, post, context, contextCalls };
}

it("rechecks pinned context for each request and applies an output cap when the client omits it", async () => {
  const { post, calls, contextCalls, context } = await fixture(true, budget);
  const body = { model: "test", messages: [{ role: "user", content: "hi" }] };
  expect((await post(body)).status).toBe(200);
  context.window = 32768;
  expect((await post(body)).status).toBe(200);
  expect(calls).toHaveLength(2); expect(contextCalls).toHaveLength(2);
  for (const call of contextCalls) {
    expect(Object.fromEntries(call.url.searchParams)).toEqual({ model: "test", scope: "local", potluck_model_sha256: hash });
    expect(call.headers.authorization).toBe("Bearer actual-gateway-secret");
  }
  expect(calls.map(call => call.body.max_tokens)).toEqual([2048, 2048]);
});

it.each([
  [{ max_tokens: 99999 }, 2048],
  [{ max_completion_tokens: 99999 }, 2048],
  [{ max_tokens: 128, max_completion_tokens: 4096 }, 128],
  [{ max_tokens: 4096, max_completion_tokens: 64 }, 64],
])("caps both output limit spellings without expanding smaller limits: %j", async (limits, expected) => {
  const { post, calls } = await fixture(true, budget);
  expect((await post({ model: "test", messages: [{ role: "user", content: "hi" }], ...limits })).status).toBe(200);
  expect(calls[0].body.max_tokens).toBe(expected);
  expect(calls[0].body.max_completion_tokens).toBeUndefined();
});

it.each([0, -1, 1.5, "200", null, Number.MAX_SAFE_INTEGER + 1])("refuses invalid output limits: %j", async value => {
  const { post, calls, contextCalls } = await fixture(true, budget);
  for (const field of ["max_tokens", "max_completion_tokens"]) {
    expect((await post({ model: "test", messages: [], [field]: value })).status).toBe(400);
  }
  expect(calls).toHaveLength(0); expect(contextCalls).toHaveLength(0);
});

it("preserves messages at the budget boundary and rejects overflow before contacting the model", async () => {
  const { post, calls, contextCalls } = await fixture(true, budget);
  const content = "a".repeat(budget.maxInputTokens - 128 - 16);
  expect((await post({ model: "test", messages: [{ role: "user", content }] })).status).toBe(200);
  expect(calls[0].body.messages[0].content).toBe(content);
  const rejected = await post({ model: "test", messages: [{ role: "user", content: content + "a" }] });
  expect(rejected.status).toBe(400);
  expect(await rejected.json()).toMatchObject({ error: { type: "invalid_request_error", message: expect.stringMatching(/fewer or smaller files/) } });
  expect(calls).toHaveLength(1); expect(contextCalls).toHaveLength(1);
});

it("counts UTF-8 bytes across every message, not characters or just the last turn", async () => {
  const { post, calls } = await fixture(true, budget);
  const response = await post({ model: "test", messages: [
    { role: "system", content: "a".repeat(2000) },
    { role: "assistant", content: "🙂".repeat(1000) },
    { role: "user", content: "go" },
  ] });
  expect(response.status).toBe(400); expect(calls).toHaveLength(0);
});

it("makes the request-size ceiling nonretryable for Aider too", async () => {
  const { post, calls, contextCalls } = await fixture(true, budget);
  const result = await post({ model: "test", messages: [{ role: "user", content: "a".repeat(8 * 1024 * 1024) }] });
  expect(result.status).toBe(400);
  expect(await result.json()).toMatchObject({ error: { type: "invalid_request_error", message: expect.stringContaining("8 MiB") } });
  expect(calls).toHaveLength(0); expect(contextCalls).toHaveLength(0);
});

it.each([null, 4096])("does not send project content when loaded context becomes %j", async window => {
  const { post, calls, context } = await fixture(true, budget);
  context.window = window;
  const result = await post({ model: "test", messages: [{ role: "user", content: "private-file" }] });
  expect(result.status).toBe(400); expect(await result.json()).toMatchObject({ error: {
    type: "invalid_request_error", message: expect.stringContaining("Restart potluck launch"),
  } });
  expect(calls).toHaveLength(0);
});

it.each([401, 404, 409, 503])("keeps context lookup failure %s safe and does not send project content", async status => {
  const { post, calls, context } = await fixture(true, budget);
  context.status = status;
  const result = await post({ model: "test", messages: [{ role: "user", content: "private-file" }] });
  expect(result.status).toBe(400);
  const error = await result.text();
  expect(error).toContain("potluck doctor"); expect(error).not.toContain("private-upstream-error-and-key");
  expect(calls).toHaveLength(0);
});

it.each(["model", "scope"])("refuses context for a different %s", async field => {
  const { post, calls, context } = await fixture(true, budget);
  context[field as "model" | "scope"] = "other";
  expect((await post({ model: "test", messages: [] })).status).toBe(400);
  expect(calls).toHaveLength(0);
});

it.each([
  { messages: [null] },
  { messages: [{ role: "tool", content: "hidden" }] },
  { messages: [{ role: "user", content: [{ type: "text", text: "hidden" }] }] },
  { messages: [{ role: "user", content: "hi", name: "hidden" }] },
  { messages: [], response_format: { type: "json_schema", json_schema: {} } },
  { messages: [], tool_choice: "auto" },
])("refuses inputs the plain-text budget cannot account for: %j", async body => {
  const { post, calls, contextCalls } = await fixture(true, budget);
  expect((await post({ model: "test", ...body })).status).toBe(400);
  expect(calls).toHaveLength(0); expect(contextCalls).toHaveLength(0);
});
it("enforces launch scope/digest for an unaware external client and keeps the real key behind the bridge", async () => {
  const { proxy, calls, post } = await fixture();
  expect(proxy.key).not.toBe("actual-gateway-secret");
  const response = await post({ model: "test", messages: [{ role: "user", content: "hi" }], stream: true });
  expect(response.status).toBe(200); expect(await response.text()).toContain("hello");
  expect(calls[0].headers["x-potluck-scope"]).toBe("local");
  expect(calls[0].headers.authorization).toBe("Bearer actual-gateway-secret");
  expect(calls[0].body.potluck_model_sha256).toBe(hash);
});
it("does not forward wrong credentials, model switches, bad JSON or native tool requests", async () => {
  const { calls, post } = await fixture();
  expect((await post({}, "wrong")).status).toBe(401);
  expect((await post({ model: "other", messages: [] })).status).toBe(409);
  expect((await post({ model: "test", messages: [], potluck_model_sha256: "b".repeat(64) })).status).toBe(409);
  expect((await post({ model: "test", messages: [], tools: [{}] })).status).toBe(400);
  expect((await post(null)).status).toBe(400); expect(calls).toHaveLength(0);
});
it("rejects an old upstream's unacknowledged response before forwarding any tokens", async () => {
  const { post } = await fixture(false);
  const result = await post({ model: "test", messages: [], stream: true });
  expect(result.status).toBe(502); expect(await result.text()).not.toContain("hello");
});
it("exposes only the selected model and no management endpoints", async () => {
  const { proxy, calls } = await fixture();
  const options = { headers: { Authorization: `Bearer ${proxy.key}` } };
  const result = await fetch(proxy.baseURL + "/models", options);
  expect((await result.json()).data.map((m: any) => m.id)).toEqual(["test"]);
  expect((await fetch(proxy.baseURL + "/settings", options)).status).toBe(404);
  expect(calls).toHaveLength(0);
});
it("validates launcher arguments without consuming coding-agent options", () => {
  expect(parseArgs(["launch", "aider", "--model", "test", "--scope", "local", "--message", "hi"]).action).toBe("aider");
  for (const args of [["launch", "unknown"], ["launch", "aider", "--json"], ["launch", "aider", "--scope", "pool"], ["--message", "hi"]]) {
    expect(() => parseArgs(args)).toThrow();
  }
});

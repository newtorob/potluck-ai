import { createServer } from "node:http";
import { afterEach, expect, it } from "vitest";
import { startIntegrationProxy } from "../src/integration-proxy.js";
import { parseArgs } from "../src/args.js";
const hash = "a".repeat(64);
const cleanup: Array<() => Promise<void>> = [];
afterEach(async () => { for (const close of cleanup.splice(0).reverse()) await close(); });
async function fixture(acknowledge = true) {
  const calls: Array<{ headers: import("node:http").IncomingHttpHeaders; body: any }> = [];
  const server = createServer(async (req, res) => {
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
    modelLock: { path: "/project/potluck.lock", schemaVersion: 1, model: { id: "test", format: "gguf", sizeBytes: 10, sha256: hash } } }, "test", "local");
  cleanup.push(proxy.close);
  const post = (body: unknown, key = proxy.key) => fetch(proxy.baseURL + "/chat/completions", {
    method: "POST", headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json", "X-Potluck-Scope": "pool" }, body: JSON.stringify(body),
  });
  return { proxy, calls, post };
}
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

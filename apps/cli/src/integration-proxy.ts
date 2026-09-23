/** Per-launch loopback bridge: applies Potluck scope/pin even to unaware clients. */
import { createServer } from "node:http";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import type { GatewayConfig, Scope } from "./config.js";

export async function startIntegrationProxy(cfg: GatewayConfig, model: string, scope: Scope) {
  const key = randomBytes(32).toString("hex");
  const active = new Set<AbortController>();
  const server = createServer(async (req, res) => {
    const reply = (status: number, message: string) => {
      if (res.headersSent) { res.destroy(); return; }
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: { message, type: "potluck_integration_error" } }));
    };
    const auth = Buffer.from(req.headers.authorization || "");
    const expected = Buffer.from(`Bearer ${key}`);
    if (auth.length !== expected.length || !timingSafeEqual(auth, expected)) { reply(401, "Invalid integration credential."); return; }
    if (req.method === "GET" && req.url === "/v1/models") {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ object: "list", data: [{ id: model, object: "model", owned_by: "potluck" }] })); return;
    }
    if (req.method !== "POST" || req.url !== "/v1/chat/completions") { reply(404, "This integration supports chat completions only."); return; }
    const abort = new AbortController(); active.add(abort);
    const timer = setTimeout(() => abort.abort(), 5 * 60 * 1000);
    res.on("close", () => abort.abort());
    try {
      let size = 0; const parts: Buffer[] = [];
      for await (const part of req) {
        size += part.length;
        if (size > 8 * 1024 * 1024) { reply(413, "Integration request exceeds 8 MiB."); return; }
        parts.push(part);
      }
      let body: Record<string, unknown>;
      try { body = JSON.parse(Buffer.concat(parts).toString("utf8")); }
      catch { reply(400, "Invalid JSON request."); return; }
      if (!body || Array.isArray(body) || typeof body !== "object") { reply(400, "Invalid request."); return; }
      if (body.model !== model) { reply(409, "This launch is fixed to its selected model. Restart potluck launch to change models."); return; }
      if (body.tools || body.functions || !Array.isArray(body.messages)) { reply(400, "This integration uses plain chat messages; native tool calls are not supported."); return; }
      const digest = cfg.modelLock?.model.sha256;
      if (body.potluck_model_sha256 && body.potluck_model_sha256 !== digest) { reply(409, "Request conflicts with the project model lock."); return; }
      if (digest) body.potluck_model_sha256 = digest;
      const upstream = await fetch(`${cfg.baseURL}/chat/completions`, {
        method: "POST", redirect: "error", signal: abort.signal,
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${cfg.apiKey}`, "X-Potluck-Scope": scope },
        body: JSON.stringify(body),
      });
      if (upstream.ok && digest && (upstream.headers.get("x-potluck-model-lock") !== "1"
          || upstream.headers.get("x-potluck-model-sha256") !== digest)) {
        await upstream.body?.cancel(); reply(502, "Runtime did not acknowledge the project lock. Upgrade the runtime."); return;
      }
      res.statusCode = upstream.status;
      res.setHeader("Content-Type", upstream.headers.get("content-type") || "application/json");
      if (upstream.body) await pipeline(Readable.fromWeb(upstream.body as import("node:stream/web").ReadableStream<Uint8Array>), res);
      else res.end();
    } catch {
      reply(502, "Potluck inference was interrupted or unavailable. Check potluck doctor.");
    } finally { clearTimeout(timer); active.delete(abort); }
  });
  server.requestTimeout = 30000;
  server.headersTimeout = 10000;
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  const port = (server.address() as import("node:net").AddressInfo).port;
  return { baseURL: `http://127.0.0.1:${port}/v1`, key, close: async () => {
    for (const request of active) request.abort();
    await new Promise<void>((resolve, reject) => { server.close(error => error ? reject(error) : resolve()); server.closeAllConnections(); });
  } };
}

import { createServer, type Server } from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { meshSocketPath, requestJSON, runtimeURL, safeURL } from "../src/control.js";

const servers: Server[] = [];
const dirs: string[] = [];
async function server(handler: Parameters<typeof createServer>[0]): Promise<string> {
  const srv = createServer(handler);
  servers.push(srv);
  await new Promise<void>(resolve => srv.listen(0, "127.0.0.1", resolve));
  return `http://127.0.0.1:${(srv.address() as { port: number }).port}`;
}
afterEach(async () => {
  for (const s of servers.splice(0)) {
    s.closeAllConnections(); await new Promise<void>(resolve => s.close(() => resolve()));
  }
  for (const d of dirs.splice(0)) await rm(d, { recursive: true, force: true });
});

describe("control transports", () => {
  it("sends only the explicitly supplied token", async () => {
    const headers: (string | undefined)[] = [];
    const url = await server((req, res) => { headers.push(req.headers.authorization); res.end('{"status":"ok"}'); });
    await requestJSON({ url, timeout: 500 });
    await requestJSON({ url, token: "control-key", timeout: 500 });
    expect(headers).toEqual([undefined, "Bearer control-key"]);
  });
  it("does not follow redirects or expose response bodies", async () => {
    const url = await server((_req, res) => {
      res.writeHead(302, { Location: "http://example.invalid?token=secret" }); res.end("secret");
    });
    await expect(requestJSON({ url, token: "key", timeout: 500 }))
      .rejects.toMatchObject({ code: "http_error", message: "Service returned HTTP 302." });
  });
  it("only displays allowlisted local hints for error codes", async () => {
    let code = "split_busy";
    const url = await server((_req, res) => {
      res.writeHead(409, { "X-Potluck-Error": code }); res.end("private model path or token");
    });
    const options = { url, timeout: 500, errorHints: { split_busy: "Stop the current split." } };
    await expect(requestJSON(options)).rejects.toMatchObject({ message: "Stop the current split." });
    for (const value of ["unrecognized-private-token", "toString", "__proto__"]) {
      code = value;
      await expect(requestJSON(options)).rejects.toMatchObject({ message: "Service returned HTTP 409." });
    }
  });
  it("bounds stalled and oversized replies", async () => {
    const stalled = await server(() => {});
    await expect(requestJSON({ url: stalled, timeout: 30 })).rejects.toMatchObject({ code: "timeout" });
    const large = await server((_req, res) => res.end(" ".repeat(1024 * 1024 + 1)));
    await expect(requestJSON({ url: large, timeout: 500 })).rejects.toMatchObject({ code: "invalid_response" });
  });
  it("rejects incomplete JSON and auth failures", async () => {
    const invalid = await server((_req, res) => res.end("{broken"));
    await expect(requestJSON({ url: invalid, timeout: 500 })).rejects.toMatchObject({ code: "invalid_response" });
    const denied = await server((_req, res) => { res.writeHead(403); res.end("secret detail"); });
    await expect(requestJSON({ url: denied, timeout: 500 })).rejects.toMatchObject({ code: "unauthorized" });
  });
  it("accepts an explicitly allowed empty unpair response", async () => {
    const url = await server((_req, res) => { res.writeHead(204); res.end(); });
    await expect(requestJSON({ url, timeout: 500, allowEmpty: true })).resolves.toEqual({});
    await expect(requestJSON({ url, timeout: 500 })).rejects.toMatchObject({ code: "invalid_response" });
  });
  it.skipIf(process.platform === "win32")("uses HTTP over the real mesh Unix socket", async () => {
    const dir = await mkdtemp(join(tmpdir(), "potluck-ipc-")); dirs.push(dir);
    const socket = join(dir, "mesh.sock");
    const srv = createServer((req, res) => {
      expect(req.url).toBe("/status"); res.end('{"mesh_status":"running"}');
    }); servers.push(srv);
    await new Promise<void>(resolve => srv.listen(socket, resolve));
    await expect(requestJSON({ socketPath: socket, path: "/status", timeout: 500 }))
      .resolves.toMatchObject({ mesh_status: "running" });
  });
  it("matches platform paths and rejects non-loopback management URLs", () => {
    expect(meshSocketPath("darwin", {})).toBe("/tmp/potluck-mesh.sock");
    expect(meshSocketPath("win32", {})).toBe("\\\\.\\pipe\\potluck-mesh");
    expect(meshSocketPath("linux", { POTLUCK_LOCAL_API_SOCKET: "/custom.sock" })).toBe("/custom.sock");
    expect(runtimeURL({ POTLUCK_PYTHON_PORT: "9321" })).toBe("http://127.0.0.1:9321");
    for (const value of ["http://evil.test", "http://localhost", "http://127.0.0.1@evil.test", "http://127.0.0.1/path"]) {
      expect(() => runtimeURL({ POTLUCK_SIDECAR_URL: value })).toThrow();
    }
    expect(safeURL("https://user:secret@example.test/v1?token=secret#secret")).toBe("https://example.test/v1");
  });
});

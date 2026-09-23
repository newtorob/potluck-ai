import { createServer, type Server } from "node:http";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { diagnose, formatDiagnostics } from "../src/diagnostics.js";

let dir: string;
const servers: Server[] = [];
let health: unknown;
let modelList: unknown;
let gatewayStatus: number;
let mesh: unknown;
let peers: unknown;
let base: string;

beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "potluck-diag-"));
  health = { status: "ok", version: "0.1.0", active_model_id: "coder", secret: "never-echo" };
  modelList = { data: [{ id: "coder" }] };
  gatewayStatus = 200;
  mesh = { mesh_status: "running", node_id: "own-node", mesh_ip: "100.64.0.1",
    coordinator_connection: "connected", peers_online_count: 2, wg_private_key: "never-echo" };
  peers = { peers: [{ reachable: true }, { reachable: true }] };
  const srv = createServer((req, res) => {
    if (req.url === "/v1/models") {
      expect(req.headers.authorization).toBe("Bearer test-gateway-key");
      res.writeHead(gatewayStatus); res.end(JSON.stringify(modelList));
    } else { expect(req.headers.authorization).toBeUndefined(); res.end(JSON.stringify(health)); }
  }); servers.push(srv);
  await new Promise<void>(resolve => srv.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(srv.address() as { port: number }).port}`;
  vi.stubEnv("POTLUCK_DATA_DIR", dir);
  vi.stubEnv("POTLUCK_SIDECAR_URL", base);
  vi.stubEnv("POTLUCK_GATEWAY_URL", `${base}/v1`);
  vi.stubEnv("POTLUCK_GATEWAY_KEY", "test-gateway-key");
  vi.stubEnv("POTLUCK_LOCAL_API_SOCKET", join(dir, "absent.sock"));
});
afterEach(async () => {
  vi.unstubAllEnvs();
  for (const s of servers.splice(0)) {
    s.closeAllConnections(); await new Promise<void>(resolve => s.close(() => resolve()));
  }
  await rm(dir, { recursive: true, force: true });
});

describe("diagnostics", () => {
  it.each(["loading", "failed"])("explains model restoration state: %s", async status => {
    health = { status: "ok", active_model_id: null, model_restore: { status, model_id: "coder", error: "never-echo" } };
    const result = await diagnose({ scope: "local", timeout: 500 });
    expect(result.ready).toBe(false);
    expect(result.checks.find(c => c.id === "model_restore")?.message).toContain(status === "loading" ? "loading" : "could not be restored");
    expect(JSON.stringify(result)).not.toContain("never-echo");
  });
  it("works locally without the app or mesh and never echoes secrets", async () => {
    const result = await diagnose({ scope: "local", timeout: 500 });
    expect(result.ready).toBe(true);
    expect(result.mesh.reachable).toBe(false);
    expect(JSON.stringify(result)).not.toContain("test-gateway-key");
    expect(JSON.stringify(result)).not.toContain("never-echo");
    expect(formatDiagnostics(result, true)).toContain("scope: local");
  });
  it.skipIf(process.platform === "win32")("reports a connected household through IPC", async () => {
    const socket = join(dir, "mesh.sock");
    const srv = createServer((req, res) => res.end(JSON.stringify(req.url === "/peers" ? peers : mesh))); servers.push(srv);
    await new Promise<void>(resolve => srv.listen(socket, resolve));
    vi.stubEnv("POTLUCK_LOCAL_API_SOCKET", socket);
    const result = await diagnose({ scope: "mine", timeout: 500 });
    expect(result.ready).toBe(true);
    expect(result.mesh.peersOnline).toBe(2);
    expect(JSON.stringify(result)).not.toContain("never-echo");
    // The daemon's historical online count is a roster count, not tunnel health.
    health = { status: "ok", active_model_id: null };
    peers = { peers: [{ reachable: false }, { reachable: false }] };
    const connecting = await diagnose({ scope: "mine", timeout: 500 });
    expect(connecting.mesh).toMatchObject({ peersKnown: 2, peersOnline: 0, connected: true });
    expect(connecting.ready).toBe(false);
    expect(connecting.checks.find(c => c.id === "household_peers")?.state).toBe("error");
    health = { status: "ok", active_model_id: "coder" };
    expect((await diagnose({ scope: "mine", timeout: 500 })).ready).toBe(true);
    mesh = { mesh_status: "running", node_id: "node", mesh_ip: "100.64.0.1", coordinator_connection: "disconnected" };
    expect((await diagnose({ scope: "mine", timeout: 500 })).ready).toBe(false);
  });
  it("distinguishes gateway authentication from runtime liveness", async () => {
    gatewayStatus = 401;
    const result = await diagnose({ scope: "local", timeout: 500 });
    expect(result.runtime.reachable).toBe(true);
    expect(result.gateway.reachable).toBe(false);
    expect(result.checks.find(c => c.id === "gateway")?.message).toContain("unauthorized");
  });
  it("treats malformed liveness and empty models as failures", async () => {
    health = { status: "not-potluck" }; modelList = { data: [] };
    const result = await diagnose({ scope: "local", timeout: 500 });
    expect(result.ready).toBe(false);
    expect(result.runtime.reachable).toBe(false);
    expect(result.checks.find(c => c.id === "models")?.state).toBe("error");
  });
  it("reads prefs from the same isolated data directory as the sidecar", async () => {
    await writeFile(join(dir, "cli.json"), JSON.stringify({ scope: "local" }));
    expect((await diagnose({ timeout: 500 })).scope).toBe("local");
  });
});

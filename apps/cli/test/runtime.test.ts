import { spawn } from "node:child_process";
import { createServer, type Server } from "node:http";
import { mkdtemp, mkdir, writeFile, chmod, rm, stat, symlink } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readRuntimeRecord, runtimeRequest, startRuntime, stopRuntime, type RuntimeRecord } from "../src/runtime.js";

const require = createRequire(import.meta.url);
const { discoverRuntime } = require("../../desktop/electron/runtime-control.js");
const pythonRoot = resolve("../desktop/python");
const python = join(pythonRoot, ".venv/bin/python");
const token = "a".repeat(64);
const instanceId = "00000000-0000-0000-0000-000000000001";
let dir: string;
const servers: Server[] = [];
const spawned = new Set<number>();

async function server(handler: Parameters<typeof createServer>[0]): Promise<string> {
  const srv = createServer(handler); servers.push(srv);
  await new Promise<void>(resolve => srv.listen(0, "127.0.0.1", resolve));
  return `http://127.0.0.1:${(srv.address() as { port: number }).port}`;
}
async function record(url: string, owner: RuntimeRecord["owner"] = "cli"): Promise<RuntimeRecord> {
  const value: RuntimeRecord = { schemaVersion: 1, instanceId, token, url, owner, pid: 100, version: "0.1.0" };
  await mkdir(join(dir, "run"), { recursive: true, mode: 0o700 });
  await writeFile(join(dir, "run/runtime.json"), JSON.stringify(value), { mode: 0o600 });
  return value;
}
beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "potluck-runtime-"));
  vi.stubEnv("POTLUCK_DATA_DIR", dir);
});
afterEach(async () => {
  for (const pid of spawned) { try { process.kill(pid, "SIGTERM"); } catch {} }
  spawned.clear();
  for (const srv of servers.splice(0)) {
    srv.closeAllConnections(); await new Promise<void>(resolve => srv.close(() => resolve()));
  }
  vi.unstubAllEnvs();
  await rm(dir, { recursive: true, force: true });
});

describe.skipIf(process.platform === "win32")("shared runtime", () => {
  it("requires private file/directory permissions in both clients", async () => {
    const saved = await record("http://127.0.0.1:8321");
    expect(await readRuntimeRecord()).toEqual(saved);
    await chmod(join(dir, "run/runtime.json"), 0o644);
    await expect(readRuntimeRecord()).rejects.toThrow("private regular file");
    await expect(discoverRuntime({ dataDir: dir })).rejects.toThrow("Cannot read private");
    await chmod(join(dir, "run/runtime.json"), 0o600);
    // A loose directory comes from an older Potluck: both clients tighten it
    // and treat any record inside as untrusted.
    await chmod(join(dir, "run"), 0o755);
    expect(await readRuntimeRecord()).toBeNull();
    expect((await stat(join(dir, "run"))).mode & 0o777).toBe(0o700);
    await chmod(join(dir, "run"), 0o755);
    expect(await discoverRuntime({ dataDir: dir })).toBeNull();
    expect((await stat(join(dir, "run"))).mode & 0o777).toBe(0o700);
  });
  it("rejects symlinked records and non-loopback endpoints without making requests", async () => {
    await record("https://remote.invalid");
    await expect(readRuntimeRecord()).rejects.toThrow();
    await expect(discoverRuntime({ dataDir: dir })).rejects.toThrow("Incompatible");
    await rm(join(dir, "run/runtime.json"));
    await writeFile(join(dir, "secret"), "{}", { mode: 0o600 });
    await symlink(join(dir, "secret"), join(dir, "run/runtime.json"));
    await expect(readRuntimeRecord()).rejects.toThrow();
    await expect(discoverRuntime({ dataDir: dir })).rejects.toThrow();
  });
  it("shares authenticated control across CLI and Electron without returning tokens", async () => {
    let saved: RuntimeRecord;
    const requests: string[] = [];
    const url = await server((req, res) => {
      expect(req.headers.authorization).toBe(`Bearer ${token}`);
      requests.push(`${req.method} ${req.url}`);
      const { token: _token, ...info } = saved;
      res.end(JSON.stringify(req.url === "/control" ? info : { updated: true }));
    });
    saved = await record(url);
    expect(await startRuntime(500)).toMatchObject({ started: false, state: "running", pid: 100 });
    expect(JSON.stringify(await startRuntime(500))).not.toContain(token);
    expect(await discoverRuntime({ dataDir: dir, port: Number(new URL(url).port) })).toEqual(saved);
    expect(await runtimeRequest("/settings", { timeout: 500, method: "PATCH", body: { active_model_id: "test" } })).toEqual({ updated: true });
    expect(requests).toContain("PATCH /settings");
  });
  it("rejects identity mismatch and does not stop a desktop-owned runtime", async () => {
    let saved: RuntimeRecord;
    let mismatch = false;
    const url = await server((req, res) => {
      expect(req.url).toBe("/control");
      res.end(JSON.stringify({ ...saved, instanceId: mismatch ? "changed" : saved.instanceId }));
    });
    saved = await record(url, "desktop");
    await expect(stopRuntime(500)).rejects.toThrow("owned by desktop");
    mismatch = true;
    await expect(startRuntime(500)).rejects.toThrow("identity changed");
    await expect(discoverRuntime({ dataDir: dir, port: Number(new URL(url).port) })).rejects.toThrow("identity changed");
  });
  it.each(["removed", "replaced"])("waits for process exit after discovery is %s", async (change) => {
    const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"]);
    spawned.add(child.pid!);
    let saved: RuntimeRecord;
    const url = await server(async (req, res) => {
      if (req.url === "/shutdown") {
        if (change === "removed") await rm(join(dir, "run/runtime.json"));
        else await writeFile(join(dir, "run/runtime.json"), JSON.stringify({ ...saved, instanceId: "00000000-0000-0000-0000-000000000002" }), { mode: 0o600 });
        res.end("{}");
      } else res.end(JSON.stringify(saved));
    });
    saved = { ...await record(url), pid: child.pid! };
    await writeFile(join(dir, "run/runtime.json"), JSON.stringify(saved), { mode: 0o600 });
    await expect(stopRuntime(250)).rejects.toThrow("still shutting down");
    child.kill();
    await new Promise<void>(resolve => child.once("exit", () => resolve()));
  });
  it("does not report a live process stopped when its API is unreachable", async () => {
    const saved = { ...await record("http://127.0.0.1:1"), pid: process.pid };
    await writeFile(join(dir, "run/runtime.json"), JSON.stringify(saved), { mode: 0o600 });
    await expect(stopRuntime(250)).rejects.toThrow("process still exists");
  });
  it("refuses to start over an unmanaged listener", async () => {
    const url = await server((_req, res) => res.end('{"status":"ok"}'));
    vi.stubEnv("POTLUCK_SIDECAR_URL", url);
    await expect(startRuntime(500)).rejects.toThrow("unmanaged runtime");
  });
  it.skipIf(!existsSync(python))("starts once under racing launchers, reattaches, and shuts down via real Python control", async () => {
    const fixture = join(dir, "runtime");
    await writeFile(fixture, `#!${python}\n` + `
import argparse, asyncio, os, signal
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from potluck_local.runtime_control import RuntimeControl
parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int)
args = parser.parse_args()
@asynccontextmanager
async def lifespan(app):
    await asyncio.sleep(1.2)  # The losing launcher exits before the winner is ready.
    yield
app = FastAPI(lifespan=lifespan)
@app.middleware('http')
async def auth(request: Request, call_next):
    if request.url.path != '/health' and request.headers.get('authorization') != 'Bearer ' + os.environ['POTLUCK_SIDECAR_TOKEN']:
        return JSONResponse(status_code=401, content={})
    return await call_next(request)
@app.get('/health')
async def health(): return {'status': 'ok'}
@app.get('/control')
async def info(): return control.info()
@app.post('/shutdown')
async def shutdown():
    asyncio.get_running_loop().call_later(0.05, lambda: setattr(server, 'should_exit', True))
    return {'status': 'shutting_down'}
with RuntimeControl(port=args.port, host='127.0.0.1', token=os.environ['POTLUCK_SIDECAR_TOKEN'], owner='cli') as control:
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    server.run(sockets=[control.socket])
`, { mode: 0o700 });
    // Reserve an isolated port, then release it immediately before launching.
    const reservation = createServer();
    await new Promise<void>(r => reservation.listen(0, "127.0.0.1", r));
    const port = (reservation.address() as { port: number }).port;
    await new Promise<void>(r => reservation.close(() => r()));
    vi.stubEnv("POTLUCK_SIDECAR_URL", `http://127.0.0.1:${port}`);
    vi.stubEnv("POTLUCK_RUNTIME_BIN", fixture);
    vi.stubEnv("PYTHONPATH", pythonRoot);
    const results = await Promise.all([startRuntime(10000), startRuntime(10000)]);
    for (const result of results) spawned.add(result.pid!);
    expect(results[0].pid).toBe(results[1].pid);
    expect(results.filter(r => r.started)).toHaveLength(1);
    const current = await readRuntimeRecord();
    expect(await discoverRuntime({ dataDir: dir, port })).toEqual(current);
    expect(await startRuntime(500)).toMatchObject({ pid: current?.pid, started: false });
    expect(await stopRuntime(3000)).toEqual({ schemaVersion: 1, state: "stopped" });
    expect(await readRuntimeRecord()).toBeNull();
    expect(() => process.kill(current!.pid, 0)).toThrow();
    spawned.clear();
  }, 20000);
});

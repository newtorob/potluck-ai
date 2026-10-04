import { execFile } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

// Runs the built package exactly as installed; build is part of the test script.
const entry = fileURLToPath(new URL("../dist/index.js", import.meta.url));
let dir: string;
beforeEach(async () => { dir = await mkdtemp(join(tmpdir(), "potluck-entry-")); });
afterEach(async () => { await rm(dir, { recursive: true, force: true }); });
function run(args: string[], extraEnv: NodeJS.ProcessEnv = {}) {
  return new Promise<{ code: number; stdout: string; stderr: string }>(resolve => {
    const child = execFile(process.execPath, [entry, ...args], { timeout: 3000, cwd: dir,
      env: { ...process.env, POTLUCK_DATA_DIR: dir, POTLUCK_SIDECAR_URL: "http://127.0.0.1:1",
        POTLUCK_LOCAL_API_SOCKET: join(dir, "absent.sock"), POTLUCK_GATEWAY_KEY: "",
        POTLUCK_GATEWAY_URL: "", NO_COLOR: "1", ...extraEnv },
    }, (err, stdout, stderr) => resolve({ code: typeof err?.code === "number" ? err.code : err ? -1 : 0, stdout, stderr }));
    child.stdin?.end();
  });
}

describe("installed CLI dispatch", () => {
  it("prints help without a running app or a setup prompt", async () => {
    const r = await run(["--help"]);
    expect(r.code).toBe(0); expect(r.stdout).toContain("potluck doctor");
  });
  it("returns one structured report when every service is offline", async () => {
    const r = await run(["status", "--json", "--timeout", "50"]);
    expect(r.code).toBe(1); expect(r.stderr).toBe("");
    expect(JSON.parse(r.stdout)).toMatchObject({ schemaVersion: 1, ready: false, runtime: { reachable: false } });
  });
  it.each([["bogus"], ["setup"], []])("rejects invalid or interactive commands without hanging: %j", async (...args) => {
    expect((await run(args)).code).toBe(2);
  });
  it("reports missing logs as JSON without exposing config", async () => {
    const r = await run(["logs", "--json"]);
    expect(r.code).toBe(1); expect(JSON.parse(r.stdout)).toMatchObject({ error: "not_found" });
  });
});


it("the installed agent selects the project pin ahead of global and shared model preferences", async () => {
  const sha = "a".repeat(64);
  await writeFile(join(dir, "potluck.lock"), JSON.stringify({ schemaVersion: 1,
    model: { id: "pinned-model", format: "gguf", sha256: sha, sizeBytes: 42 } }));
  await writeFile(join(dir, "cli.json"), JSON.stringify({ model: "global-model", scope: "local" }));
  await writeFile(join(dir, "config.json"), JSON.stringify({ runtime: { active_model_id: "shared-model" }, gateway: { enabled: true } }));
  const calls: Record<string, unknown>[] = [];
  const server = createServer(async (req, res) => {
    if (req.url?.endsWith("/potluck/capabilities")) {
      res.setHeader("Content-Type", "application/json"); res.end(JSON.stringify({ model_lock_version: 1 })); return;
    }
    let body = ""; for await (const part of req) body += part;
    calls.push(JSON.parse(body));
    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("X-Potluck-Model-Lock", "1"); res.setHeader("X-Potluck-Model-SHA256", sha);
    res.end('data: {"choices":[{"delta":{"content":"The pinned model answered."}}]}\n\ndata: [DONE]\n\n');
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    const port = (server.address() as import("node:net").AddressInfo).port;
    const result = await run(["-p", "Say hello", "--no-verify"], {
      POTLUCK_GATEWAY_KEY: "test", POTLUCK_GATEWAY_URL: `http://127.0.0.1:${port}/v1`,
    });
    expect(result.code).toBe(0); expect(result.stdout).toContain("pinned model answered");
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({ model: "pinned-model", potluck_model_sha256: sha });
    const conflict = await run(["-m", "different", "-p", "hi"]);
    expect(conflict.code).toBe(1); expect(conflict.stderr).toContain("project pins");
    expect(calls).toHaveLength(1);
  } finally { await new Promise<void>(resolve => server.close(() => resolve())); }
});

it.each(["repeated failure", "step limit", "failed verify", "repaired verify"])(
  "reports the actual outcome of a one-shot agent run: %s", async scenario => {
    await writeFile(join(dir, "f.txt"), "original");
    let requests = 0;
    const server = createServer(async (req, res) => {
      for await (const _ of req) { /* consume */ }
      requests++;
      const action = (tool: string, args: Record<string, unknown>) =>
        "```action\n" + JSON.stringify({ tool, args }) + "\n```";
      let content: string;
      if (scenario === "repeated failure") content = action("read_file", { path: "absent.txt" });
      else if (scenario === "step limit") content = action("read_file", { path: "f.txt" });
      else if (requests === 1) content = action("edit_file", { path: "f.txt", old_string: "original", new_string: "broken" });
      else if (requests === 2 && scenario === "repaired verify") content = action("edit_file", { path: "f.txt", old_string: "broken", new_string: "fixed" });
      else content = "All fixed.";
      res.setHeader("Content-Type", "text/event-stream");
      res.end(`data: ${JSON.stringify({ choices: [{ delta: { content } }] })}\n\ndata: [DONE]\n\n`);
    });
    await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
    try {
      const port = (server.address() as import("node:net").AddressInfo).port;
      const nodePath = "'" + process.execPath.replace(/'/g, "'\\''") + "'";
      const verify = `${nodePath} -e "process.exit(require('fs').readFileSync('f.txt','utf8') === 'fixed' ? 0 : 1)"`;
      const result = await run(["-p", "Repair f.txt", "-m", "test-model", "--scope", "local", "--yes",
        ...(scenario.includes("verify") ? ["--verify", verify] : ["--no-verify"])], {
        POTLUCK_GATEWAY_KEY: "test", POTLUCK_GATEWAY_URL: `http://127.0.0.1:${port}/v1`,
      });
      expect(result.code).toBe(scenario === "repaired verify" ? 0 : 1);
      if (scenario === "failed verify") expect(result.stderr).toContain("Task incomplete: verification did not pass.");
      if (scenario === "step limit") expect(result.stderr).toContain("step limit");
      if (scenario === "repeated failure") expect(result.stderr).toContain("failed identically");
    } finally { await new Promise<void>(resolve => server.close(() => resolve())); }
  },
);

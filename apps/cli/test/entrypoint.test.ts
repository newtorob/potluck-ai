import { execFile } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

// Runs the built package exactly as installed; build is part of the test script.
const entry = fileURLToPath(new URL("../dist/index.js", import.meta.url));
let dir: string;
beforeEach(async () => { dir = await mkdtemp(join(tmpdir(), "potluck-entry-")); });
afterEach(async () => { await rm(dir, { recursive: true, force: true }); });
function run(args: string[]) {
  return new Promise<{ code: number; stdout: string; stderr: string }>(resolve => {
    const child = execFile(process.execPath, [entry, ...args], { timeout: 3000,
      env: { ...process.env, POTLUCK_DATA_DIR: dir, POTLUCK_SIDECAR_URL: "http://127.0.0.1:1",
        POTLUCK_LOCAL_API_SOCKET: join(dir, "absent.sock"), POTLUCK_GATEWAY_KEY: "",
        POTLUCK_GATEWAY_URL: "", NO_COLOR: "1" },
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

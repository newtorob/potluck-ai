import { mkdtemp, mkdir, writeFile, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readLogs, redactLog } from "../src/logs.js";

let dir: string;
beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "potluck-logs-"));
  vi.stubEnv("POTLUCK_DATA_DIR", dir);
  vi.stubEnv("POTLUCK_GATEWAY_KEY", "known-secret");
  await mkdir(join(dir, "logs"));
});
afterEach(async () => { vi.unstubAllEnvs(); await rm(dir, { recursive: true, force: true }); });

describe("bounded log reader", () => {
  it("reads only the requested tail and strips credentials and escape sequences", async () => {
    await writeFile(join(dir, "logs", "python.log"), 'old\nAuthorization: Bearer private-token\nvalue=known-secret\n\x1b[31merror\x1b[0m\n');
    const report = await readLogs("runtime", 3);
    expect(report.truncated).toBe(true);
    expect(report.lines).toEqual(["Authorization: Bearer [REDACTED]", "value=[REDACTED]", "error"]);
  });
  it("caps bytes, drops a partial leading line and does not echo PEM material", async () => {
    await writeFile(join(dir, "logs", "runtime.log"), "x".repeat(300000) + "\nlast\n");
    const report = await readLogs("runtime", 100);
    expect(report.lines).toEqual(["last"]);
    expect(report.truncated).toBe(true);
    for (const input of [
      'pairing_token="abc123" node_token=def456',
      '{"api_key":"ghi789","device_code":"jkl123"}',
      "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----",
      "abc123\n-----END PRIVATE KEY-----",
      "https://user:abc123@host.test/callback?token=def456",
    ]) expect(redactLog(input)).not.toMatch(/abc123|def456|ghi789|jkl123/);
  });
  it("reports missing logs without reading the real user's app logs", async () => {
    expect(await readLogs("app", 100)).toMatchObject({ error: "not_found", path: null, lines: [] });
  });
  it.skipIf(process.platform === "win32")("redacts the discovered control credential even without a token label", async () => {
    const token = "b".repeat(64);
    await mkdir(join(dir, "run"), { mode: 0o700 });
    await writeFile(join(dir, "run/runtime.json"), JSON.stringify({ schemaVersion: 1,
      instanceId: "00000000-0000-0000-0000-000000000001", pid: 100, owner: "cli",
      url: "http://127.0.0.1:8321", version: "0.1.0", token }), { mode: 0o600 });
    await writeFile(join(dir, "logs/runtime.log"), `unexpected ${token}\n`);
    expect((await readLogs("runtime", 10)).lines).toEqual(["unexpected [REDACTED]"]);
  });
  it.skipIf(process.platform === "win32")("refuses a log symlink escaping its directory", async () => {
    await writeFile(join(dir, "secret.txt"), "secret");
    await symlink(join(dir, "secret.txt"), join(dir, "logs", "runtime.log"));
    expect(await readLogs("runtime", 100)).toMatchObject({ error: "unsafe_path", lines: [] });
  });
});

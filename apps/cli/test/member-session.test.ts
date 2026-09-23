import { createRequire } from "node:module";
import { chmod, mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, expect, it } from "vitest";
const require = createRequire(import.meta.url);
const { readMemberSession } = require("../../desktop/electron/member-session.js");
let dir: string;
beforeEach(async () => { dir = await mkdtemp(join(tmpdir(), "potluck-app-member-")); });
afterEach(async () => { await rm(dir, { recursive: true, force: true }); });
async function write(token: string | null) {
  await mkdir(join(dir, "auth"), { mode: 0o700 });
  await writeFile(join(dir, "auth", "member-session.json"), JSON.stringify({ schemaVersion: 1, cloudOrigin: "https://account.example", token }), { mode: 0o600 });
}
it("distinguishes a missing legacy migration from explicit shared logout", async () => {
  expect(readMemberSession("https://account.example", dir)).toBeUndefined();
  await write(null);
  expect(readMemberSession("https://account.example/", dir).token).toBeNull();
});
it("reads a shared session only for its own origin", async () => {
  await write("private-token");
  expect(readMemberSession("https://account.example", dir).token).toBe("private-token");
  expect(() => readMemberSession("https://other.example", dir)).toThrow();
});
it("treats expired shared sessions as signed out without legacy fallback", async () => {
  await write("private-token");
  const file = join(dir, "auth", "member-session.json");
  await writeFile(file, JSON.stringify({ schemaVersion: 1, cloudOrigin: "https://account.example", token: "private-token", expiresAt: "2020-01-01T00:00:00Z" }));
  expect(readMemberSession("https://account.example", dir).token).toBeNull();
});
it("rejects readable-by-others and symlinked records", async () => {
  await write("private-token");
  const file = join(dir, "auth", "member-session.json");
  await chmod(file, 0o644);
  expect(() => readMemberSession("https://account.example", dir)).toThrow();
  await rm(file);
  await symlink(join(dir, "missing"), file);
  expect(() => readMemberSession("https://account.example", dir)).toThrow();
});

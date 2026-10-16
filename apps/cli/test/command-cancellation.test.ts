import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { runTool, runVerify } from "../src/tools.js";

let cwd: string;
beforeEach(async () => { cwd = await mkdtemp(join(tmpdir(), "potluck-cancel-")); });
afterEach(async () => { await rm(cwd, { force: true, recursive: true }); });
const pause = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
async function ready(): Promise<number[]> {
  for (let i = 0; i < 100; i++) {
    try { return JSON.parse(await readFile(join(cwd, "ready.json"), "utf8")); } catch {}
    await pause(20);
  }
  throw new Error("command did not start");
}
async function fixture(): Promise<void> {
  await writeFile(join(cwd, "child.cjs"), `
    const fs = require('node:fs');
    process.on('SIGTERM', () => {});
    fs.writeFileSync('ready.json', JSON.stringify([process.ppid, process.pid]));
    setInterval(() => {}, 1000);
  `);
  await writeFile(join(cwd, "parent.cjs"), `
    const { spawn } = require('node:child_process');
    spawn(process.execPath, ['child.cjs'], { stdio: 'inherit' });
    setInterval(() => {}, 1000);
  `);
}
async function expectGone(pids: number[]): Promise<void> {
  for (const pid of pids) {
    let alive = true;
    for (let i = 0; i < 50 && alive; i++) {
      try { process.kill(pid, 0); await pause(20); } catch { alive = false; }
    }
    expect(alive, `process ${pid} must stop`).toBe(false);
  }
}

describe("command cancellation", () => {
  it("does not start an already cancelled command", async () => {
    const controller = new AbortController(); controller.abort();
    await expect(runTool("run_command", { command: "touch unexpected" }, {
      cwd, signal: controller.signal, confirm: async () => true,
    })).rejects.toMatchObject({ name: "AbortError" });
    await expect(readFile(join(cwd, "unexpected"))).rejects.toMatchObject({ code: "ENOENT" });
  });
  it("does not start a command cancelled during approval", async () => {
    const controller = new AbortController();
    await expect(runTool("run_command", { command: "touch unexpected" }, {
      cwd, signal: controller.signal, confirm: async () => { controller.abort(); return true; },
    })).rejects.toMatchObject({ name: "AbortError" });
    await expect(readFile(join(cwd, "unexpected"))).rejects.toMatchObject({ code: "ENOENT" });
  });
  it.skipIf(process.platform === "win32")("kills a command and its TERM-resistant descendant", async () => {
    await fixture();
    const controller = new AbortController();
    const work = runTool("run_command", { command: "node parent.cjs" }, {
      cwd, signal: controller.signal, confirm: async () => true,
    });
    const rejection = expect(work).rejects.toMatchObject({ name: "AbortError" });
    const pids = await ready(); controller.abort();
    await rejection; await expectGone(pids);
  });
  it.skipIf(process.platform === "win32")("kills a configured check and its descendant", async () => {
    await fixture();
    const controller = new AbortController();
    const rejection = expect(runVerify("node parent.cjs", cwd, controller.signal)).rejects.toMatchObject({ name: "AbortError" });
    const pids = await ready(); controller.abort();
    await rejection; await expectGone(pids);
  });
  it("preserves normal exit codes and output", async () => {
    expect(await runVerify("node -e 'console.log(42);process.exit(7)'", cwd)).toEqual({ code: 7, output: "42" });
  });
});

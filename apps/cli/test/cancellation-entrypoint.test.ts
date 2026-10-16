import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, it } from "vitest";

const entry = fileURLToPath(new URL("../dist/index.js", import.meta.url));
const pause = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
it.skipIf(process.platform === "win32").each(["command", "verify"])("one-shot cancellation stops its active %s", async kind => {
  const dir = await mkdtemp(join(tmpdir(), "potluck-cancel-entry-"));
  await writeFile(join(dir, "wait.cjs"), `
    require('node:fs').writeFileSync('running', String(process.pid));
    process.on('SIGTERM', () => {});
    setInterval(() => {}, 1000);
  `);
  const command = "'" + process.execPath.replace(/'/g, "'\\''") + "' wait.cjs";
  const server = createServer(async (req, res) => {
    if (req.url?.startsWith("/v1/potluck/context?")) {
      res.end(JSON.stringify({ model: "fixture", scope: "local", context_window: 8192 })); return;
    }
    for await (const _ of req) {}
    const content = kind === "verify" ? "Done." : "```action\n" + JSON.stringify({ tool: "run_command", args: { command } }) + "\n```";
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.end(`data: ${JSON.stringify({ choices: [{ delta: { content }, finish_reason: "stop" }] })}\n\ndata: [DONE]\n\n`);
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as import("node:net").AddressInfo).port;
  const cli = spawn(process.execPath, [entry, "-p", "Perform the task", "-m", "fixture", "--scope", "local", "--yes",
    ...(kind === "verify" ? ["--verify", command] : ["--no-verify"])], {
    cwd: dir, env: { ...process.env, POTLUCK_DATA_DIR: dir, POTLUCK_GATEWAY_KEY: "test", POTLUCK_GATEWAY_URL: `http://127.0.0.1:${port}/v1`, NO_COLOR: "1" },
  });
  let stderr = ""; cli.stderr.on("data", b => { stderr += b; }); cli.stdout.resume(); cli.stdin.end();
  const exited = new Promise<number | null>(resolve => cli.once("exit", resolve));
  let pid = 0;
  try {
    for (let i = 0; i < 150; i++) {
      try { pid = Number(await readFile(join(dir, "running"), "utf8")); break; } catch {}
      await pause(20);
    }
    expect(pid, stderr).toBeGreaterThan(0);
    cli.kill("SIGINT");
    const code = await Promise.race([exited, pause(3000).then(() => "timeout")]);
    expect(code).toBe(130);
    expect(stderr).toContain("Task cancelled");
    expect(() => process.kill(pid, 0)).toThrow();
  } finally {
    cli.kill("SIGKILL");
    if (pid) { try { process.kill(pid, "SIGKILL"); } catch {} }
    server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve()));
    await rm(dir, { recursive: true, force: true });
  }
}, 10_000);

import { createServer, type Server } from "node:http";
import { spawn } from "node:child_process";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Readable } from "node:stream";
import { afterEach, beforeEach, expect, it } from "vitest";
import { readPrompt } from "../src/inference.js";
import { parseArgs } from "../src/args.js";
let directory: string, server: Server, url: string;
let mode: "ok" | "truncated" | "slow" | "old";
let requests: Array<any>;
const hash = "a".repeat(64);
const entry = resolve("dist/index.js");
beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), "potluck-run-")); mode = "ok"; requests = [];
  await writeFile(join(directory, "potluck.lock"), JSON.stringify({ schemaVersion: 1, model: { id: "fixture", format: "gguf", sha256: hash, sizeBytes: 100 } }));
  server = createServer(async (req, res) => {
    if (req.url?.endsWith("capabilities")) { res.end(JSON.stringify({ model_lock_version: mode === "old" ? 0 : 1 })); return; }
    let raw = ""; for await (const chunk of req) raw += chunk;
    requests.push({ body: JSON.parse(raw), headers: req.headers });
    if (mode === "slow") return;
    res.writeHead(200, { "Content-Type": "text/event-stream", "X-Potluck-Model-Lock": "1", "X-Potluck-Model-SHA256": hash });
    res.write('data: {"choices":[{"delta":{"content":"hello"}}]}\r');
    res.write('\n\r\n');
    res.end(mode === "truncated" ? "" : "data: [DONE]\r\n\r\n");
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  url = `http://127.0.0.1:${(server.address() as any).port}/v1`;
});
afterEach(async () => {
  await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); });
  await rm(directory, { recursive: true, force: true });
});
async function run(args: string[], input = "") {
  const child = spawn(process.execPath, [entry, "run", ...args], { cwd: directory,
    env: { ...process.env, POTLUCK_DATA_DIR: directory, POTLUCK_GATEWAY_URL: url, POTLUCK_GATEWAY_KEY: "never-echo" }, stdio: ["pipe", "pipe", "pipe"] });
  let stdout = "", stderr = "";
  child.stdout.on("data", chunk => { stdout += chunk; }); child.stderr.on("data", chunk => { stderr += chunk; }); child.stdin.end(input);
  const code = await new Promise<number | null>(resolve => child.on("close", resolve));
  return { code, stdout, stderr };
}
it("combines a prompt and stdin and returns one structured response with exact pin enforcement", async () => {
  const result = await run(["Summarize", "--scope", "local", "--json"], "input document");
  expect(result.code).toBe(0); expect(result.stderr).toBe("");
  expect(JSON.parse(result.stdout)).toMatchObject({ schemaVersion: 1, text: "hello", model: "fixture", scope: "local", sha256: hash });
  expect(requests[0].body.messages).toEqual([{ role: "user", content: "Summarize\n\ninput document" }]);
  expect(requests[0].body.potluck_model_sha256).toBe(hash);
  expect(requests[0].headers["x-potluck-scope"]).toBe("local");
});
it("keeps ordinary output pipeline-friendly", async () => {
  const result = await run([], "hi");
  expect(result).toEqual({ code: 0, stdout: "hello\n", stderr: "" });
});
it("does not emit a partial success object on a dropped stream", async () => {
  mode = "truncated"; const result = await run(["hi", "--json"]);
  expect(result.code).toBe(1); expect(JSON.parse(result.stdout).error.code).toBe("inference_failed");
  expect(result.stdout).not.toContain("hello");
});
it("bounds hung requests and rejects an old runtime before sending input", async () => {
  mode = "slow"; const slow = await run(["hi", "--timeout", "150", "--json"]);
  expect(slow.code).toBe(124); expect(JSON.parse(slow.stdout).error.code).toBe("timeout");
  mode = "old"; requests = [];
  expect((await run(["private", "--json"])).code).toBe(1); expect(requests).toHaveLength(0);
});
it("bounds piped input and validates command-specific options", async () => {
  await expect(readPrompt(undefined, Readable.from([Buffer.alloc(1024 * 1024 + 1)]), new AbortController().signal)).rejects.toThrow("1 MiB");
  expect(parseArgs(["run", "hi"]).timeout).toBe(120000);
  expect(() => parseArgs(["status", "--timeout", "30001"])).toThrow();
  expect(() => parseArgs(["run", "hi", "--scope", "pool"])).toThrow();
});

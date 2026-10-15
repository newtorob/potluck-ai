import { execFile } from "node:child_process";
import { chmod, mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { createServer, type IncomingHttpHeaders } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

// The installed CLI and its bridge run unchanged; only Aider and inference are fixtures.
const entry = fileURLToPath(new URL("../dist/index.js", import.meta.url));
const model = "fixture-coder";
const digest = "a".repeat(64);
const gatewayKey = "isolated-launch-gateway-key";
const task = "Explain PRIVATE_PROJECT_REQUEST without editing files.";
const existingEnv = "OPENAI_API_BASE=http://127.0.0.1:1/v1\nOPENAI_API_KEY=existing-project-key\n";
const existingConfig = "model: openai/other\nmap-tokens: 999999\n";
let directory: string;
let project: string;
let profile: string;
let binary: string;
let capturePath: string;
const cleanup: Array<() => Promise<void>> = [];

interface Call {
  url: string;
  headers: IncomingHttpHeaders;
  body?: Record<string, unknown>;
}
interface Capture {
  argv: string[];
  files: Record<string, { path: string; content: string; mode: number }>;
  env: Record<string, string>;
  response?: { status: number; text: string };
}
interface GatewayOptions {
  window?: number | null;
  windowAfterLaunch?: number | null;
  contextStatus?: number;
  contextDetail?: string;
  contextModel?: string;
  contextScope?: string;
  finish?: string | null;
  done?: boolean;
  output?: string;
  inferred?: boolean;
  probeError?: string;
  probeStatus?: number;
}

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), "potluck-launch-test-"));
  project = join(directory, "project");
  profile = join(directory, "profile");
  binary = join(directory, "fake-aider.cjs");
  capturePath = join(directory, "child.json");
  await mkdir(project); await mkdir(profile); await mkdir(join(project, ".git"));
  await writeFile(join(project, ".env"), existingEnv);
  await writeFile(join(project, ".aider.conf.yml"), existingConfig);
  await writeFile(join(project, "potluck.lock"), JSON.stringify({ schemaVersion: 1,
    model: { id: model, format: "gguf", sha256: digest, sizeBytes: 100 } }));
  await writeFile(join(profile, "cli.json"), JSON.stringify({ model: "wrong-global-model", scope: "mine" }));
  await writeFile(join(profile, "config.json"), JSON.stringify({ active_model_id: "wrong-shared-model" }));
  await writeFile(binary, `#!${process.execPath}
const fs = require("node:fs/promises");
async function main() {
  const argv = process.argv.slice(2);
  if (argv[0] === "--version") { process.stdout.write("aider fixture\\n"); return; }
  const value = flag => argv.includes(flag) ? argv[argv.indexOf(flag) + 1] : undefined;
  const capture = { argv, files: {}, env: Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    key.startsWith("POTLUCK_") || ["OPENAI_API_BASE", "OPENAI_API_KEY", "AIDER_OPENAI_API_KEY"].includes(key))) };
  for (const flag of ["--env-file", "--model-settings-file", "--model-metadata-file"]) {
    const path = value(flag);
    if (path) capture.files[flag] = { path, content: await fs.readFile(path, "utf8"), mode: (await fs.stat(path)).mode & 0o777 };
  }
  await fs.writeFile(${JSON.stringify(capturePath)}, JSON.stringify(capture));
  const settings = JSON.parse(capture.files["--model-settings-file"].content)[0];
  const response = await fetch(process.env.OPENAI_API_BASE + "/chat/completions", {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + process.env.OPENAI_API_KEY },
    body: JSON.stringify({ model: value("--model").replace(/^openai\\//, ""),
      messages: [{ role: "user", content: value("--message") }], stream: true, max_tokens: settings.extra_params.max_tokens }),
  });
  capture.response = { status: response.status, text: await response.text() };
  await fs.writeFile(${JSON.stringify(capturePath)}, JSON.stringify(capture));
  process.exitCode = response.ok ? 0 : 3;
}
main().catch(error => { process.stderr.write(String(error)); process.exitCode = 4; });
`, { mode: 0o700 });
  await chmod(binary, 0o700);
});
afterEach(async () => {
  for (const close of cleanup.splice(0).reverse()) await close();
  await rm(directory, { recursive: true, force: true });
});

async function gateway(options: GatewayOptions = {}) {
  const calls: Call[] = [];
  let contextRequests = 0;
  const server = createServer(async (req, res) => {
    const call: Call = { url: req.url ?? "", headers: req.headers };
    calls.push(call);
    res.setHeader("Content-Type", "application/json");
    if (call.url.startsWith("/v1/potluck/context?")) {
      contextRequests++;
      res.statusCode = options.contextStatus ?? 200;
      if (res.statusCode !== 200) {
        res.end(JSON.stringify({ detail: options.contextDetail ?? "Not Found" })); return;
      }
      const query = new URL(call.url, "http://fixture").searchParams;
      const window = contextRequests > 1 && options.windowAfterLaunch !== undefined
        ? options.windowAfterLaunch : options.window === undefined ? 8192 : options.window;
      res.end(JSON.stringify({ model: options.contextModel ?? query.get("model"),
        scope: options.contextScope ?? query.get("scope"), context_window: window }));
      return;
    }
    if (call.url === "/v1/potluck/capabilities") {
      res.end(JSON.stringify({ model_lock_version: 1 })); return;
    }
    if (call.url !== "/v1/chat/completions") {
      res.statusCode = 404; res.end(JSON.stringify({ detail: "Unexpected fixture request" })); return;
    }
    let raw = ""; for await (const chunk of req) raw += chunk;
    call.body = JSON.parse(raw) as Record<string, unknown>;
    const probe = !raw.includes("PRIVATE_PROJECT_REQUEST");
    if (probe && options.probeStatus) {
      res.statusCode = options.probeStatus;
      res.end(JSON.stringify({ detail: options.probeError ?? "Selected route unavailable" })); return;
    }
    res.setHeader("X-Potluck-Model-Lock", "1");
    res.setHeader("X-Potluck-Model-SHA256", digest);
    res.setHeader("Content-Type", "text/event-stream");
    if (probe && options.probeError) {
      res.end(`data: ${JSON.stringify({ error: { message: options.probeError } })}\n\ndata: [DONE]\n\n`); return;
    }
    const content = probe ? options.output ?? "OK" : "Fixture Aider response";
    const reason = probe ? options.finish === undefined ? "stop" : options.finish : "stop";
    const chunk = { choices: [{ index: 0, delta: { content }, ...(reason === null ? {} : { finish_reason: reason }) }],
      ...(probe && options.inferred ? { potluck: { finish_reason_inferred: true } } : {}) };
    res.end(`data: ${JSON.stringify(chunk)}\n\n${!probe || options.done !== false ? "data: [DONE]\n\n" : ""}`);
  });
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  cleanup.push(() => new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); }));
  const port = (server.address() as import("node:net").AddressInfo).port;
  return { calls, baseURL: `http://127.0.0.1:${port}/v1` };
}

function run(baseURL: string, scope: "local" | "mine" = "local") {
  return new Promise<{ code: number; stdout: string; stderr: string }>(resolve => {
    // Do not pass the developer's provider credentials, config directories or gateway to either child.
    const child = execFile(process.execPath, [entry, "launch", "aider", "--scope", scope, "--message", task], {
      cwd: project, timeout: 8000, maxBuffer: 128 * 1024,
      env: { PATH: dirname(process.execPath), POTLUCK_DATA_DIR: profile,
        POTLUCK_GATEWAY_URL: baseURL, POTLUCK_GATEWAY_KEY: gatewayKey, POTLUCK_AIDER_BIN: binary,
        POTLUCK_SIDECAR_URL: "http://127.0.0.1:1", POTLUCK_LOCAL_API_SOCKET: join(directory, "absent.sock"), NO_COLOR: "1" },
    }, (error, stdout, stderr) => resolve({ code: typeof error?.code === "number" ? error.code : error ? -1 : 0, stdout, stderr }));
    child.stdin?.end();
  });
}
async function capture(): Promise<Capture> { return JSON.parse(await readFile(capturePath, "utf8")) as Capture; }
const flag = (argv: string[], name: string) => argv.includes(name) ? argv[argv.indexOf(name) + 1] : undefined;
const chats = (calls: Call[]) => calls.filter(call => call.url === "/v1/chat/completions");
const contexts = (calls: Call[]) => calls.filter(call => call.url.startsWith("/v1/potluck/context?"));

// The executable fixture uses a POSIX shebang. No real Aider install is needed.
describe.skipIf(process.platform === "win32")("installed Aider launcher preflight", () => {
  it.each(["local", "mine"] as const)("uses the exact pinned %s route before project content and throughout the launch", async scope => {
    const mock = await gateway();
    const result = await run(mock.baseURL, scope);
    expect(result.code, result.stderr).toBe(0);
    expect(mock.calls[0].url).toMatch(/^\/v1\/potluck\/context\?/);
    expect(contexts(mock.calls).length).toBeGreaterThanOrEqual(2);
    for (const call of contexts(mock.calls)) {
      const query = new URL(call.url, "http://fixture").searchParams;
      expect(Object.fromEntries(query)).toEqual({ model, scope, potluck_model_sha256: digest });
      expect(call.headers.authorization).toBe(`Bearer ${gatewayKey}`);
    }
    expect(chats(mock.calls)).toHaveLength(2);
    for (const call of chats(mock.calls)) {
      expect(call.headers["x-potluck-scope"]).toBe(scope);
      expect(call.headers.authorization).toBe(`Bearer ${gatewayKey}`);
      expect(call.body).toMatchObject({ model, potluck_model_sha256: digest });
    }
    expect(JSON.stringify(chats(mock.calls)[0].body)).not.toContain("PRIVATE_PROJECT_REQUEST");
    expect(JSON.stringify(chats(mock.calls)[1].body)).toContain(task);
    const child = await capture();
    expect(child.argv).toContain("--no-gitignore");
    expect(child.argv).not.toContain("--yes-always");
    expect(child.response?.status).toBe(200);
    expect(Object.keys(child.env).some(key => key.startsWith("POTLUCK_"))).toBe(false);
    expect(JSON.stringify(child)).not.toContain(gatewayKey);
    expect(result.stdout + result.stderr).not.toContain(gatewayKey);
  });

  it.each([8192, 32768])("derives Aider budgets from a %i loaded window and cleans temporary files", async window => {
    const mock = await gateway({ window });
    const result = await run(mock.baseURL);
    expect(result.code, result.stderr).toBe(0);
    const child = await capture();
    const output = Math.min(4096, Math.floor(window / 4));
    const input = window - output - 512;
    const settings = JSON.parse(child.files["--model-settings-file"].content);
    expect(settings[0]).toMatchObject({ name: `openai/${model}`,
      weak_model_name: `openai/${model}`, editor_model_name: `openai/${model}`,
      extra_params: { max_tokens: output, api_base: child.env.OPENAI_API_BASE, api_key: child.env.OPENAI_API_KEY } });
    expect(settings[1]).toEqual({ name: "aider/extra_params", extra_params: settings[0].extra_params });
    expect(flag(child.argv, "--max-chat-history-tokens")).toBe(String(Math.min(4096, Math.floor(input / 4))));
    expect(flag(child.argv, "--map-tokens")).toBe(String(Math.min(1024, Math.floor(input / 8))));
    expect(flag(child.argv, "--map-multiplier-no-files")).toBe("1");
    for (const name of ["--model", "--weak-model", "--editor-model"]) expect(flag(child.argv, name)).toBe(`openai/${model}`);
    expect(JSON.parse(child.files["--model-metadata-file"].content)).toEqual({ [`openai/${model}`]: {
      max_input_tokens: input, max_output_tokens: output, max_tokens: output,
      input_cost_per_token: 0, output_cost_per_token: 0, litellm_provider: "openai", mode: "chat",
    } });
    for (const file of Object.values(child.files)) {
      expect(file.mode).toBe(0o600);
      await expect(stat(file.path)).rejects.toMatchObject({ code: "ENOENT" });
    }
    await expect(stat(dirname(child.files["--env-file"].path))).rejects.toMatchObject({ code: "ENOENT" });
    expect(await readFile(join(project, ".env"), "utf8")).toBe(existingEnv);
    expect(await readFile(join(project, ".aider.conf.yml"), "utf8")).toBe(existingConfig);
  });

  it.each([
    ["unknown loaded window", { window: null }],
    ["legacy runtime", { contextStatus: 404 }],
    ["too-small loaded window", { window: 4096 }],
    ["different model", { contextModel: "other-model" }],
    ["different scope", { contextScope: "mine" }],
  ] satisfies Array<[string, GatewayOptions]>)("refuses %s before the readiness probe or Aider", async (_name, options) => {
    const mock = await gateway(options);
    const result = await run(mock.baseURL);
    expect(result.code).toBe(1);
    expect(result.stderr).toMatch(/context|runtime|scope|model/i);
    await expect(stat(capturePath)).rejects.toMatchObject({ code: "ENOENT" });
    expect(chats(mock.calls)).toHaveLength(0);
  });

  it.each([
    ["missing DONE", { done: false }],
    ["missing native finish", { finish: null }],
    ["legacy inferred finish", { inferred: true }],
    ["content filter", { finish: "content_filter" }],
    ["native tools", { finish: "tool_calls" }],
    ["empty output", { output: "" }],
  ] satisfies Array<[string, GatewayOptions]>)("does not launch after a probe with %s", async (_name, options) => {
    const mock = await gateway(options);
    const result = await run(mock.baseURL);
    expect(result.code).toBe(1);
    await expect(stat(capturePath)).rejects.toMatchObject({ code: "ENOENT" });
    expect(chats(mock.calls)).toHaveLength(1);
    expect(JSON.stringify(chats(mock.calls)[0].body)).not.toContain(task);
  });

  it("accepts a nonempty output-limited synthetic probe that reaches DONE", async () => {
    const mock = await gateway({ finish: "length" });
    const result = await run(mock.baseURL);
    expect(result.code, result.stderr).toBe(0);
    expect((await capture()).response?.status).toBe(200);
  });

  it.each([4096, null])("blocks the first project request when loaded context changes to %s, then cleans up", async windowAfterLaunch => {
    const mock = await gateway({ windowAfterLaunch });
    const result = await run(mock.baseURL);
    expect(result.code).toBe(3); // The fixture child propagates a rejected provider request.
    const child = await capture();
    expect(child.response?.status).toBe(400);
    expect(child.response?.text).toMatch(/context|restart/i);
    expect(chats(mock.calls)).toHaveLength(1);
    expect(JSON.stringify(chats(mock.calls)[0].body)).not.toContain(task);
    expect(contexts(mock.calls)).toHaveLength(2);
    await expect(stat(dirname(child.files["--env-file"].path))).rejects.toMatchObject({ code: "ENOENT" });
  });

  it("reports a rejected one-shot request even when Aider itself exits zero", async () => {
    const source = await readFile(binary, "utf8");
    await writeFile(binary, source.replace("process.exitCode = response.ok ? 0 : 3;", "process.exitCode = 0;"));
    const mock = await gateway({ windowAfterLaunch: 4096 });
    const result = await run(mock.baseURL);
    expect(result.code).toBe(1);
    expect((await capture()).response?.status).toBe(400);
    expect(chats(mock.calls)).toHaveLength(1);
  });

  it.each([
    ["context", { contextStatus: 409, contextDetail: `Pinned model hash conflicts with the selected machine. Credential: ${gatewayKey}` }],
    ["probe", { probeError: `Pinned model hash conflicts with the selected machine. Credential: ${gatewayKey}` }],
    ["HTTP probe", { probeStatus: 409, probeError: `Pinned model hash conflicts with the selected machine. Credential: ${gatewayKey}` }],
  ] satisfies Array<[string, GatewayOptions]>)("gives safe, actionable %s conflict guidance and does not start Aider", async (_name, options) => {
    const mock = await gateway(options);
    const result = await run(mock.baseURL);
    expect(result.code).toBe(1);
    expect(result.stderr).toMatch(/pin|hash|lock|conflict/i);
    expect(result.stdout + result.stderr).not.toContain(gatewayKey);
    await expect(stat(capturePath)).rejects.toMatchObject({ code: "ENOENT" });
    expect(chats(mock.calls).length).toBeLessThanOrEqual(1);
  });
});

import { execFile } from "node:child_process";
import { mkdtemp, mkdir, writeFile, readFile, rm, stat } from "node:fs/promises";
import { createServer, type IncomingHttpHeaders } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { openCodeBudget, openCodeConfig, verifyOpenCodeConfig } from "../src/launch-opencode.js";
import { toolRequestTokens } from "../src/integration-tools.js";
import { parseArgs } from "../src/args.js";

const entry = fileURLToPath(new URL("../dist/index.js", import.meta.url));
const model = "fixture-coder", digest = "a".repeat(64), secret = "real-private-gateway-key";
const message = "PRIVATE_PROJECT_REQUEST";
let directory: string, project: string, profile: string, binary: string, capture: string;
const cleanup: Array<() => Promise<void>> = [];
beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), "potluck-opencode-test-"));
  project = join(directory, "project"); profile = join(directory, "profile");
  binary = join(directory, "opencode.cjs"); capture = join(directory, "capture.json");
  await mkdir(project); await mkdir(profile);
  await writeFile(join(project, "potluck.lock"), JSON.stringify({ schemaVersion: 1, model: { id: model, format: "gguf", sha256: digest, sizeBytes: 100 } }));
  await writeFile(join(project, "opencode.json"), '{"model":"wrong/model"}');
  await writeFile(binary, `#!${process.execPath}
const fs = require('node:fs/promises');
async function main(){
 const argv=process.argv.slice(2);
 if(argv.includes('--version')){if(Object.keys(process.env).some(k=>k.startsWith('POTLUCK_')))throw new Error('Gateway environment leaked');console.log('1.19.3');return;}
 const config=JSON.parse(process.env.OPENCODE_CONFIG_CONTENT);
 if(argv.includes('debug')){console.log(JSON.stringify(config));return;}
 const provider=config.provider[config.enabled_providers[0]];
 const response=await fetch(provider.options.baseURL+'/chat/completions',{method:'POST',headers:{Authorization:'Bearer '+provider.options.apiKey,'Content-Type':'application/json'},body:JSON.stringify({model:${JSON.stringify(model)},stream:true,max_tokens:99999,messages:[{role:'user',content:argv.at(-1)}],tools:[{type:'function',function:{name:'read',parameters:{type:'object'}}}]})});
 await fs.writeFile(${JSON.stringify(capture)},JSON.stringify({argv,config,env:Object.fromEntries(Object.entries(process.env).filter(([k])=>k.startsWith('POTLUCK_')||k.startsWith('OPENCODE_')||k.startsWith('XDG_'))),status:response.status,body:await response.text()}));
 // Some external clients exit zero even after displaying a provider failure.
}
main().catch(e=>{console.error(String(e));process.exitCode=9;});
`, { mode: 0o700 });
});
afterEach(async () => { for (const close of cleanup.splice(0).reverse()) await close(); await rm(directory, { recursive: true, force: true }); });

type Options = { window?: number | null; changedWindow?: number; badCall?: boolean; badResult?: boolean; inferred?: boolean; acknowledge?: boolean; upstreamStatus?: number };
async function gateway(options: Options = {}) {
  const calls: Array<{ body: any; headers: IncomingHttpHeaders }> = []; let contexts = 0;
  const server = createServer(async (req, res) => {
    res.setHeader("Content-Type", "application/json");
    if (req.url?.startsWith("/v1/potluck/context?")) {
      contexts++;
      const query = new URL(req.url, "http://fixture").searchParams;
      expect(query.get("potluck_model_sha256")).toBe(digest);
      res.end(JSON.stringify({ model, scope: query.get("scope"), context_window: contexts > 3 && options.changedWindow !== undefined ? options.changedWindow : options.window === undefined ? 32768 : options.window })); return;
    }
    let raw = ""; for await (const part of req) raw += part;
    const body = JSON.parse(raw); calls.push({ body, headers: req.headers });
    if (options.acknowledge !== false) { res.setHeader("X-Potluck-Model-Lock", "1"); res.setHeader("X-Potluck-Model-SHA256", digest); }
    if (body.stream) { res.statusCode = options.upstreamStatus ?? 200; res.end('data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'); return; }
    const first = body.tool_choice?.function?.name === "potluck_probe";
    res.end(JSON.stringify({ ...(options.inferred ? { potluck: { finish_reason_inferred: true } } : {}),
      choices: [{ finish_reason: first ? "tool_calls" : options.badResult ? "length" : "stop", message: first
        ? { role: "assistant", content: null, tool_calls: [{ id: "probe", type: "function", function: { name: options.badCall ? "wrong" : "potluck_probe", arguments: '{"value":"POTLUCK_TOOL_OK"}' } }] }
        : { role: "assistant", content: "POTLUCK_TOOL_OK" } }] }));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  cleanup.push(() => new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); }));
  return { calls, baseURL: `http://127.0.0.1:${(server.address() as import("node:net").AddressInfo).port}/v1` };
}
async function run(baseURL: string, scope = "local", prompt = message) {
  return new Promise<{ code: number; stdout: string; stderr: string }>(resolve => {
    const child = execFile(process.execPath, [entry, "launch", "opencode", "--scope", scope, "--message", prompt], {
      cwd: project, timeout: 15000, maxBuffer: 1024 * 1024,
      env: { PATH: dirname(process.execPath), POTLUCK_OPENCODE_BIN: binary, POTLUCK_GATEWAY_URL: baseURL,
        POTLUCK_GATEWAY_KEY: secret, POTLUCK_DATA_DIR: profile, POTLUCK_SIDECAR_URL: "http://127.0.0.1:1",
        POTLUCK_LOCAL_API_SOCKET: join(directory, "absent.sock"), OPENCODE_CONFIG: "/private/wrong.json",
        OPENCODE_PERMISSION: '{"*":"allow"}', OPENCODE_CONFIG_CONTENT: '{"model":"wrong/model"}' },
    }, (error, stdout, stderr) => resolve({ code: typeof error?.code === "number" ? error.code : error ? -1 : 0, stdout, stderr }));
    child.stdin?.end();
  });
}

describe.skipIf(process.platform === "win32")("OpenCode launcher", () => {
  it.each(["local", "mine"])("keeps every native-tool request on the pinned %s route and cleans the isolated profile", async scope => {
    const mock = await gateway(); const result = await run(mock.baseURL, scope);
    expect(result.code, result.stderr).toBe(0); expect(mock.calls).toHaveLength(3);
    for (const call of mock.calls) {
      expect(call.headers.authorization).toBe(`Bearer ${secret}`); expect(call.headers["x-potluck-scope"]).toBe(scope);
      expect(call.body).toMatchObject({ model, potluck_model_sha256: digest });
    }
    expect(JSON.stringify(mock.calls.slice(0, 2))).not.toContain(message);
    expect(mock.calls[2].body).toMatchObject({ max_tokens: 4096, messages: [{ role: "user", content: message }], tools: [{ type: "function" }] });
    const child = JSON.parse(await readFile(capture, "utf8"));
    expect(child.argv).toContain("--pure"); expect(child.argv).not.toContain("--auto");
    expect(child.env.OPENCODE_PERMISSION).toBeUndefined(); expect(child.env.OPENCODE_CONFIG).toBeUndefined();
    expect(child.env.OPENCODE_DISABLE_PROJECT_CONFIG).toBe("true");
    expect(Object.keys(child.env).some(key => key.startsWith("POTLUCK_"))).toBe(false);
    expect(JSON.stringify(child)).not.toContain(secret); expect(result.stdout + result.stderr).not.toContain(secret);
    await expect(stat(child.env.OPENCODE_TEST_HOME)).rejects.toMatchObject({ code: "ENOENT" });
    for (const key of ["XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"]) await expect(stat(child.env[key])).rejects.toMatchObject({ code: "ENOENT" });
    expect(await readFile(join(project, "opencode.json"), "utf8")).toBe('{"model":"wrong/model"}');
  });
  it.each([{ window: null }, { window: 8192 }, { badCall: true }, { badResult: true }, { inferred: true }, { acknowledge: false }])("stops failed readiness %j before starting OpenCode", async options => {
    const mock = await gateway(options); const result = await run(mock.baseURL);
    expect(result.code).toBe(1); expect(JSON.stringify(mock.calls)).not.toContain(message);
    await expect(stat(capture)).rejects.toMatchObject({ code: "ENOENT" });
  });
  it.each([{ changedWindow: 8192 }, { upstreamStatus: 503 }])("does not report a successful one-shot after a provider rejection %j", async options => {
    const mock = await gateway(options); const result = await run(mock.baseURL);
    expect(result.code).toBe(1);
    if (options.changedWindow) expect(mock.calls).toHaveLength(2);
  });
  it("treats a prompt beginning with a flag as literal text", async () => {
    const mock = await gateway(); const result = await run(mock.baseURL, "local", "-m");
    expect(result.code).toBe(0);
    const child = JSON.parse(await readFile(capture, "utf8")); expect(child.argv.slice(-2)).toEqual(["--", "-m"]);
  });
});

it("validates OpenCode CLI arguments and loaded context", () => {
  expect(parseArgs(["launch", "opencode", "--scope", "mine", "--message", "hello"]).action).toBe("opencode");
  for (const args of [["launch", "opencode", "--json"], ["launch", "opencode", "--yes"], ["launch", "opencode", "--scope", "pool"]]) expect(() => parseArgs(args)).toThrow();
  for (const window of [undefined, 0, -1, 1.5, 8192, Number.MAX_SAFE_INTEGER + 1]) expect(() => openCodeBudget(window)).toThrow();
  expect(openCodeBudget(32768)).toEqual({ contextWindowTokens: 32768, maxOutputTokens: 4096, maxInputTokens: 28160 });
});
it("accepts redacted debug credentials but rejects routing, permission, model and MCP overrides", () => {
  const expected = openCodeConfig("potluck-test", "test", { baseURL: "http://127.0.0.1:123/v1", key: "temporary" }, openCodeBudget(32768));
  const redacted = structuredClone(expected); redacted.provider["potluck-test"].options.apiKey = "***";
  expect(() => verifyOpenCodeConfig(redacted, expected)).not.toThrow();
  const changes = [
    { model: "other/test" }, { small_model: "other/test" }, { enabled_providers: ["potluck-test", "other"] },
    { permission: { ...expected.permission, bash: "allow" } }, { agent: { build: { permission: { edit: "allow" } } } },
    { mcp: { remote: { type: "remote", url: "https://example.invalid" } } },
    { provider: { "potluck-test": { ...expected.provider["potluck-test"], options: { apiKey: "***", baseURL: "https://example.invalid" } } } },
  ];
  for (const change of changes) expect(() => verifyOpenCodeConfig({ ...redacted, ...change }, expected)).toThrow();
});
it("counts schemas, tool results, arguments and multibyte text without mutating them", () => {
  const base = { messages: [{ role: "user", content: "hi" }] };
  const tool = { type: "function", function: { name: "read", description: "🙂".repeat(100), parameters: { type: "object" } } };
  expect(toolRequestTokens({ ...base, tools: [tool] })).toBeGreaterThan(toolRequestTokens(base) + 400);
  const history = { messages: [base.messages[0], { role: "assistant", content: null, tool_calls: [{ id: "a", type: "function", function: { name: "read", arguments: '{"path":"file"}' } }] }, { role: "tool", tool_call_id: "a", content: "result" }] };
  const before = JSON.stringify(history); expect(toolRequestTokens(history)).toBeGreaterThan(toolRequestTokens(base)); expect(JSON.stringify(history)).toBe(before);
  for (const body of [{ messages: [{ role: "user", content: [{ type: "image_url", image_url: { url: "x" } }] }] }, { messages: [{ role: "tool", content: "missing id" }] }, { ...base, tools: [{ type: "custom" }] }, { messages: [{ role: "user", content: "hi", hidden: "uncounted" }] }]) expect(() => toolRequestTokens(body)).toThrow();
});

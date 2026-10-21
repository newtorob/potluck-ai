import { chmod, mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";
import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { promisify } from "node:util";
import { UsageError, type CliArgs } from "./args.js";
import { assertGatewayReady, loadCliPrefs, loadConfig, loadSharedPrefs, resolveModel, type Scope } from "./config.js";
import { modelContextWindow } from "./gateway.js";
import { pinnedModel, readProjectModelLock } from "./model-lock.js";
import { RuntimeError } from "./runtime.js";
import { preflightError } from "./launch.js";
import { startIntegrationProxy } from "./integration-proxy.js";

export const OPENCODE_VERSION = "1.19.3";
const execute = promisify(execFile);

export function openCodeBudget(window: number | undefined) {
  if (!window || !Number.isSafeInteger(window)) throw new RuntimeError("Cannot determine the loaded context for OpenCode. Update Potluck and load a model before retrying.");
  if (window < 16384) throw new RuntimeError(`Potluck requires at least 16384 loaded context tokens to launch OpenCode; this model has ${window}. Reload with a larger context. Larger projects and tool definitions may need more.`);
  return { contextWindowTokens: window, maxOutputTokens: 4096, maxInputTokens: window - 4096 - 512 };
}

/** Prove a tool-call/result round trip without reading the project or executing a tool. */
export async function probeOpenCodeTools(proxy: { baseURL: string; key: string }, model: string) {
  const marker = "POTLUCK_TOOL_OK";
  const tools = [{ type: "function", function: { name: "potluck_probe", description: "Echo a readiness marker.",
    parameters: { type: "object", properties: { value: { type: "string", enum: [marker] } }, required: ["value"], additionalProperties: false } } }];
  const messages: unknown[] = [{ role: "user", content: `Call potluck_probe with value ${marker}. /no_think` }];
  const request = async (extra: object) => {
    const response = await fetch(`${proxy.baseURL}/chat/completions`, { method: "POST", redirect: "error",
      signal: AbortSignal.timeout(60_000), headers: { "Content-Type": "application/json", Authorization: `Bearer ${proxy.key}` },
      body: JSON.stringify({ model, messages, tools, stream: false, max_tokens: 128, temperature: 0, ...extra }) });
    if (!response.ok) { await response.body?.cancel(); throw new Error("Readiness request rejected"); }
    const data = await response.json() as any;
    if (data.error || data.potluck?.finish_reason_inferred || data.choices?.length !== 1) throw new Error("Incomplete readiness reply");
    return data.choices[0];
  };
  try {
    const first = await request({ tool_choice: { type: "function", function: { name: "potluck_probe" } } });
    const call = first.message?.tool_calls?.[0];
    if (first.finish_reason !== "tool_calls" || first.message?.tool_calls?.length !== 1 || call?.type !== "function"
      || typeof call.id !== "string" || !call.id || call.function?.name !== "potluck_probe"
      || JSON.parse(call.function.arguments).value !== marker) throw new Error("Tool call did not match probe");
    messages.push({ role: "assistant", content: first.message.content ?? null, tool_calls: [call] },
      { role: "tool", tool_call_id: call.id, content: marker },
      { role: "user", content: `Reply with only ${marker}. /no_think` });
    const last = await request({ tool_choice: "none" });
    if (last.finish_reason !== "stop" || last.message?.content?.trim() !== marker || last.message?.tool_calls?.length) {
      throw new Error("Tool result was not acknowledged");
    }
  } catch {
    throw new RuntimeError("The selected model did not complete the native tool-call readiness check. Load a tool-capable model on the selected route and retry. No project content was sent and no tool was executed.");
  }
}

export function openCodeConfig(provider: string, model: string, proxy: { baseURL: string; key: string }, budget: ReturnType<typeof openCodeBudget>) {
  const selected = `${provider}/${model}`;
  return {
    enabled_providers: [provider], model: selected, small_model: selected, default_agent: "build",
    share: "disabled", autoupdate: false, plugin: [], mcp: {},
    permission: { "*": "ask", read: "allow", glob: "allow", grep: "allow", list: "allow", webfetch: "deny", websearch: "deny" },
    provider: { [provider]: { npm: "@ai-sdk/openai-compatible", name: "Potluck", options: { baseURL: proxy.baseURL, apiKey: proxy.key },
      models: { [model]: { name: model, tool_call: true,
        limit: { context: budget.contextWindowTokens, input: budget.maxInputTokens, output: budget.maxOutputTokens } } } } },
  };
}

/** Managed/user overrides must not redirect the launch or enable unattended writes. */
export function verifyOpenCodeConfig(actual: any, expected: ReturnType<typeof openCodeConfig>) {
  const id = expected.enabled_providers[0];
  const mismatch = actual?.model !== expected.model || actual?.small_model !== expected.small_model
    || actual?.default_agent !== "build" || actual?.share !== "disabled"
    || JSON.stringify(actual?.enabled_providers) !== JSON.stringify([id]) || actual?.disabled_providers?.includes(id)
    || Object.entries(expected.permission).some(([key, value]) => actual?.permission?.[key] !== value)
    || Object.keys(actual?.permission ?? {}).length !== Object.keys(expected.permission).length
    || Object.values(actual?.mcp ?? {}).some((entry: any) => entry?.enabled !== false)
    || Object.keys(actual?.agent ?? {}).length > 0 || Object.keys(actual?.mode ?? {}).length > 0;
  const provider = actual?.provider?.[id];
  const target = expected.provider[id];
  if (mismatch || provider?.npm !== target.npm || provider?.options?.baseURL !== target.options.baseURL
    // OpenCode redacts apiKey in debug output; the bridge still authenticates
    // the actual credential before forwarding any content.
    || ![target.options.apiKey, "***"].includes(provider?.options?.apiKey) || Object.keys(provider?.options?.headers ?? {}).length
    || Object.keys(provider?.options ?? {}).some(key => !["baseURL", "apiKey"].includes(key))
    || JSON.stringify(provider?.models) !== JSON.stringify(target.models)) {
    throw new RuntimeError("OpenCode configuration overrides conflict with this Potluck launch. Check managed or home OpenCode settings; no project request was sent.");
  }
}

export async function launchOpenCode(args: CliArgs): Promise<number> {
  if (!process.stdin.isTTY && !args.message) throw new UsageError("Use an interactive terminal, or launch opencode --message <prompt> for a single request.");
  const binary = process.env.POTLUCK_OPENCODE_BIN || "opencode";
  if (process.env.POTLUCK_OPENCODE_BIN && !isAbsolute(binary)) throw new UsageError("POTLUCK_OPENCODE_BIN must be an absolute executable path.");
  const inheritedEnv: NodeJS.ProcessEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !key.startsWith("POTLUCK_") && !key.startsWith("OPENCODE_")));
  let version: string;
  try { version = (await execute(binary, ["--version"], { env: inheritedEnv, timeout: 10_000, maxBuffer: 16_384 })).stdout.trim(); }
  catch { throw new RuntimeError(`Could not start OpenCode. Install opencode-ai@${OPENCODE_VERSION} separately or set POTLUCK_OPENCODE_BIN to its executable.`); }
  if (version !== OPENCODE_VERSION) throw new RuntimeError(`This experimental launcher is verified with OpenCode ${OPENCODE_VERSION}; found ${version.replace(/[^\w.+-]/g, "").slice(0,40)}. Use that version via POTLUCK_OPENCODE_BIN.`);
  let stage: "setup" | "context" = "setup";
  const ready = await (async () => {
    try {
      const cfg = await loadConfig(); assertGatewayReady(cfg);
      cfg.modelLock = await readProjectModelLock(process.cwd());
      const prefs = await loadCliPrefs(); const shared = await loadSharedPrefs();
      const scope = (args.scope ?? prefs.scope ?? "local") as Scope;
      if (!["local", "mine"].includes(scope)) throw new UsageError("Launch scope must be local or mine.");
      const model = await resolveModel(cfg, pinnedModel(cfg.modelLock, args.model) ?? prefs.model ?? shared.activeModelId);
      stage = "context";
      return { cfg, model, scope, budget: openCodeBudget(await modelContextWindow(cfg, model, scope)) };
    } catch (error) { throw preflightError(error, stage, "OpenCode"); }
  })();
  const { cfg, model, scope, budget } = ready;
  const directory = await mkdtemp(join(tmpdir(), "potluck-opencode-"));
  let proxy: Awaited<ReturnType<typeof startIntegrationProxy>> | undefined;
  try {
    await chmod(directory, 0o700);
    proxy = await startIntegrationProxy(cfg, model, scope, budget, true);
    await probeOpenCodeTools(proxy, model);
    const providerID = `potluck-${randomBytes(8).toString("hex")}`;
    const config = openCodeConfig(providerID, model, proxy, budget);
    const env: NodeJS.ProcessEnv = { ...inheritedEnv };
    for (const kind of ["CONFIG", "DATA", "STATE", "CACHE"]) {
      const path = join(directory, kind.toLowerCase()); await mkdir(path, { mode: 0o700 });
      env[`XDG_${kind}_HOME`] = path;
    }
    // The pinned OpenCode build also searches ~/.opencode independently of XDG.
    // Its profile-home override prevents loading or modifying that directory.
    const profileHome = join(directory, "home"); await mkdir(profileHome, { mode: 0o700 });
    Object.assign(env, { OPENCODE_CONFIG_CONTENT: JSON.stringify(config), OPENCODE_DISABLE_PROJECT_CONFIG: "true",
      OPENCODE_TEST_HOME: profileHome,
      OPENCODE_PURE: "true", OPENCODE_DISABLE_AUTOUPDATE: "true", OPENCODE_DISABLE_MODELS_FETCH: "true",
      OPENCODE_DISABLE_CLAUDE_CODE: "true", OPENCODE_DISABLE_LSP_DOWNLOAD: "true" });
    try {
      const resolved = await execute(binary, ["--pure", "debug", "config"], { env, timeout: 30_000, maxBuffer: 2 * 1024 * 1024 });
      verifyOpenCodeConfig(JSON.parse(resolved.stdout), config);
    } catch (error) {
      if (error instanceof RuntimeError) throw error;
      throw new RuntimeError("Could not verify OpenCode's effective configuration. Check the installed version and managed settings before retrying.");
    }
    const argv = ["--pure", ...(args.message ? ["run"] : []), "--model", config.model, "--agent", "build",
      ...(args.message ? ["--", args.message] : [])];
    process.stderr.write(`Launching OpenCode ${version} with ${model}, scope ${scope}${cfg.modelLock ? ", project model locked" : ""}; loaded context ${budget.contextWindowTokens}, output cap ${budget.maxOutputTokens}. Edits and commands require OpenCode approval; headless requests cannot grant it. Session data is temporary.\n`);
    const bridge = proxy;
    return await new Promise<number>((resolve, reject) => {
      const child = spawn(binary, argv, { env, stdio: [args.message ? "ignore" : "inherit", "inherit", "inherit"] });
      const interrupt = () => { child.kill("SIGINT"); };
      const terminate = () => { child.kill("SIGTERM"); };
      const detach = () => { process.off("SIGINT", interrupt); process.off("SIGTERM", terminate); };
      process.on("SIGINT", interrupt); process.on("SIGTERM", terminate);
      child.once("error", () => { detach(); reject(new RuntimeError("OpenCode could not start. Check POTLUCK_OPENCODE_BIN and retry.")); });
      child.once("close", (code, signal) => { detach(); resolve(code === 0 && args.message && bridge.requestRejected ? 1 : code ?? (signal === "SIGTERM" ? 143 : signal ? 130 : 1)); });
    });
  } finally { try { await proxy?.close(); } finally { await rm(directory, { recursive: true, force: true }); } }
}

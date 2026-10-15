import { mkdtemp, chmod, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, isAbsolute } from "node:path";
import { promisify } from "node:util";
import { execFile, spawn } from "node:child_process";
import type { CliArgs } from "./args.js";
import { UsageError } from "./args.js";
import { assertGatewayReady, GatewayDisabledError, loadConfig, loadCliPrefs, loadSharedPrefs, resolveModel, type GatewayConfig, type Scope } from "./config.js";
import { GatewayContextError, GatewayRefusedError, modelContextWindow, streamChat } from "./gateway.js";
import { ModelLockError, pinnedModel, readProjectModelLock } from "./model-lock.js";
import { RuntimeError } from "./runtime.js";
import { startIntegrationProxy } from "./integration-proxy.js";
import { AiderContextError, planAiderContext, type AiderContextBudget } from "./aider-context.js";

/** Keep useful setup guidance without reflecting untrusted gateway bodies. */
function preflightError(error: unknown, stage: "setup" | "context" | "probe"): Error {
  if (error instanceof ModelLockError || error instanceof UsageError || error instanceof RuntimeError) return error;
  if (error instanceof GatewayDisabledError || error instanceof AiderContextError) return new RuntimeError(error.message);
  const message = error instanceof Error ? error.message : "";
  const status = error instanceof GatewayContextError || error instanceof GatewayRefusedError ? error.status : undefined;
  if (status === 401 || status === 403) {
    return new RuntimeError("Potluck gateway authentication failed. Check Settings -> Connect tools or POTLUCK_GATEWAY_KEY, then retry.");
  }
  if (status === 409) {
    return new RuntimeError("The selected machine's loaded model conflicts with the project model lock. Load the exact pinned GGUF and retry.");
  }
  if (status === 404 || status === 503) {
    return new RuntimeError("The selected model is not loaded or reachable in this scope. Load it on the selected machine, check potluck doctor, and retry.");
  }
  if (message.includes("does not support project model locks") || message.includes("did not acknowledge the exact model lock")) {
    return new RuntimeError("The runtime cannot verify the project model lock. Update Potluck on the local and serving machines, then retry.");
  }
  if (/\bpin(?:ned)?\b|\bsha-?256\b|\bmodel (?:identity|lock|hash)\b/i.test(message)) {
    return new RuntimeError("The selected machine's loaded model conflicts with the project model lock. Load the exact pinned GGUF and retry.");
  }
  if (message.includes("different model or scope")) {
    return new RuntimeError("The gateway returned context for a different model or scope. Check the selected route and update Potluck before retrying.");
  }
  if (message.startsWith("No models are installed")) {
    return new RuntimeError("No model is available. Load a model in Potluck or pass --model <id>, then retry.");
  }
  if (stage === "context") {
    return new RuntimeError("Could not verify the selected model's loaded context. Check potluck doctor and update Potluck on the local and serving machines, then retry.");
  }
  if (stage === "probe") {
    return new RuntimeError("The selected model did not complete a usable readiness reply. Check potluck doctor and update Potluck on the local and serving machines, then retry.");
  }
  return new RuntimeError("Could not prepare Aider's Potluck connection. Check gateway settings, load a model, and run potluck doctor before retrying.");
}

async function prepareAider(args: CliArgs): Promise<{ cfg: GatewayConfig; model: string; scope: Scope; budget: AiderContextBudget }> {
  let stage: "setup" | "context" | "probe" = "setup";
  try {
    const cfg = await loadConfig(); assertGatewayReady(cfg);
    cfg.modelLock = await readProjectModelLock(process.cwd());
    const prefs = await loadCliPrefs(); const shared = await loadSharedPrefs();
    const scope = (args.scope ?? prefs.scope ?? "local") as Scope;
    if (!["local", "mine"].includes(scope)) throw new UsageError("Launch scope must be local or mine.");
    const model = await resolveModel(cfg, pinnedModel(cfg.modelLock, args.model) ?? prefs.model ?? shared.activeModelId);
    stage = "context";
    const budget = planAiderContext(await modelContextWindow(cfg, model, scope));
    // These checks send no project content. An explicit length finish is valid
    // for this deliberately tiny probe, but EOF and inferred success are not.
    stage = "probe";
    let response = "";
    for await (const token of streamChat(cfg, [{ role: "user", content: "Reply OK. /no_think" }], {
      model, scope, maxTokens: 16, requireSuccessfulFinish: true, allowLengthFinish: true,
      signal: AbortSignal.timeout(60000),
    })) { response += token; }
    if (!response.trim()) throw new RuntimeError("The selected model returned no usable text during the readiness check. Load a compatible chat model and retry.");
    return { cfg, model, scope, budget };
  } catch (error) { throw preflightError(error, stage); }
}

export async function launchAider(args: CliArgs): Promise<number> {
  if (!process.stdin.isTTY && !args.message) throw new UsageError("Use an interactive terminal, or launch aider --message <prompt> for a single request.");
  const nonInteractiveMessage = Boolean(args.message && !process.stdin.isTTY);
  const binary = process.env.POTLUCK_AIDER_BIN || "aider";
  if (process.env.POTLUCK_AIDER_BIN && !isAbsolute(binary)) throw new UsageError("POTLUCK_AIDER_BIN must be an absolute executable path.");
  try { await promisify(execFile)(binary, ["--version"], { timeout: 10000, maxBuffer: 16384 }); }
  catch { throw new RuntimeError("Could not start Aider. Install aider-chat separately or set POTLUCK_AIDER_BIN to its executable."); }
  const { cfg, model, scope, budget } = await prepareAider(args);
  let proxy: Awaited<ReturnType<typeof startIntegrationProxy>> | undefined;
  const directory = await mkdtemp(join(tmpdir(), "potluck-aider-"));
  try {
    await chmod(directory, 0o700);
    proxy = await startIntegrationProxy(cfg, model, scope, budget);
    // Explicit files are loaded last by Aider. Preserve the user's files while
    // preventing existing .env/model settings from changing this launch's provider.
    const envFile = join(directory, "provider.env");
    await writeFile(envFile, `OPENAI_API_KEY=${proxy.key}\nAIDER_OPENAI_API_KEY=${proxy.key}\nOPENAI_API_BASE=${proxy.baseURL}\n`, { mode: 0o600 });
    const modelSettings = join(directory, "model-settings.json");
    const extraParams = { api_base: proxy.baseURL, api_key: proxy.key, max_tokens: budget.maxOutputTokens };
    await writeFile(modelSettings, JSON.stringify([{ name: `openai/${model}`, weak_model_name: `openai/${model}`,
      editor_model_name: `openai/${model}`, edit_format: "whole", use_repo_map: true,
      extra_params: extraParams }, { name: "aider/extra_params", extra_params: extraParams }]), { mode: 0o600 });
    const modelMetadata = join(directory, "model-metadata.json");
    await writeFile(modelMetadata, JSON.stringify({ [`openai/${model}`]: {
      max_input_tokens: budget.maxInputTokens, max_output_tokens: budget.maxOutputTokens,
      max_tokens: budget.maxOutputTokens, litellm_provider: "openai", mode: "chat",
      input_cost_per_token: 0, output_cost_per_token: 0,
    } }), { mode: 0o600 });
    const argv = ["--model", `openai/${model}`, "--weak-model", `openai/${model}`, "--editor-model", `openai/${model}`,
      "--openai-api-base", proxy.baseURL, "--env-file", envFile, "--model-settings-file", modelSettings,
      "--model-metadata-file", modelMetadata, "--max-chat-history-tokens", String(budget.maxHistoryTokens),
      "--map-tokens", String(budget.mapTokens), "--map-multiplier-no-files", "1",
      "--no-analytics", "--no-check-update", "--no-show-model-warnings", "--no-show-release-notes", "--no-auto-commits", "--no-dirty-commits",
      // A single-message launch without a terminal cannot answer this setup prompt.
      ...(nonInteractiveMessage ? ["--no-gitignore"] : []),
      ...(args.message ? ["--message", args.message] : [])];
    process.stderr.write(`Launching Aider with ${model}, scope ${scope}${cfg.modelLock ? ", project model locked" : ""}; loaded context ${budget.contextWindowTokens} tokens, output ${budget.maxOutputTokens}, history ${budget.maxHistoryTokens}, repo map ${budget.mapTokens}.\n`);
    const provider = proxy;
    const inheritedEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith("POTLUCK_")));
    return await new Promise<number>((resolve, reject) => {
      const child = spawn(binary, argv, { stdio: [nonInteractiveMessage ? "ignore" : "inherit", "inherit", "inherit"], env: { ...inheritedEnv,
        OPENAI_API_BASE: provider.baseURL, OPENAI_API_KEY: provider.key, AIDER_OPENAI_API_KEY: provider.key,
        LITELLM_LOCAL_MODEL_COST_MAP: "True" } });
      // The foreground child receives terminal SIGINT too. Keep the bridge alive
      // while it shuts down, then remove the temporary credentials.
      const forward = () => { child.kill("SIGINT"); };
      process.on("SIGINT", forward);
      child.once("error", () => { process.off("SIGINT", forward); reject(new RuntimeError("Could not start Aider. Install aider-chat separately or set POTLUCK_AIDER_BIN to its executable.")); });
      child.once("close", (code, signal) => {
        process.off("SIGINT", forward);
        // Aider may exit zero after displaying a provider error. A rejected
        // one-shot request must not look successful to scripts invoking us.
        resolve(code === 0 && args.message && provider.requestRejected ? 1 : code ?? (signal ? 130 : 1));
      });
    });
  } finally { try { await proxy?.close(); } finally { await rm(directory, { recursive: true, force: true }); } }
}

import { mkdtemp, chmod, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, isAbsolute } from "node:path";
import { promisify } from "node:util";
import { execFile, spawn } from "node:child_process";
import type { CliArgs } from "./args.js";
import { UsageError } from "./args.js";
import { assertGatewayReady, loadConfig, loadCliPrefs, loadSharedPrefs, resolveModel, type Scope } from "./config.js";
import { streamChat } from "./gateway.js";
import { pinnedModel, readProjectModelLock } from "./model-lock.js";
import { RuntimeError } from "./runtime.js";
import { startIntegrationProxy } from "./integration-proxy.js";

export async function launchAider(args: CliArgs): Promise<number> {
  if (!process.stdin.isTTY && !args.message) throw new UsageError("Use an interactive terminal, or launch aider --message <prompt> for a single request.");
  const binary = process.env.POTLUCK_AIDER_BIN || "aider";
  if (process.env.POTLUCK_AIDER_BIN && !isAbsolute(binary)) throw new UsageError("POTLUCK_AIDER_BIN must be an absolute executable path.");
  try { await promisify(execFile)(binary, ["--version"], { timeout: 10000, maxBuffer: 16384 }); }
  catch { throw new RuntimeError("Could not start Aider. Install aider-chat separately or set POTLUCK_AIDER_BIN to its executable."); }
  const cfg = await loadConfig(); assertGatewayReady(cfg);
  cfg.modelLock = await readProjectModelLock(process.cwd());
  const prefs = await loadCliPrefs(); const shared = await loadSharedPrefs();
  const scope = (args.scope ?? prefs.scope ?? "local") as Scope;
  if (!["local", "mine"].includes(scope)) throw new UsageError("Launch scope must be local or mine.");
  const model = await resolveModel(cfg, pinnedModel(cfg.modelLock, args.model) ?? prefs.model ?? shared.activeModelId);
  // A synthetic probe catches old runtimes, unloaded models and pin mismatches
  // before the external tool gets project content or begins editing.
  try {
    let response = "";
    for await (const token of streamChat(cfg, [{ role: "user", content: "Reply OK. /no_think" }], {
      model, scope, maxTokens: 16, signal: AbortSignal.timeout(60000),
    })) { response += token; }
    if (!response.trim()) throw new Error("No model output");
  } catch { throw new RuntimeError("The selected model is not ready for this integration. Run potluck doctor or setup, then retry."); }
  let proxy: Awaited<ReturnType<typeof startIntegrationProxy>> | undefined;
  const directory = await mkdtemp(join(tmpdir(), "potluck-aider-"));
  try {
    await chmod(directory, 0o700);
    proxy = await startIntegrationProxy(cfg, model, scope);
    // Explicit files are loaded last by Aider. Preserve the user's files while
    // preventing existing .env/model settings from changing this launch's provider.
    const envFile = join(directory, "provider.env");
    await writeFile(envFile, `OPENAI_API_KEY=${proxy.key}\nAIDER_OPENAI_API_KEY=${proxy.key}\nOPENAI_API_BASE=${proxy.baseURL}\n`, { mode: 0o600 });
    const modelSettings = join(directory, "model-settings.json");
    await writeFile(modelSettings, JSON.stringify([{ name: `openai/${model}`, weak_model_name: `openai/${model}`,
      editor_model_name: `openai/${model}`, edit_format: "whole", use_repo_map: true,
      extra_params: { api_base: proxy.baseURL, api_key: proxy.key, max_tokens: 1024 } }]), { mode: 0o600 });
    const argv = ["--model", `openai/${model}`, "--weak-model", `openai/${model}`, "--editor-model", `openai/${model}`,
      "--openai-api-base", proxy.baseURL, "--env-file", envFile, "--model-settings-file", modelSettings,
      "--no-analytics", "--no-check-update", "--no-show-model-warnings", "--no-show-release-notes", "--no-auto-commits", "--no-dirty-commits",
      ...(args.message ? ["--message", args.message] : [])];
    process.stderr.write(`Launching Aider with ${model}, scope ${scope}${cfg.modelLock ? ", project model locked" : ""}.\n`);
    const provider = proxy;
    const inheritedEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith("POTLUCK_")));
    return await new Promise<number>((resolve, reject) => {
      const child = spawn(binary, argv, { stdio: [args.message && !process.stdin.isTTY ? "ignore" : "inherit", "inherit", "inherit"], env: { ...inheritedEnv,
        OPENAI_API_BASE: provider.baseURL, OPENAI_API_KEY: provider.key, AIDER_OPENAI_API_KEY: provider.key,
        LITELLM_LOCAL_MODEL_COST_MAP: "True" } });
      // The foreground child receives terminal SIGINT too. Keep the bridge alive
      // while it shuts down, then remove the temporary credentials.
      const forward = () => { child.kill("SIGINT"); };
      process.on("SIGINT", forward);
      child.once("error", () => { process.off("SIGINT", forward); reject(new RuntimeError("Could not start Aider. Install aider-chat separately or set POTLUCK_AIDER_BIN to its executable.")); });
      child.once("close", (code, signal) => { process.off("SIGINT", forward); resolve(code ?? (signal ? 130 : 1)); });
    });
  } finally { try { await proxy?.close(); } finally { await rm(directory, { recursive: true, force: true }); } }
}

import type { Readable } from "node:stream";
import { type CliArgs, UsageError } from "./args.js";
import { assertGatewayReady, GatewayDisabledError, loadCliPrefs, loadConfig, loadSharedPrefs } from "./config.js";
import { GatewayRefusedError, listModels, streamChat } from "./gateway.js";
import { ModelLockError, pinnedModel, readProjectModelLock } from "./model-lock.js";

const MAX_INPUT = 1024 * 1024;
const MAX_OUTPUT = 4 * 1024 * 1024;
export async function readPrompt(prompt: string | undefined, input: Readable & { isTTY?: boolean }, signal: AbortSignal): Promise<string> {
  let size = Buffer.byteLength(prompt || ""); const chunks: Buffer[] = [];
  if (!input.isTTY) {
    const cancel = () => { input.destroy(new Error("Input cancelled.")); };
    signal.throwIfAborted(); signal.addEventListener("abort", cancel, { once: true });
    try {
      for await (const chunk of input) {
        const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        size += bytes.length; if (size > MAX_INPUT) throw new UsageError("Prompt and stdin must total at most 1 MiB.");
        chunks.push(bytes);
      }
    } finally { signal.removeEventListener("abort", cancel); }
  }
  if (size > MAX_INPUT) throw new UsageError("Prompt and stdin must total at most 1 MiB.");
  const text = [prompt, Buffer.concat(chunks).toString("utf8")].filter(Boolean).join("\n\n");
  if (!text.trim()) throw new UsageError('Pass a prompt or pipe text to potluck run.');
  return text;
}

export async function runInference(args: CliArgs, out: (text: string) => void): Promise<number> {
  const controller = new AbortController(); let interrupted = false, timedOut = false, brokenPipe = false;
  const interrupt = () => { interrupted = true; controller.abort(); };
  const outputError = (error: NodeJS.ErrnoException) => { if (error.code === "EPIPE") brokenPipe = true; controller.abort(); };
  process.on("SIGINT", interrupt); process.stdout.on("error", outputError);
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, args.timeout);
  const signal = controller.signal;
  let scope = args.scope ?? "local";
  try {
    const prompt = await readPrompt(args.task, process.stdin, signal);
    const cfg = await loadConfig(); assertGatewayReady(cfg);
    cfg.modelLock = await readProjectModelLock(process.cwd());
    const [prefs, shared] = await Promise.all([loadCliPrefs(), loadSharedPrefs()]);
    const model = pinnedModel(cfg.modelLock, args.model) ?? prefs.model ?? shared.activeModelId ?? (await listModels(cfg, signal))[0]?.id;
    if (!model) throw new UsageError("No model selected. Run potluck setup or pass --model.");
    scope = args.scope ?? prefs.scope ?? "local";
    if (scope === "circle" && cfg.modelLock) throw new UsageError("This project pins an exact model with potluck.lock, which a trusted circle cannot guarantee. Use --scope local or mine here, or run outside the project.");
    const start = performance.now(); let first: number | null = null, text = "", size = 0;
    for await (const token of streamChat(cfg, [{ role: "user", content: prompt }], {
      model, scope, maxTokens: args.maxTokens ?? 2048, signal, requireDone: true,
      ...(args.circle !== undefined ? { circle: args.circle } : {}),
    })) {
      first ??= performance.now() - start;
      size += Buffer.byteLength(token); if (size > MAX_OUTPUT) throw new Error("Output limit exceeded.");
      if (args.json) text += token; else out(token);
    }
    if (first === null) throw new Error("No model output.");
    if (args.json) out(JSON.stringify({ schemaVersion: 1, model, scope, sha256: cfg.modelLock?.model.sha256 ?? null,
      text, durationMs: Math.round(performance.now() - start), timeToFirstTokenMs: Math.round(first) }) + "\n");
    else out("\n");
    return 0;
  } catch (error) {
    if (brokenPipe) return 0;
    const code = interrupted ? "cancelled" : timedOut ? "timeout" : error instanceof UsageError ? "invalid_input" : "inference_failed";
    const message = interrupted ? "Request cancelled." : timedOut ? "Request timed out." :
      error instanceof UsageError || error instanceof ModelLockError || error instanceof GatewayDisabledError ? error.message :
      // A circle request is refused for reasons only the gateway can name:
      // not signed in, which circle, nobody online. Say them.
      scope === "circle" && error instanceof GatewayRefusedError && error.detail ? error.detail : "Inference failed or the stream ended early. Run potluck doctor to check the selected model and gateway.";
    if (args.json) out(JSON.stringify({ schemaVersion: 1, error: { code, message } }) + "\n");
    else process.stderr.write(message + "\n");
    return interrupted ? 130 : timedOut ? 124 : error instanceof UsageError ? 2 : 1;
  } finally { clearTimeout(timer); process.off("SIGINT", interrupt); process.stdout.off("error", outputError); }
}

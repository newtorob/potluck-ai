/**
 * A stand-in for this machine's Potluck engine: a loopback HTTP server with the
 * routes `potluck benchmark` uses, a private runtime record the CLI discovers,
 * and a log of every request. It keeps real state (installed, loaded), so a
 * test can check what is loaded after a switch and a restore.
 */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

export interface FakeModel { id: string; sizeBytes: number; installed: boolean; fits?: boolean; reasons?: string[];
  /** What an engine that measures adds to a model. Left out, as an older engine does. */
  fitBasis?: "measured" | "estimate"; measured?: Record<string, unknown> | null; checked?: "passed" | "failed" | null }
export interface Call { method: string; path: string; body: unknown; headers: Record<string, string | string[] | undefined> }
export interface BenchResult { output_tokens: number; ttft_ms: number; duration_ms: number; tokens_per_sec: number; prompt_tokens_per_sec: number | null }

const GIB = 1024 ** 3;
export const token = "a".repeat(64);
export const gatewayKey = "sk-potluck-engine-gateway-key";

/** The `acceptance` object of the engine's answer check, as the contract spells it. */
export const acceptanceFixture = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
  schema_version: 1, status: "passed", model_id: "big", level: "quick", checked_at: "2026-10-09T14:32:05Z", from_proof: false,
  ran_on: { device_kind: "gpu", backend: "cuda", device_name: "NVIDIA GeForce RTX 2080 Ti", driver: "560.35" },
  checks: [{ id: "arithmetic", passed: true, required: true }, { id: "echo", passed: true, required: false }],
  verdict: "ok", fallback: null, message: null, ...over });
export const FALLBACK_MESSAGE = "The GPU gave wrong answers for this model, so it is running on the CPU, which is slower. Updating the graphics driver usually fixes this.";
export const FAILED_MESSAGE = "This model did not pass its answer check on this machine. You can still use it yourself. It is not offered to other people.";

export class FakeEngine {
  dir = ""; url = ""; server!: Server;
  instanceId = "00000000-0000-0000-0000-000000000042";
  calls: Call[] = [];
  loaded: string | null = "big";
  models: FakeModel[] = [
    { id: "big", sizeBytes: 5 * GIB, installed: true },
    { id: "small", sizeBytes: 2 * GIB, installed: true },
    { id: "huge", sizeBytes: 60 * GIB, installed: false, fits: false, reasons: ["Needs about 73.5 GiB available RAM and 73.5 GiB total RAM."] },
    { id: "fresh", sizeBytes: 3 * GIB, installed: false },
  ];
  /** The `recommended` list of GET /models/setup-options. Undefined leaves the key out, as an older engine does. */
  recommended: unknown = undefined;
  hardware: Record<string, unknown> = { os: "Darwin", arch: "arm64", cpu_name: "arm", ram_total_gb: 16, ram_available_gb: 8, gpu_name: "Apple M2 Pro" };
  /** Progress states a download walks through before it is complete. */
  downloadStates = ["downloading", "verifying", "complete"];
  /** Downloads in progress, by model ID. A test can add one to stand for someone else's transfer. */
  downloads = new Map<string, string[]>();
  /** Milliseconds a load request takes before it answers. */
  loadDelay = 0;
  /** Result for each POST /models/benchmark call, by call index (0 is the warm-up). */
  bench: (call: number, model: string) => BenchResult = call => FakeEngine.run(call === 0 ? 3600 : 3300 - (call % 2) * 100);
  benchCalls = 0;
  /** Await before answering a benchmark call; tests use it to press Ctrl-C mid-run. */
  onBench?: (call: number) => Promise<void>;
  failLoad = new Set<string>();
  failUnload = new Set<string>();
  /** A failed load that already released the model that was loaded. */
  releaseOnFailedLoad = false;
  inferenceRoute = true;
  /** The `acceptance` object GET /health/inference and a passing POST /models/{id}/test carry. Null leaves the key out, as an older engine does. */
  acceptance: Record<string, unknown> | null = null;
  /** Answer POST /models/{id}/test with this status and body instead of a pass. */
  testRefusal: { status: number; body: unknown } | null = null;
  /** Answer GET /models/setup-options with an error, as an engine that has gone away would. */
  setupFails = false;
  placement: (model: string) => Record<string, unknown> | null = model => ({
    backend_selection: {}, used_n_ctx: 8192, offloaded_layers: 37, total_layers: 37,
    summary: { device_kind: "gpu", device_name: "Apple M2 Pro", backend: "metal", execution_verified: true, model_id: model } });
  devices: Array<Record<string, unknown>> = [{ name: "Metal", description: "Apple M2 Pro", type: "gpu", free_bytes: 1, total_bytes: 2 }];
  lastBenchmarks = new Map<string, Record<string, unknown>>();
  gateway: { enabled: boolean; api_key: string } = { enabled: true, api_key: gatewayKey };
  /** GET /models/available rows. */
  available: unknown = null;
  /** What /v1/chat/completions answers: how many chunks, the X-Potluck-Served-By header, and a delay between chunks. */
  chat = { chunks: 5, servedBy: "household" as string | null, status: 200, delay: 0 };

  static run(durationMs: number, ttftMs = 300, tokens = 64, engine = 19.4, prompt: number | null = 240): BenchResult {
    return { output_tokens: tokens, ttft_ms: ttftMs, duration_ms: durationMs, tokens_per_sec: engine, prompt_tokens_per_sec: prompt };
  }

  async start(): Promise<void> {
    this.dir = await mkdtemp(join(tmpdir(), "potluck-bench-"));
    this.server = createServer((req, res) => { void this.handle(req, res).catch(() => { try { res.writeHead(500).end("{}"); } catch { /* client gone */ } }); });
    await new Promise<void>(resolve => this.server.listen(0, "127.0.0.1", resolve));
    this.url = `http://127.0.0.1:${(this.server.address() as { port: number }).port}`;
    await mkdir(join(this.dir, "run"), { recursive: true, mode: 0o700 });
    await writeFile(join(this.dir, "run/runtime.json"), JSON.stringify({ schemaVersion: 1, instanceId: this.instanceId, pid: process.pid, url: this.url,
      owner: "cli", version: "0.1.0", token }), { mode: 0o600 });
  }
  async stop(): Promise<void> {
    this.server.closeAllConnections();
    await new Promise<void>(resolve => this.server.close(() => resolve()));
    await rm(this.dir, { recursive: true, force: true });
  }
  paths(): string[] { return this.calls.map(c => `${c.method} ${c.path}`); }
  count(path: string): number { return this.calls.filter(c => c.path === path).length; }

  private json(res: ServerResponse, value: unknown, status = 200): void {
    res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify(value));
  }
  private setup(): unknown {
    return { schemaVersion: 1, hardware: this.hardware, diskFreeBytes: 100 * GIB, currentModel: this.loaded, recommendedModel: "small",
      models: this.models.map(m => ({ id: m.id, name: m.id.toUpperCase(), sizeBytes: m.sizeBytes, installed: m.installed, loaded: m.id === this.loaded,
        fits: m.fits ?? true, reasons: m.reasons ?? [], estimatedWorkingGiB: 4,
        ...(m.fitBasis ? { fitBasis: m.fitBasis, measured: m.measured ?? null, checked: m.checked ?? null } : {}) })),
      ...(this.recommended !== undefined ? { recommended: this.recommended } : {}) };
  }

  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(chunk as Buffer);
    const raw = Buffer.concat(chunks).toString("utf8");
    const path = (req.url ?? "/").split("?")[0];
    const call: Call = { method: req.method ?? "GET", path, body: raw ? JSON.parse(raw) : undefined, headers: req.headers };
    this.calls.push(call);
    if (path.startsWith("/v1/")) return this.v1(call, res);
    if (path === "/health") return this.json(res, { status: "ok", version: "0.1.0", active_model_id: this.loaded });
    if (req.headers.authorization !== `Bearer ${token}`) return this.json(res, { detail: "secret traceback from the engine" }, 401);
    if (path === "/control") return this.json(res, { schemaVersion: 1, instanceId: this.instanceId, pid: process.pid, owner: "cli", url: this.url });
    if (path === "/settings") return this.json(res, { gateway: this.gateway, runtime: {}, ui: {} });
    if (path === "/models/setup-options") return this.setupFails ? this.json(res, { detail: "secret traceback from the engine" }, 500) : this.json(res, this.setup());
    if (path === "/models/available") return this.json(res, this.available ?? { schemaVersion: 1, scope: "mine", checkedAt: 1, local: { model: this.loaded, sha256: null }, models: [], machines: [] });
    if (path === "/health/inference") {
      if (!this.inferenceRoute) return this.json(res, { detail: "not found" }, 404);
      const bench = this.loaded ? this.lastBenchmarks.get(this.loaded) ?? null : null;
      return this.json(res, { build: { backend: "metal", lib_dir: "/private/secret/lib" }, devices: this.devices,
        loaded: this.loaded ? this.placement(this.loaded) : null, benchmark: bench, ...(this.acceptance ? { acceptance: this.acceptance } : {}) });
    }
    const test = /^\/models\/([^/]+)\/test$/.exec(path);
    if (test && call.method === "POST") {
      const id = decodeURIComponent(test[1]);
      if (this.testRefusal) return this.json(res, this.testRefusal.body, this.testRefusal.status);
      if (this.loaded !== id) return this.json(res, { detail: "Requested model is no longer loaded. Load the requested model and retry." }, 409);
      return this.json(res, { schemaVersion: 1, status: "passed", model: id, durationMs: 25, outputTokens: 8, scope: "local", ...(this.acceptance ? { acceptance: this.acceptance } : {}) });
    }
    if (path === "/models/benchmark") {
      if (!this.loaded) return this.json(res, { detail: "No model loaded to benchmark." }, 503);
      const index = this.benchCalls++;
      await this.onBench?.(index);
      const result = { model_id: this.loaded, ...this.bench(index, this.loaded) };
      this.lastBenchmarks.set(this.loaded, result);
      return this.json(res, result);
    }
    const m = /^\/models\/([^/]+)\/(install|download-progress|cancel-download|load|unload)$/.exec(path);
    if (m) {
      if (m[2] === "load" && this.loadDelay) await new Promise(resolve => setTimeout(resolve, this.loadDelay));
      return this.modelRoute(decodeURIComponent(m[1]), m[2], res);
    }
    this.json(res, { detail: "not found" }, 404);
  }

  private modelRoute(id: string, action: string, res: ServerResponse): void {
    const model = this.models.find(x => x.id === id);
    if (!model) return this.json(res, { detail: `Model '${id}' not found in registry.` }, 404);
    if (action === "install") {
      if (model.installed) return this.json(res, { status: "already_installed" }, 202);
      if (this.downloads.has(id)) return this.json(res, { status: "already_downloading" }, 202);
      this.downloads.set(id, [...this.downloadStates]);
      return this.json(res, { status: "downloading", slug: id }, 202);
    }
    if (action === "download-progress") {
      const states = this.downloads.get(id);
      if (!states) return this.json(res, { detail: "No download record" }, 404);
      const status = states.length > 1 ? states.shift()! : states[0];
      if (status === "complete") { model.installed = true; this.downloads.delete(id); }
      return this.json(res, { status, bytes_downloaded: status === "complete" ? model.sizeBytes : model.sizeBytes / 2, bytes_total: model.sizeBytes });
    }
    if (action === "cancel-download") { this.downloads.delete(id); return this.json(res, { status: "cancelled" }); }
    if (action === "unload") {
      if (this.failUnload.has(id)) return this.json(res, { detail: "unload failed: secret traceback from the engine" }, 500);
      if (this.loaded === id) this.loaded = null;
      return this.json(res, { status: "unloaded" });
    }
    if (this.failLoad.has(id)) {
      if (this.releaseOnFailedLoad) this.loaded = null;
      return this.json(res, { detail: "load failed: secret traceback from the engine" }, 500);
    }
    if (!model.installed) return this.json(res, { detail: "not installed" }, 404);
    this.loaded = id;
    this.json(res, { status: "loaded", model_id: id });
  }

  private async v1(call: Call, res: ServerResponse): Promise<void> {
    if (call.headers.authorization !== `Bearer ${this.gateway.api_key}`) return this.json(res, { detail: "missing or invalid gateway API key" }, 401);
    if (this.chat.status !== 200) return this.json(res, { detail: "model is not loaded here or on a reachable household peer" }, this.chat.status);
    res.writeHead(200, { "content-type": "text/event-stream", ...(this.chat.servedBy ? { "x-potluck-served-by": this.chat.servedBy } : {}) });
    for (let i = 0; i < this.chat.chunks; i++) {
      if (this.chat.delay) await new Promise(resolve => setTimeout(resolve, this.chat.delay));
      res.write(`data: ${JSON.stringify({ choices: [{ delta: { content: `w${i} ` } }] })}\n\n`);
    }
    res.end("data: [DONE]\n\n");
  }
}

/** A server that records requests and answers like some other gateway (an Ollama tunnel, for instance). */
export class Decoy {
  hits: string[] = [];
  server!: Server; url = "";
  async start(): Promise<void> {
    this.server = createServer((req, res) => { this.hits.push(`${req.method} ${req.url}`); req.resume(); res.writeHead(200, { "content-type": "text/event-stream", "x-potluck-served-by": "household" }).end("data: [DONE]\n\n"); });
    await new Promise<void>(resolve => this.server.listen(0, "127.0.0.1", resolve));
    this.url = `http://127.0.0.1:${(this.server.address() as { port: number }).port}/v1`;
  }
  async stop(): Promise<void> {
    this.server.closeAllConnections();
    await new Promise<void>(resolve => this.server.close(() => resolve()));
  }
}

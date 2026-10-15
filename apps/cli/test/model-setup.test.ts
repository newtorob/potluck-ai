import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { parseRecommended, recommendedLines, setupModel, type SetupModel } from "../src/model-setup.js";
import { runtimeRequest } from "../src/runtime.js";
import { readProjectModelLock } from "../src/model-lock.js";
import { loadCliPrefs, saveCliPrefs } from "../src/config.js";
vi.mock("../src/runtime.js", async original => ({ ...await original<typeof import("../src/runtime.js")>(), runtimeRequest: vi.fn() }));
vi.mock("../src/model-lock.js", async original => ({ ...await original<typeof import("../src/model-lock.js")>(), readProjectModelLock: vi.fn() }));
vi.mock("../src/config.js", () => ({ loadCliPrefs: vi.fn(), saveCliPrefs: vi.fn() }));
const hash = "a".repeat(64);
const model = { id: "small", name: "Small", sizeBytes: 1024 ** 3, fits: true, installed: false, loaded: false, reasons: [], estimatedWorkingGiB: 2.7 };
const base = { schemaVersion: 1, hardware: { ram_total_gb: 16, ram_available_gb: 8 }, diskFreeBytes: 100 * 1024 ** 3,
  currentModel: null, recommendedModel: "small", models: [model] };
const deps = { wait: async () => {}, now: () => 0 };
const request = vi.mocked(runtimeRequest);
beforeEach(() => { vi.mocked(loadCliPrefs).mockResolvedValue({ scope: "mine", verify: "npm test" }); });
afterEach(() => vi.resetAllMocks());
function serve(options = base, states = ["downloading", "verifying", "complete"]) {
  request.mockImplementation(async (route, opts) => {
    if (route.startsWith("/models/setup-options")) return options as never;
    if (route.endsWith("/install")) return { status: "downloading" } as never;
    if (route.endsWith("/download-progress")) return { status: states.shift() ?? "complete", bytes_downloaded: 1024 ** 3, bytes_total: 1024 ** 3 } as never;
    if (route.endsWith("/test")) return { status: "passed", model: "small", scope: "local", durationMs: 25 } as never;
    return { status: "loaded" } as never;
  });
}
it("takes an explicit unattended request through download, verification, loading and actual response before saving preferences", async () => {
  serve();
  const result = await setupModel(parseArgs(["setup", "--model", "small", "--no-input"]), undefined, deps);
  expect(result).toMatchObject({ status: "ready", model: "small", scope: "local" });
  expect(request.mock.calls.map(c => c[0])).toEqual(["/models/setup-options", "/models/small/install", "/models/small/download-progress",
    "/models/small/download-progress", "/models/small/download-progress", "/models/small/load", "/models/small/test"]);
  expect(saveCliPrefs).toHaveBeenCalledWith({ scope: "mine", verify: "npm test", model: "small" });
});
it("declining a download changes neither model nor preferences", async () => {
  serve(); const ask = vi.fn().mockResolvedValueOnce("").mockResolvedValueOnce("").mockResolvedValueOnce("no");
  expect(await setupModel(parseArgs(["setup"]), { ask, progress: vi.fn() }, deps)).toEqual({ status: "skipped" });
  expect(request).toHaveBeenCalledTimes(1); expect(saveCliPrefs).not.toHaveBeenCalled();
});
it("reuses an already loaded model without another download or model swap", async () => {
  serve({ ...base, currentModel: "small" as never, models: [{ ...model, installed: true, loaded: true }] });
  await setupModel(parseArgs(["setup", "--with-model"]), undefined, deps);
  expect(request.mock.calls.map(c => c[0])).toEqual(["/models/setup-options", "/models/small/test"]);
});
it("does not silently replace a pin and verifies it before and after loading", async () => {
  vi.mocked(readProjectModelLock).mockResolvedValue({ schemaVersion: 1, path: "/project/potluck.lock", model: { id: "small", format: "gguf", sizeBytes: model.sizeBytes, sha256: hash } });
  serve({ ...base, recommendedModel: "other", models: [{ ...model, installed: true }] });
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (route, opts) => route.endsWith("/install-locked")
    ? { model_lock_version: 1, status: "verified", sha256: hash } as never : original(route, opts));
  await setupModel(parseArgs(["setup", "--with-model"]), undefined, deps);
  expect(request).toHaveBeenCalledWith("/models/small/test", expect.objectContaining({ body: { sha256: hash } }));
  expect(saveCliPrefs).not.toHaveBeenCalled();
  await expect(setupModel(parseArgs(["setup", "--model", "other"]), undefined, deps)).rejects.toThrow("pins small");
});
it.each(["error", "cancelled"])("does not load or save after a failed download: %s", async state => {
  serve(base, [state]);
  await expect(setupModel(parseArgs(["setup", "--with-model"]), undefined, deps)).rejects.toThrow("Download");
  expect(request.mock.calls.some(c => c[0].endsWith("/load"))).toBe(false); expect(saveCliPrefs).not.toHaveBeenCalled();
});
it("refuses insufficient memory/disk before starting a download", async () => {
  serve({ ...base, models: [{ ...model, fits: false, reasons: ["Insufficient RAM"] }] });
  await expect(setupModel(parseArgs(["setup", "--model", "small"]), undefined, deps)).rejects.toThrow("Insufficient RAM");
  expect(request).toHaveBeenCalledTimes(1);
});
it("cancels only the download this setup started and preserves preferences on interrupt", async () => {
  serve(); const abort = new AbortController();
  await expect(setupModel(parseArgs(["setup", "--model", "small"]), { ask: vi.fn(), progress: vi.fn(), signal: abort.signal },
    { now: () => 0, wait: async () => { abort.abort(); } })).rejects.toThrow("interrupted");
  expect(request).toHaveBeenCalledWith("/models/small/cancel-download", expect.anything());
  expect(saveCliPrefs).not.toHaveBeenCalled();
});
it("does not report readiness or save preferences when inference returns no success", async () => {
  serve(); const original = request.getMockImplementation()!;
  request.mockImplementation(async (route, opts) => route.endsWith("/test") ? { status: "error" } as never : original(route, opts));
  await expect(setupModel(parseArgs(["setup", "--model", "small"]), undefined, deps)).rejects.toThrow("local response check");
  expect(saveCliPrefs).not.toHaveBeenCalled();
});
it("follows a preexisting locked download without cancelling it, then verifies exact bytes", async () => {
  const { ControlError } = await import("../src/control.js");
  vi.mocked(readProjectModelLock).mockResolvedValue({ schemaVersion: 1, path: "/project/potluck.lock", model: { id: "small", format: "gguf", sizeBytes: model.sizeBytes, sha256: hash } });
  serve(base, ["downloading", "complete"]);
  const original = request.getMockImplementation()!; let installs = 0;
  request.mockImplementation(async (route, opts) => {
    if (route.endsWith("/install-locked")) {
      if (++installs === 1) throw new ControlError("http_error", "busy", 409);
      return { model_lock_version: 1, status: "verified", sha256: hash } as never;
    }
    return original(route, opts);
  });
  expect((await setupModel(parseArgs(["setup", "--with-model"]), undefined, deps)).status).toBe("ready");
  expect(installs).toBe(2);
  expect(request.mock.calls.some(c => c[0].endsWith("/cancel-download"))).toBe(false);
});

const GIB = 1024 ** 3;
const sized = (id: string, gib: number, over: Partial<SetupModel> = {}): SetupModel => ({ ...model, id, sizeBytes: gib * GIB, ...over });
it("shows the engine's picks before the list in a terminal, and returns them in the result", async () => {
  const picks = [{ id: "big", basis: "proven_fast", tokensPerSecond: 45 }, { id: "small", basis: "estimate", tokensPerSecond: null }];
  serve({ ...base, recommended: picks, models: [sized("big", 5, { installed: true, fitBasis: "measured", checked: "passed",
    measured: { hostGiB: 5.2, gpuGiB: null, contextTokens: 4096, tokensPerSecond: 45, checkedAt: null } }), sized("small", 1)] } as never);
  const said: string[] = [], ask = vi.fn().mockResolvedValueOnce("").mockResolvedValueOnce("small").mockResolvedValueOnce("n");
  const result = await setupModel(parseArgs(["setup"]), { ask, progress: m => { said.push(m); } }, deps);
  expect(result).toEqual({ status: "skipped", recommended: picks });
  const out = said.join("");
  expect(out.indexOf("Recommended for this machine:")).toBeGreaterThan(-1);
  expect(out).toContain("  big (5.0 GiB, installed): passed its answer check here, 45.0 tokens/s; needs 5.2 GiB, measured here\n");
  expect(out).toContain("  small (1.0 GiB, download): not measured on this machine yet\n");
  expect(out.indexOf("Recommended for this machine:")).toBeLessThan(out.indexOf("  big: 5.0 GiB, installed"));
});
it("defaults to the first ranked pick, matching models recommend", async () => {
  serve({ ...base, recommendedModel: "small", recommended: [{ id: "big", basis: "proven_fast", tokensPerSecond: 45 }],
    models: [sized("big", 5, { installed: true }), sized("small", 1)] } as never);
  const ask = vi.fn().mockResolvedValueOnce("").mockResolvedValueOnce("").mockResolvedValueOnce("n");
  await setupModel(parseArgs(["setup"]), { ask, progress: vi.fn() }, deps);
  expect(ask.mock.calls[1][0]).toBe("Model ID [big], or skip: ");
});
it("does not fall back to a failed legacy default when ranked picks are empty", async () => {
  serve({ ...base, recommended: [], models: [{ ...model, checked: "failed" }] } as never);
  await expect(setupModel(parseArgs(["setup", "--with-model"]), undefined, deps)).rejects.toThrow("No fitting model");
  expect(request).toHaveBeenCalledTimes(1);
  expect(saveCliPrefs).not.toHaveBeenCalled();
});
it("honors an explicit choice over a different recommendation", async () => {
  serve({ ...base, recommended: [{ id: "big", basis: "estimate" }], models: [sized("big", 5), model] } as never);
  expect(await setupModel(parseArgs(["setup", "--model", "small"]), undefined, deps)).toMatchObject({ model: "small", status: "ready" });
  expect(request.mock.calls.some(c => c[0].includes("/big/"))).toBe(false);
});
it("asks intended use before recommendations and uses that ranking as the model default", async () => {
  serve({ ...base, useCase: "coding", recommended: [{ id: "small", basis: "estimate" }], models: [{ ...model, useCases: ["coding"] }] } as never);
  const ask = vi.fn().mockResolvedValueOnce("1").mockResolvedValueOnce("").mockResolvedValueOnce("n");
  const progress = vi.fn();
  expect(await setupModel(parseArgs(["setup"]), { ask, progress }, deps)).toMatchObject({ status: "skipped", useCase: "coding" });
  expect(ask.mock.calls.map(c => c[0])).toEqual(["Use [4], or skip: ", "Model ID [small], or skip: ", expect.stringContaining("Download")]);
  expect(request).toHaveBeenCalledExactlyOnceWith("/models/setup-options?use=coding", { timeout: 30000 });
  expect(progress.mock.calls.flat().join("")).toContain("catalog match for coding");
  expect(saveCliPrefs).not.toHaveBeenCalled();
});
it.each(["", "skip", "4", "general"])("accepts %j as general use without extra requests", async answer => {
  serve();
  const ask = vi.fn().mockResolvedValueOnce(answer).mockResolvedValueOnce("skip");
  expect(await setupModel(parseArgs(["setup"]), { ask, progress: vi.fn() }, deps)).toMatchObject({ status: "skipped" });
  expect(request).toHaveBeenCalledExactlyOnceWith("/models/setup-options", { timeout: 30000 });
});
it("does not interpret an invalid purpose or EOF as consent to install", async () => {
  serve(); const ask = vi.fn().mockResolvedValueOnce("anything").mockResolvedValueOnce(null);
  await expect(setupModel(parseArgs(["setup"]), { ask, progress: vi.fn() }, deps)).rejects.toThrow("interrupted");
  expect(request).not.toHaveBeenCalled(); expect(saveCliPrefs).not.toHaveBeenCalled();
});
it("uses --use without asking the purpose again", async () => {
  serve({ ...base, useCase: "writing" } as never);
  const ask = vi.fn().mockResolvedValueOnce("skip");
  await setupModel(parseArgs(["setup", "--use", "writing"]), { ask, progress: vi.fn() }, deps);
  expect(ask).toHaveBeenCalledExactlyOnceWith("Model ID [small], or skip: ");
  expect(request).toHaveBeenCalledExactlyOnceWith("/models/setup-options?use=writing", { timeout: 30000 });
});
it("adds the picks to a finished setup's result and leaves the result alone for an engine without them", async () => {
  serve({ ...base, recommended: [{ id: "small", basis: "estimate", tokensPerSecond: null }] } as never);
  expect(await setupModel(parseArgs(["setup", "--model", "small", "--no-input"]), undefined, deps))
    .toMatchObject({ status: "ready", model: "small", recommended: [{ id: "small", basis: "estimate", tokensPerSecond: null }] });
  serve();
  expect(await setupModel(parseArgs(["setup", "--model", "small", "--no-input"]), undefined, deps)).not.toHaveProperty("recommended");
});
it("prints no list and no new line for an engine without picks", async () => {
  serve(); const said: string[] = [], ask = vi.fn().mockResolvedValueOnce("").mockResolvedValueOnce("small").mockResolvedValueOnce("n");
  await setupModel(parseArgs(["setup"]), { ask, progress: m => { said.push(m); } }, deps);
  expect(said.join("")).not.toContain("Recommended");
});
it("keeps valid IDs of listed models, once each, three at most, in the engine's order", () => {
  const models = ["a", "b", "c", "d"].map(id => sized(id, 1));
  const raw = [{ id: "b", basis: "estimate" }, { id: "b", basis: "estimate" }, { id: "zzz", basis: "estimate" }, { id: "../x", basis: "estimate" },
    7, null, [], { id: "a", basis: "proven_fast", tokensPerSecond: -1 }, { id: "c", basis: "estimate", tokensPerSecond: Number.NaN }, { id: "d" }];
  expect(parseRecommended(raw, models)).toEqual([{ id: "b", basis: "estimate", tokensPerSecond: null },
    { id: "a", basis: "proven_fast", tokensPerSecond: null }, { id: "c", basis: "estimate", tokensPerSecond: null }]);
  expect(parseRecommended(undefined, models)).toEqual([]);
  expect(parseRecommended({ id: "a" }, models)).toEqual([]);
});
it("words each kind of pick, and names the measurement only where the fit came from one", () => {
  const models = [sized("fast", 4, { installed: true, loaded: true, fitBasis: "measured", measured: { hostGiB: 5.24, gpuGiB: null, contextTokens: null, tokensPerSecond: null, checkedAt: null } }),
    sized("slow", 2, { installed: true, fitBasis: "estimate", measured: null }), sized("nospeed", 1, { fitBasis: "measured", measured: { hostGiB: 1.04, gpuGiB: null, contextTokens: null, tokensPerSecond: null, checkedAt: null } }),
    sized("new", 3)];
  const lines = recommendedLines([{ id: "fast", basis: "proven_fast", tokensPerSecond: 130.4 }, { id: "slow", basis: "proven_slow", tokensPerSecond: 9 },
    { id: "nospeed", basis: "proven_speed_unknown", tokensPerSecond: null }, { id: "new", basis: "estimate", tokensPerSecond: null }], models);
  expect(lines).toEqual(["Recommended for this machine:",
    "  fast (4.0 GiB, loaded): passed its answer check here, 130 tokens/s; needs 5.2 GiB, measured here",
    "  slow (2.0 GiB, installed): passed its answer check here, 9.0 tokens/s",
    "  nospeed (1.0 GiB, download): passed its answer check here, speed not measured yet; needs 1.0 GiB, measured here",
    "  new (3.0 GiB, download): not measured on this machine yet"]);
  expect(recommendedLines([], models)).toEqual([]);
});

it("requires explicit fit rather than truthy unknown hardware", () => {
  const models = [sized("unknown", 1, { fits: "unknown" as unknown as boolean }), sized("failed", 1, { checked: "failed" })];
  expect(parseRecommended(models.map(m => ({ id: m.id, basis: "estimate" })), models)).toEqual([]);
});

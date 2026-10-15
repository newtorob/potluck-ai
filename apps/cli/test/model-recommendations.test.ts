import { afterEach, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { runOperation } from "../src/operations.js";
import { runtimeRequest } from "../src/runtime.js";

vi.mock("../src/runtime.js", async importOriginal => ({ ...await importOriginal<typeof import("../src/runtime.js")>(), runtimeRequest: vi.fn() }));
afterEach(() => vi.resetAllMocks());
const model = (id: string) => ({ id, name: id, sizeBytes: 5 * 1024 ** 3, installed: false, loaded: false,
  fits: true, reasons: [], estimatedWorkingGiB: 7.5, fitBasis: "estimate" });
const options = () => ({ schemaVersion: 1,
  hardware: { os: "Darwin", arch: "arm64", cpu_name: "Apple M2 Pro", gpu_name: "Apple M2 Pro", ram_available_gb: 10, ram_total_gb: 16, gpu_vram_gb: 16 },
  diskFreeBytes: 100 * 1024 ** 3, currentModel: "small", recommendedModel: "small", models: [model("medium"), model("small")],
  recommended: [{ id: "medium", basis: "estimate", tokensPerSecond: null }] });
async function run(...args: string[]) {
  let stdout = "";
  const code = await runOperation(parseArgs(["models", "recommend", ...args]), text => { stdout += text; });
  return { code, stdout };
}

it("shows this machine, shared memory and the first pick with an explicit setup command", async () => {
  vi.mocked(runtimeRequest).mockResolvedValue(options());
  const { stdout, code } = await run();
  expect(code).toBe(0);
  expect(stdout).toContain("GPU: Apple M2 Pro (shares system memory)");
  expect(stdout).toContain("10.0 GiB available / 16.0 GiB total, unified");
  expect(stdout).toContain("not measured on this machine yet");
  expect(stdout).toContain("First choice: medium");
  expect(stdout).toContain("potluck setup --model medium");
  expect(runtimeRequest).toHaveBeenCalledExactlyOnceWith("/models/setup-options", { timeout: 30000 });
});

it("returns structured picks without private engine fields or download sources", async () => {
  const source = options();
  vi.mocked(runtimeRequest).mockResolvedValue({ ...source, api_key: "private-token", models: source.models.map(m => ({ ...m, download_url: "private-url", path: "/private/model" })) });
  const { stdout } = await run("--json");
  expect(JSON.parse(stdout)).toMatchObject({ action: "recommend", status: "ready", currentModel: "small", recommendedModel: "medium" });
  expect(stdout).not.toContain("private");
  expect(runtimeRequest).toHaveBeenCalledTimes(1);
});

it("excludes failed or nonfitting picks and explains an empty result without suggesting a download", async () => {
  const source = options();
  vi.mocked(runtimeRequest).mockResolvedValue({ ...source, recommended: [...source.recommended, { id: "small", basis: "proven_fast" }],
    models: [{ ...model("medium"), fits: false, reasons: ["Not enough memory."] }, { ...model("small"), checked: "failed" }] });
  const { stdout } = await run();
  expect(stdout).toContain("No model can be recommended");
  expect(stdout).toContain("Not enough memory.");
  expect(stdout).toContain("failed its local answer check");
  expect(stdout).not.toContain("potluck setup --model");
  expect(JSON.parse((await run("--json")).stdout).recommendedModel).toBeNull();
});

it("labels CPU fallback despite the physical GPU being present", async () => {
  const source = options();
  vi.mocked(runtimeRequest).mockResolvedValue({ ...source, hardware: { ...source.hardware, os: "Linux", arch: "x86_64", gpu_name: "RTX 2080 Ti", gpu_vram_gb: 11, inference_gpu_available: false } });
  expect((await run()).stdout).toContain("The runtime is using CPU execution.");
});

it("requests a runtime upgrade when ranking is unavailable", async () => {
  vi.mocked(runtimeRequest).mockResolvedValue({ ...options(), recommended: undefined });
  await expect(run()).rejects.toThrow("Upgrade the Potluck runtime");
});
it.each(["coding", "writing", "reasoning"])("requests and explains recommendations for %s", async useCase => {
  vi.mocked(runtimeRequest).mockResolvedValue({ ...options(), useCase, models: [{ ...model("medium"), useCases: [useCase] }] });
  const { stdout } = await run("--use", useCase);
  expect(runtimeRequest).toHaveBeenCalledExactlyOnceWith(`/models/setup-options?use=${useCase}`, { timeout: 30000 });
  expect(stdout).toContain(`catalog match for ${useCase}`);
  expect(stdout).toContain("Task quality and tool reliability have not been benchmarked here.");
  expect(JSON.parse((await run("--use", useCase, "--json")).stdout).useCase).toBe(useCase);
});
it("does not silently ignore the purpose on an older runtime", async () => {
  vi.mocked(runtimeRequest).mockResolvedValue(options());
  await expect(run("--use", "coding")).rejects.toThrow("Upgrade the Potluck runtime for recommendations by intended use");
});
it("rejects invalid purposes and unrelated commands before requests", () => {
  for (const argv of [["models", "recommend", "--use", "gaming"], ["models", "list", "--use", "coding"],
    ["--use", "writing"], ["setup", "--preferences", "--use", "coding"], ["setup", "--no-input", "--use", "coding"]]) {
    expect(() => parseArgs(argv)).toThrow();
  }
  expect(parseArgs(["setup", "--with-model", "--no-input", "--use", "coding"]).use).toBe("coding");
  expect(runtimeRequest).not.toHaveBeenCalled();
});

it("rejects targets and inference-only flags before contacting the engine", () => {
  for (const extra of [["tiny"], ["--model", "tiny"], ["--full"], ["--force"]]) {
    expect(() => parseArgs(["models", "recommend", ...extra])).toThrow();
  }
  expect(runtimeRequest).not.toHaveBeenCalled();
});

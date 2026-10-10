import { describe, expect, it } from "vitest";
import { backendName, describeLoaded, describeMachine, formatSummary, generationRate, gpuNote, parseInference, parseRun, round1, roundRun, spread, summarizeRuns } from "../src/benchmark-stats.js";

const result = (over: Record<string, unknown> = {}) => ({ model_id: "m", output_tokens: 64, ttft_ms: 300, duration_ms: 3300, tokens_per_sec: 19.4, prompt_tokens_per_sec: 240, ...over });

describe("generation rate", () => {
  it("counts tokens after the first over the time after the first", () => {
    expect(generationRate(64, 300, 3300)).toBe(21);
    expect(generationRate(101, 0, 10000)).toBe(10);
  });
  it.each([[64, 3300, 3300], [64, 3400, 3300], [1, 300, 3300], [0, 0, 0], [0, 300, 3300], [64, Number.NaN, 3300], [Number.POSITIVE_INFINITY, 300, 3300]])(
    "gives no rate instead of NaN or Infinity for tokens=%s ttft=%s duration=%s", (tokens, ttft, duration) => {
      expect(generationRate(tokens, ttft, duration)).toBeNull();
    });
});

describe("medians", () => {
  it("takes the middle run, or the mean of the middle two, and keeps min and max", () => {
    expect(spread([21, 19.5, 22.5])).toEqual({ median: 21, min: 19.5, max: 22.5 });
    expect(spread([10, 20, 40, 30])).toEqual({ median: 25, min: 10, max: 40 });
    expect(spread([7])).toEqual({ median: 7, min: 7, max: 7 });
  });
  it("ignores missing values and reports nothing when none remain", () => {
    expect(spread([null, 4, null])).toEqual({ median: 4, min: 4, max: 4 });
    expect(spread([null, Number.NaN])).toBeNull();
    expect(spread([])).toBeNull();
  });
  it("summarizes runs on known numbers, leaving out runs that have no first token", () => {
    const runs = [result({ duration_ms: 3300 }), result({ duration_ms: 3100, tokens_per_sec: 20.6 }), result({ duration_ms: 3500, tokens_per_sec: 18.3, prompt_tokens_per_sec: null }),
      result({ output_tokens: 0, ttft_ms: 0, duration_ms: 50, tokens_per_sec: 0 })].map(r => parseRun(r, "m"));
    const s = summarizeRuns(runs);
    // Rates and times are rounded to one decimal after the median is taken from the unrounded runs.
    expect(s.generationTokensPerSecond).toEqual({ median: 21, min: 19.7, max: 22.5 });
    expect(s.firstTokenMs).toEqual({ median: 300, min: 300, max: 300 });
    expect(s.promptTokensPerSecond).toEqual({ median: 240, min: 240, max: 240 });
    expect(s.engineTokensPerSecond!.median).toBe(19.4);
  });
});

describe("rounding", () => {
  it("keeps one decimal in the run and the summary, and takes medians from the unrounded runs", () => {
    const run = parseRun(result({ output_tokens: 64, ttft_ms: 301.26, duration_ms: 3333.37, tokens_per_sec: 19.43, prompt_tokens_per_sec: 241.77 }), "m");
    expect(run.generationTokensPerSecond).toBeCloseTo(63 / 3.03211, 9);
    expect(roundRun(run)).toEqual({ outputTokens: 64, ttftMs: 301.3, durationMs: 3333.4, engineTokensPerSecond: 19.4, generationTokensPerSecond: 20.8, promptTokensPerSecond: 241.8 });
    expect(roundRun(parseRun(result({ output_tokens: 1, prompt_tokens_per_sec: null }), "m"))).toMatchObject({ generationTokensPerSecond: null, promptTokensPerSecond: null });
    const summary = summarizeRuns([run, parseRun(result({ duration_ms: 1900 }), "m")]);
    expect(JSON.stringify(summary)).not.toMatch(/\d\.\d\d/);
    expect(summary.generationTokensPerSecond).toEqual({ median: 30.1, min: 20.8, max: 39.4 }); // (20.78 + 39.375) / 2 = 30.08
  });
  it("rounds half up to one decimal", () => {
    expect(round1(157.1072319201995)).toBe(157.1); expect(round1(0.05)).toBe(0.1); expect(round1(21)).toBe(21);
  });
});

describe("engine results", () => {
  it("rejects a result for another model, or with numbers it cannot use", () => {
    expect(() => parseRun(result({ model_id: "other" }), "m")).toThrow("changed to another model");
    for (const bad of [{ ttft_ms: "300" }, { duration_ms: -1 }, { output_tokens: null }, { tokens_per_sec: Number.NaN }]) {
      expect(() => parseRun(result(bad), "m")).toThrow("invalid benchmark result");
    }
  });
  it("treats a missing or zero prompt rate as absent", () => {
    expect(parseRun(result({ prompt_tokens_per_sec: null }), "m").promptTokensPerSecond).toBeNull();
    expect(parseRun(result({ prompt_tokens_per_sec: 0 }), "m").promptTokensPerSecond).toBeNull();
  });
  it("prints a response too short to rate without NaN or Infinity", () => {
    const text = formatSummary("m", 2, summarizeRuns([parseRun(result({ output_tokens: 1, duration_ms: 300 }), "m"), parseRun(result({ duration_ms: 300 }), "m")])).join("\n");
    expect(text).toContain("speed after the first token not measured");
    expect(text).not.toMatch(/NaN|Infinity|null|undefined/);
  });
  it("shows a range only when runs differ at the printed precision", () => {
    const one = formatSummary("m", 1, summarizeRuns([parseRun(result(), "m")])).join("\n");
    expect(one).toContain("m, 1 run: 21.0 tokens/s, first token 300 ms, prompt processing 240 tokens/s");
    const three = formatSummary("m", 3, summarizeRuns([3300, 3100, 3500].map(d => parseRun(result({ duration_ms: d }), "m")))).join("\n");
    expect(three).toContain("m, 3 runs: 21.0 tokens/s (19.7 to 22.5), first token 300 ms, prompt processing 240 tokens/s");
    expect(three).toContain("Engine's own figure: 19.4 tokens/s, counted over the whole response including the wait for the first token.");
  });
});

describe("backend names", () => {
  it.each([["mtl0_mapped", "Metal"], ["MTL0_Mapped", "Metal"], ["MTL0", "Metal"], ["metal", "Metal"], ["Metal_Mapped", "Metal"], ["vulkan0", "Vulkan"], ["Vulkan0_Host", "Vulkan"],
    ["cuda0", "CUDA"], ["CUDA_Host", "CUDA"], ["rocm0", "ROCm"], ["hip0", "HIP"], ["sycl0", "SYCL"], ["opencl0", "OpenCL"], ["cpu", "CPU"], ["CPU_Mapped", "CPU"],
    ["blas", "blas"], ["Kompute0", "Kompute0"]])("shows %s as %s", (raw, shown) => {
    expect(backendName(raw)).toBe(shown);
  });
  it("leaves a missing name missing", () => { expect(backendName(null)).toBeNull(); });
  it("reads the names a real Mac engine reports", () => {
    const real = parseInference({ devices: [{ type: "gpu", description: "Apple M2 Pro" }], loaded: { used_n_ctx: 8192, offloaded_layers: 37, total_layers: 37,
      summary: { device_kind: "gpu", backend: "mtl0_mapped", device_name: "MTL0_Mapped", execution_verified: true } } });
    expect(describeLoaded("m", real.placement)).toBe("Loaded: m on the GPU (Metal), all 37 layers, 8192-token context");
    expect(real.placement!.backend).toBe("Metal");
  });
  it("shows Vulkan and CUDA engines the same way", () => {
    const on = (backend: string, kind = "gpu") => describeLoaded("m", parseInference({ loaded: { used_n_ctx: 4096, offloaded_layers: 33, total_layers: 33, summary: { device_kind: kind, backend, execution_verified: true } } }).placement);
    expect(on("vulkan0")).toBe("Loaded: m on the GPU (Vulkan), all 33 layers, 4096-token context");
    expect(on("cuda0")).toBe("Loaded: m on the GPU (CUDA), all 33 layers, 4096-token context");
    expect(on("somethingnew0")).toBe("Loaded: m on the GPU (somethingnew0), all 33 layers, 4096-token context");
  });
});

describe("where a model runs", () => {
  const info = (loaded: unknown, devices: unknown[] = [{ type: "gpu", description: "Apple M2 Pro" }]) => parseInference({ loaded, devices, benchmark: null });
  const gpu = { used_n_ctx: 8192, offloaded_layers: 37, total_layers: 37, summary: { device_kind: "gpu", backend: "metal", device_name: "Apple M2 Pro", execution_verified: true } };
  it("describes a model fully on the GPU", () => {
    expect(describeLoaded("qwen3-8b-q4", info(gpu).placement)).toBe("Loaded: qwen3-8b-q4 on the GPU (Metal), all 37 layers, 8192-token context");
  });
  it("says so in one line when the model is on the CPU although a GPU is listed", () => {
    const cpu = info({ ...gpu, offloaded_layers: 0, summary: { device_kind: "cpu", backend: "cpu", execution_verified: true } });
    expect(describeLoaded("m", cpu.placement)).toBe("Loaded: m on the CPU, 8192-token context");
    expect(gpuNote(cpu.placement, cpu.gpuDevices)).toBe("Note: this model runs on the CPU although the engine lists a GPU (Apple M2 Pro). The speeds below are CPU speeds.");
    expect(gpuNote(cpu.placement, [])).toBeNull();
    expect(gpuNote(info(gpu).placement, ["Apple M2 Pro"])).toBeNull();
  });
  it("names a partial offload", () => {
    const mixed = info({ ...gpu, offloaded_layers: 20, total_layers: 41, summary: { device_kind: "mixed", backend: "cuda", device_name: "RTX 2080 Ti", execution_verified: true } }, [{ type: "gpu", description: "RTX 2080 Ti" }]);
    expect(describeLoaded("m", mixed.placement)).toBe("Loaded: m, 20 of 41 layers on the GPU (CUDA), the rest on the CPU, 8192-token context");
    expect(gpuNote(mixed.placement, mixed.gpuDevices)).toContain("only 20 of 41 layers are on the GPU (RTX 2080 Ti)");
  });
  it("does not claim a place when the engine could not verify one, or reports none", () => {
    expect(describeLoaded("m", info({ ...gpu, summary: { device_kind: "unknown", execution_verified: false } }).placement)).toContain("could not be confirmed");
    expect(describeLoaded("m", parseInference(null).placement)).toContain("not reported by this engine");
    expect(describeLoaded(null, null)).toBe("Loaded: no model");
  });
  it("keeps only allowlisted engine fields and strips control sequences", () => {
    const parsed = parseInference({ build: { lib_dir: "/private/secret" }, loaded: { ...gpu, backend_selection: { lib_dir: "/private/secret" }, summary: { ...gpu.summary, device_name: "\x1b[31mGPU\x1b[0m" } },
      devices: [], benchmark: { model_id: "m", tokens_per_sec: 12, ttft_ms: 40, error: "secret" } });
    expect(JSON.stringify(parsed)).not.toMatch(/secret|\\u001b/);
    expect(parsed.placement!.device).toBe("GPU");
    expect(parsed.lastBenchmark).toEqual({ model: "m", tokensPerSecond: 12, firstTokenMs: 40, promptTokensPerSecond: null });
  });
});

describe("machine description", () => {
  it("uses the GPU name when the processor name is only an architecture", () => {
    expect(describeMachine({ cpu_name: "arm", gpu_name: "Apple M2 Pro", ram_total_gb: 16, ram_available_gb: 8, os: "Darwin", arch: "arm64" }).description).toBe("Apple M2 Pro, 16 GiB memory, 8.0 GiB available now");
  });
  it("lists both when they are different", () => {
    expect(describeMachine({ cpu_name: "13th Gen Intel(R) Core(TM) i7-13700K", gpu_name: "NVIDIA GeForce RTX 2080 Ti", ram_total_gb: 31.3, ram_available_gb: 20 }).description)
      .toBe("13th Gen Intel(R) Core(TM) i7-13700K, NVIDIA GeForce RTX 2080 Ti, 31 GiB memory, 20.0 GiB available now");
  });
  it("shows how much memory is free, in the GiB that `potluck setup` prints", () => {
    const mac = describeMachine({ cpu_name: "arm", gpu_name: "Apple M2 Pro", ram_total_gb: 16, ram_available_gb: 2.8 });
    expect(mac.description).toBe("Apple M2 Pro, 16 GiB memory, 2.8 GiB available now");
    expect(mac).toMatchObject({ ramTotalGB: 16, ramAvailableGB: 2.8 });
    expect(describeMachine({ cpu_name: "arm", gpu_name: "Apple M2 Pro", ram_total_gb: 16 } as never).description).toBe("Apple M2 Pro, 16 GiB memory");
  });
  it("falls back to the operating system when nothing is named", () => {
    expect(describeMachine({ cpu_name: "x86_64", ram_total_gb: 8, ram_available_gb: 4, os: "Linux", arch: "x86_64" }).description).toBe("Linux x86_64, 8 GiB memory, 4.0 GiB available now");
    expect(describeMachine({} as never).description).toBe("Unknown machine");
  });
});

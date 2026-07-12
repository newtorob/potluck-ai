import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  assertGatewayReady,
  cliPrefsExist,
  GatewayDisabledError,
  loadCliPrefs,
  loadConfig,
  loadSharedPrefs,
  mergeSharedSafeFields,
  modelCapabilityWarning,
  normalizeScope,
  resolveModel,
  resolveScope,
  resolveVerify,
  saveCliPrefs,
  saveSharedSafeFields,
} from "../src/config.js";

let home = "";
const savedEnv = { ...process.env };

beforeEach(async () => {
  home = await mkdtemp(join(tmpdir(), "potluck-cfg-"));
  process.env.HOME = home;
  delete process.env.POTLUCK_GATEWAY_KEY;
  delete process.env.POTLUCK_GATEWAY_URL;
});

afterEach(async () => {
  // Restore env vars in place. Reassigning process.env wholesale detaches it
  // from the live proxy, after which HOME mutations no longer reach os.homedir().
  process.env.HOME = savedEnv.HOME;
  delete process.env.POTLUCK_GATEWAY_KEY;
  delete process.env.POTLUCK_GATEWAY_URL;
  await rm(home, { recursive: true, force: true });
});

async function writeConfig(gateway: unknown): Promise<void> {
  const dir = join(home, ".potluck");
  await mkdir(dir, { recursive: true });
  await writeFile(join(dir, "config.json"), JSON.stringify({ gateway }), "utf8");
}

describe("loadConfig", () => {
  it("reads enabled + api_key from ~/.potluck/config.json", async () => {
    await writeConfig({ enabled: true, api_key: "secret-key" });
    const cfg = await loadConfig();
    expect(cfg.enabled).toBe(true);
    expect(cfg.apiKey).toBe("secret-key");
    expect(cfg.baseURL).toBe("http://127.0.0.1:8321/v1");
  });

  it("treats a disabled gateway as not ready", async () => {
    await writeConfig({ enabled: false, api_key: "" });
    const cfg = await loadConfig();
    expect(cfg.enabled).toBe(false);
  });

  it("falls back to defaults when no config file exists", async () => {
    const cfg = await loadConfig();
    expect(cfg.enabled).toBe(false);
    expect(cfg.apiKey).toBe("");
    expect(cfg.baseURL).toBe("http://127.0.0.1:8321/v1");
  });

  it("lets env vars override config (key + url, and forces enabled)", async () => {
    await writeConfig({ enabled: false, api_key: "from-config" });
    process.env.POTLUCK_GATEWAY_KEY = "from-env";
    process.env.POTLUCK_GATEWAY_URL = "http://127.0.0.1:9999/v1/";
    const cfg = await loadConfig();
    expect(cfg.apiKey).toBe("from-env");
    expect(cfg.baseURL).toBe("http://127.0.0.1:9999/v1"); // trailing slash trimmed
    expect(cfg.enabled).toBe(true);
  });
});

describe("assertGatewayReady", () => {
  it("throws an actionable message when disabled or keyless", () => {
    expect(() =>
      assertGatewayReady({ baseURL: "x", apiKey: "", enabled: false }),
    ).toThrow(GatewayDisabledError);

    try {
      assertGatewayReady({ baseURL: "x", apiKey: "", enabled: true });
      throw new Error("should have thrown");
    } catch (err) {
      expect(err).toBeInstanceOf(GatewayDisabledError);
      expect((err as Error).message).toContain("Settings -> Connect tools");
    }
  });

  it("passes when enabled and keyed", () => {
    expect(() =>
      assertGatewayReady({ baseURL: "x", apiKey: "k", enabled: true }),
    ).not.toThrow();
  });
});

describe("resolveModel", () => {
  it("returns the preferred model without touching the network", async () => {
    const cfg = { baseURL: "http://invalid.invalid", apiKey: "k", enabled: true };
    await expect(resolveModel(cfg, "llama-3.1-8b")).resolves.toBe("llama-3.1-8b");
  });
});

async function writeCliPrefs(prefs: unknown): Promise<void> {
  const dir = join(home, ".potluck");
  await mkdir(dir, { recursive: true });
  await writeFile(join(dir, "cli.json"), JSON.stringify(prefs), "utf8");
}

describe("config layering (config.json < cli.json < env)", () => {
  it("folds cli.json gatewayUrl into the base URL", async () => {
    await writeConfig({ enabled: true, api_key: "k" });
    await writeCliPrefs({ gatewayUrl: "http://127.0.0.1:7777/v1" });
    const cfg = await loadConfig();
    expect(cfg.baseURL).toBe("http://127.0.0.1:7777/v1");
  });

  it("lets POTLUCK_GATEWAY_URL win over cli.json gatewayUrl", async () => {
    await writeCliPrefs({ gatewayUrl: "http://127.0.0.1:7777/v1" });
    process.env.POTLUCK_GATEWAY_URL = "http://127.0.0.1:9999/v1";
    const cfg = await loadConfig();
    expect(cfg.baseURL).toBe("http://127.0.0.1:9999/v1");
  });
});

describe("loadCliPrefs", () => {
  it("reads model, scope, gatewayUrl and ignores an invalid scope", async () => {
    await writeCliPrefs({ model: "m1", scope: "bogus", gatewayUrl: "u" });
    const prefs = await loadCliPrefs();
    expect(prefs.model).toBe("m1");
    expect(prefs.gatewayUrl).toBe("u");
    expect(prefs.scope).toBeUndefined(); // bad scope dropped
  });

  it("round-trips through saveCliPrefs", async () => {
    await saveCliPrefs({ model: "m2", scope: "local", gatewayUrl: "g" });
    expect(await cliPrefsExist()).toBe(true);
    const prefs = await loadCliPrefs();
    expect(prefs).toEqual({ model: "m2", scope: "local", gatewayUrl: "g" });
  });

  it("returns empty + cliPrefsExist false when missing", async () => {
    expect(await cliPrefsExist()).toBe(false);
    expect(await loadCliPrefs()).toEqual({});
  });
});

describe("loadSharedPrefs", () => {
  it("reads active_model_id and network.coordinator_url", async () => {
    const dir = join(home, ".potluck");
    await mkdir(dir, { recursive: true });
    await writeFile(
      join(dir, "config.json"),
      JSON.stringify({
        gateway: { enabled: true, api_key: "k" },
        active_model_id: "llama",
        network: { coordinator_url: "http://10.0.0.1:8400" },
      }),
      "utf8",
    );
    const shared = await loadSharedPrefs();
    expect(shared.activeModelId).toBe("llama");
    expect(shared.coordinatorUrl).toBe("http://10.0.0.1:8400");
  });
});

describe("mergeSharedSafeFields (wizard write-back safety)", () => {
  it("sets only active_model_id + network.coordinator_url, preserving the rest", () => {
    const raw = {
      gateway: { enabled: true, api_key: "SECRET", pool_routing: false },
      network: { coordinator_url: "old", max_gpu_percent: 80 },
      active_model_id: "old-model",
      runtime: { temperature: 0.7 },
    };
    const next = mergeSharedSafeFields(raw, {
      model: "new-model",
      coordinatorUrl: "http://new:8400",
    });
    expect(next.active_model_id).toBe("new-model");
    expect(next.network).toEqual({
      coordinator_url: "http://new:8400",
      max_gpu_percent: 80,
    });
    // gateway is passed through untouched — never the key or the enabled flag.
    expect(next.gateway).toEqual({ enabled: true, api_key: "SECRET", pool_routing: false });
    expect(next.runtime).toEqual({ temperature: 0.7 });
  });

  it("does not invent a gateway block or change keys", () => {
    const next = mergeSharedSafeFields({}, { model: "m" });
    expect(next.active_model_id).toBe("m");
    expect("gateway" in next).toBe(false);
  });

  it("saveSharedSafeFields never touches gateway api_key/enabled on disk", async () => {
    await writeConfig({ enabled: true, api_key: "DO-NOT-TOUCH" });
    await saveSharedSafeFields({ model: "model-x", coordinatorUrl: "http://c:8400" });
    const raw = JSON.parse(
      await readFile(join(home, ".potluck", "config.json"), "utf8"),
    ) as Record<string, unknown>;
    expect(raw.active_model_id).toBe("model-x");
    expect((raw.network as Record<string, unknown>).coordinator_url).toBe("http://c:8400");
    expect(raw.gateway).toEqual({ enabled: true, api_key: "DO-NOT-TOUCH" });
  });
});

describe("scope resolution", () => {
  it("defaults to mine, honors cli.json, and lets a flag win", () => {
    expect(resolveScope(undefined, undefined).scope).toBe("mine");
    expect(resolveScope(undefined, "local").scope).toBe("local");
    expect(resolveScope("local", "mine").scope).toBe("local");
  });

  it("treats circle/cooperative as single-turn only and falls back to mine", () => {
    const circle = normalizeScope("circle", "local");
    expect(circle.scope).toBe("mine");
    expect(circle.notice).toMatch(/single-turn only/);

    const coop = resolveScope("cooperative", "local");
    expect(coop.scope).toBe("mine");
    expect(coop.notice).toMatch(/single-turn only/);
  });

  it("falls back with a notice for an unknown scope", () => {
    const r = normalizeScope("weird", "local");
    expect(r.scope).toBe("local");
    expect(r.notice).toMatch(/unknown scope/);
  });
});

describe("resolveVerify", () => {
  let dir = "";

  beforeEach(async () => {
    dir = await mkdtemp(join(tmpdir(), "potluck-verify-"));
  });

  afterEach(async () => {
    await rm(dir, { recursive: true, force: true });
  });

  async function writePkg(scripts: Record<string, string>): Promise<void> {
    await writeFile(join(dir, "package.json"), JSON.stringify({ scripts }), "utf8");
  }

  it("honors precedence: --verify flag > env > cli.json", async () => {
    const r = await resolveVerify({
      cwd: dir,
      cliVerify: "from-cli",
      envVerify: "from-env",
      flag: "from-flag",
    });
    expect(r).toEqual({ command: "from-flag", autoDetected: false });
  });

  it("lets env win over cli.json when no flag", async () => {
    const r = await resolveVerify({ cwd: dir, cliVerify: "from-cli", envVerify: "from-env" });
    expect(r).toEqual({ command: "from-env", autoDetected: false });
  });

  it("uses cli.json when it is the only explicit layer", async () => {
    const r = await resolveVerify({ cwd: dir, cliVerify: "tsc --noEmit" });
    expect(r).toEqual({ command: "tsc --noEmit", autoDetected: false });
  });

  it("auto-detects pnpm build from package.json + pnpm-lock.yaml", async () => {
    await writePkg({ build: "tsc" });
    await writeFile(join(dir, "pnpm-lock.yaml"), "lockfileVersion: 9\n", "utf8");
    const r = await resolveVerify({ cwd: dir });
    expect(r).toEqual({ command: "pnpm build", autoDetected: true });
  });

  it("auto-detects npm run build when there is no pnpm-lock", async () => {
    await writePkg({ build: "tsc" });
    const r = await resolveVerify({ cwd: dir });
    expect(r).toEqual({ command: "npm run build", autoDetected: true });
  });

  it("falls back to tsc --noEmit when tsconfig exists but no build script", async () => {
    await writePkg({ test: "vitest" }); // package.json without a build script
    await writeFile(join(dir, "tsconfig.json"), "{}", "utf8");
    const r = await resolveVerify({ cwd: dir });
    expect(r).toEqual({ command: "npx tsc --noEmit", autoDetected: true });
  });

  it("detects nothing when there is no package.json or tsconfig", async () => {
    const r = await resolveVerify({ cwd: dir });
    expect(r).toEqual({ command: null, autoDetected: false });
  });

  it("treats cli.json verify \"\" / \"off\" as disabled (no auto-detect)", async () => {
    await writePkg({ build: "tsc" }); // would otherwise auto-detect
    expect(await resolveVerify({ cwd: dir, cliVerify: "off" })).toEqual({
      command: null,
      autoDetected: false,
    });
    expect(await resolveVerify({ cwd: dir, cliVerify: "" })).toEqual({
      command: null,
      autoDetected: false,
    });
  });

  it("--no-verify disables even when a flag/auto-detect would apply", async () => {
    await writePkg({ build: "tsc" });
    const r = await resolveVerify({ cwd: dir, flag: "pnpm build", noVerify: true });
    expect(r).toEqual({ command: null, autoDetected: false });
  });
});

describe("modelCapabilityWarning", () => {
  it("clears a capable 14B+ code model", () => {
    expect(modelCapabilityWarning("qwen2.5-coder:14b")).toBeNull();
    expect(modelCapabilityWarning("qwen2.5-coder:32b-instruct-q4_K_M")).toBeNull();
    expect(modelCapabilityWarning("deepseek-coder-v2:16b")).toBeNull();
  });

  it("flags a ~7B coder as marginal for multi-step work", () => {
    expect(modelCapabilityWarning("qwen2.5-coder:7b")).toMatch(/14b|multi-step/i);
    expect(modelCapabilityWarning("qwen2.5-coder:8b")).toMatch(/14b|multi-step/i);
  });

  it("warns on a small model and steers to 14B", () => {
    expect(modelCapabilityWarning("qwen2.5-coder:3b")).toMatch(/small|14b/i);
    expect(modelCapabilityWarning("llama3.2:1b")).toMatch(/small|14b/i);
  });

  it("warns on a non-code-tuned model", () => {
    expect(modelCapabilityWarning("llama-3.1-8b-instruct")).toMatch(/code-tuned|code model/i);
  });
});

describe("resolveVerify auto-detect (expanded ecosystems)", () => {
  it("detects Cargo / Go / Python compile checks", async () => {
    await writeFile(join(home, "Cargo.toml"), "[package]", "utf8");
    let r = await resolveVerify({ cwd: home });
    expect(r.command).toBe("cargo build");
    expect(r.autoDetected).toBe(true);
    await rm(join(home, "Cargo.toml"));

    await writeFile(join(home, "go.mod"), "module x", "utf8");
    r = await resolveVerify({ cwd: home });
    expect(r.command).toBe("go build ./...");
    await rm(join(home, "go.mod"));

    await writeFile(join(home, "pyproject.toml"), "[project]", "utf8");
    r = await resolveVerify({ cwd: home });
    expect(r.command).toMatch(/compileall/);
  });

  it("--no-verify and explicit off still disable auto-detect", async () => {
    await writeFile(join(home, "Cargo.toml"), "[package]", "utf8");
    expect((await resolveVerify({ cwd: home, noVerify: true })).command).toBeNull();
    expect((await resolveVerify({ cwd: home, flag: "off" })).command).toBeNull();
  });
});

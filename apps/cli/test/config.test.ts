import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  assertGatewayReady,
  GatewayDisabledError,
  loadConfig,
  resolveModel,
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
  process.env = { ...savedEnv };
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
  it("returns the --model flag without touching the network", async () => {
    const cfg = { baseURL: "http://invalid.invalid", apiKey: "k", enabled: true };
    await expect(resolveModel(cfg, "llama-3.1-8b")).resolves.toBe("llama-3.1-8b");
  });
});

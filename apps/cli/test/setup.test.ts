import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { loadCliPrefs } from "../src/config.js";
import { runSetup, type SetupIO } from "../src/setup.js";

let home = "";
const savedEnv = { ...process.env };

beforeEach(async () => {
  home = await mkdtemp(join(tmpdir(), "potluck-setup-"));
  process.env.HOME = home;
  delete process.env.POTLUCK_GATEWAY_KEY;
  delete process.env.POTLUCK_GATEWAY_URL;
});

afterEach(async () => {
  // Restore env vars in place (see config.test.ts): reassigning process.env
  // wholesale detaches it from the proxy os.homedir() reads.
  process.env.HOME = savedEnv.HOME;
  delete process.env.POTLUCK_GATEWAY_KEY;
  delete process.env.POTLUCK_GATEWAY_URL;
  await rm(home, { recursive: true, force: true });
});

async function writeAppConfig(config: unknown): Promise<void> {
  const dir = join(home, ".potluck");
  await mkdir(dir, { recursive: true });
  await writeFile(join(dir, "config.json"), JSON.stringify(config), "utf8");
}

async function readAppConfig(): Promise<Record<string, unknown>> {
  return JSON.parse(
    await readFile(join(home, ".potluck", "config.json"), "utf8"),
  ) as Record<string, unknown>;
}

/** Feed scripted answers in order; null once exhausted (simulates EOF). */
function scriptedIO(answers: (string | null)[]): SetupIO {
  let i = 0;
  return {
    ask: async () => (i < answers.length ? answers[i++] : null),
    out: () => {},
  };
}

describe("runSetup", () => {
  it("writes cli.json and, on opt-in, persists ONLY safe shared fields (gateway untouched)", async () => {
    // Gateway disabled so the wizard never hits the network for model listing.
    await writeAppConfig({
      gateway: { enabled: false, api_key: "" },
      active_model_id: "app-model",
      network: { coordinator_url: "http://coord:8400", max_gpu_percent: 80 },
    });

    // import? y -> model? (blank=default) -> scope? local -> url? (blank) -> share? y
    await runSetup(scriptedIO(["y", "", "local", "", "y"]));

    const prefs = await loadCliPrefs();
    expect(prefs.model).toBe("app-model");
    expect(prefs.scope).toBe("local");
    expect(prefs.gatewayUrl).toBeUndefined();

    const raw = await readAppConfig();
    expect(raw.active_model_id).toBe("app-model");
    expect((raw.network as Record<string, unknown>).coordinator_url).toBe("http://coord:8400");
    expect((raw.network as Record<string, unknown>).max_gpu_percent).toBe(80);
    // The app owns the key + the enabled flag — never written by the CLI.
    expect(raw.gateway).toEqual({ enabled: false, api_key: "" });
  });

  it("does not modify config.json when the share step is declined", async () => {
    // Gateway disabled keeps the wizard offline; the CLI must still preserve the
    // stored block verbatim (it never rewrites gateway.* — key or enabled).
    await writeAppConfig({
      gateway: { enabled: false, api_key: "KEEP-KEY" },
      active_model_id: "orig-model",
    });

    // import? n -> model? typed-model -> scope? (blank=default) -> url? (blank) -> share? n
    await runSetup(scriptedIO(["n", "typed-model", "", "", "n"]));

    const prefs = await loadCliPrefs();
    expect(prefs.model).toBe("typed-model");
    expect(prefs.scope).toBe("mine"); // default

    const raw = await readAppConfig();
    expect(raw.active_model_id).toBe("orig-model"); // untouched
    expect(raw.gateway).toEqual({ enabled: false, api_key: "KEEP-KEY" });
  });

  it("still writes cli.json with defaults when prompts hit EOF", async () => {
    await writeAppConfig({ gateway: { enabled: false, api_key: "" } });
    await runSetup(scriptedIO([])); // every ask returns null
    const prefs = await loadCliPrefs();
    expect(prefs.scope).toBe("mine");
    expect(prefs.verify).toBeUndefined(); // blank => auto-detect per project
  });

  it("persists a typed verify command to cli.json", async () => {
    await writeAppConfig({ gateway: { enabled: false, api_key: "" } });
    // No shared model => import prompt skipped. Asks: model, scope, url, share, verify.
    await runSetup(scriptedIO(["m", "local", "", "n", "pnpm build"]));
    const prefs = await loadCliPrefs();
    expect(prefs.model).toBe("m");
    expect(prefs.scope).toBe("local");
    expect(prefs.verify).toBe("pnpm build");
  });

  it('persists "off" to disable verify', async () => {
    await writeAppConfig({ gateway: { enabled: false, api_key: "" } });
    await runSetup(scriptedIO(["m", "mine", "", "n", "off"]));
    const prefs = await loadCliPrefs();
    expect(prefs.verify).toBe("off");
  });
});

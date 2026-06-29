/**
 * The setup wizard: `potluck setup`, also auto-run on first use when
 * ~/.potluck/cli.json is missing.
 *
 * It can IMPORT settings from the desktop app's ~/.potluck/config.json (default
 * model + coordinator URL), prompts for the CLI defaults, and PERSISTS:
 *   - CLI prefs  -> ~/.potluck/cli.json   (model, scope, gatewayUrl)
 *   - shared     -> ~/.potluck/config.json (ONLY active_model_id + coordinator_url,
 *                   opt-in) — it NEVER writes gateway.api_key and NEVER flips
 *                   gateway.enabled (the app owns those).
 */

import {
  AGENT_SCOPES,
  DEFAULT_BASE_URL,
  DEFAULT_SCOPE,
  loadCliPrefs,
  loadConfig,
  loadSharedPrefs,
  normalizeScope,
  saveCliPrefs,
  saveSharedSafeFields,
  type CliPrefs,
  type Scope,
} from "./config.js";
import { listModels } from "./gateway.js";
import { c } from "./ui.js";

export interface SetupIO {
  /** Prompt the user; resolves to null on EOF (non-interactive / Ctrl-D). */
  ask: (q: string) => Promise<string | null>;
  /** Write a line of wizard output. */
  out: (s: string) => void;
}

function isYes(answer: string | null, defaultYes: boolean): boolean {
  const v = (answer ?? "").trim().toLowerCase();
  if (v === "") return defaultYes;
  return v === "y" || v === "yes";
}

/**
 * Run the interactive setup wizard. Tolerant of a missing/disabled gateway and
 * of EOF (each prompt falls back to its default). Returns the prefs it saved.
 */
export async function runSetup(io: SetupIO): Promise<CliPrefs> {
  const { ask, out } = io;

  out(`${c.bold("Potluck CLI setup")}`);
  out(c.dim("Configure the defaults for `potluck`. Press Enter to accept a default.\n"));

  const cfg = await loadConfig();
  const shared = await loadSharedPrefs();
  const existing = await loadCliPrefs();

  // 1) Offer to import from the desktop app's config.json, if present.
  let importShared = false;
  if (shared.activeModelId || shared.coordinatorUrl) {
    out(`${c.cyan("Found Potluck app settings")} in ~/.potluck/config.json:`);
    if (shared.activeModelId) out(`  default model:   ${c.bold(shared.activeModelId)}`);
    if (shared.coordinatorUrl) out(`  coordinator URL: ${c.bold(shared.coordinatorUrl)}`);
    importShared = isYes(await ask(c.yellow("Import these as the CLI defaults? [Y/n] ")), true);
    out("");
  }

  // 2) Default model. Offer the gateway's installed list when reachable.
  const modelDefault =
    (importShared ? shared.activeModelId : undefined) ?? existing.model ?? shared.activeModelId;
  if (cfg.enabled && cfg.apiKey) {
    try {
      const models = await listModels(cfg);
      if (models.length > 0) {
        out(`${c.dim("installed models:")} ${models.map((m) => m.id).join(", ")}`);
      }
    } catch {
      out(c.dim("(could not list models — the gateway is unreachable right now)"));
    }
  }
  const modelAnswer = await ask(
    c.yellow(`Default model${modelDefault ? ` [${modelDefault}]` : " (blank = first installed)"}: `),
  );
  const model = (modelAnswer ?? "").trim() || modelDefault || undefined;

  // 3) Default scope.
  const scopeDefault: Scope = existing.scope ?? DEFAULT_SCOPE;
  out("");
  out(`${c.dim("scopes:")} ${c.bold("local")} = this machine, ${c.bold("mine")} = your machines (household)`);
  out(c.dim('       circle / cooperative are single-turn only — not for agent sessions yet'));
  const scopeAnswer = await ask(
    c.yellow(`Default scope [${AGENT_SCOPES.join(" / ")}] (${scopeDefault}): `),
  );
  const scopeRaw = (scopeAnswer ?? "").trim();
  let scope = scopeDefault;
  if (scopeRaw) {
    const resolved = normalizeScope(scopeRaw, scopeDefault);
    if (resolved.notice) out(c.dim(`  ${resolved.notice}`));
    scope = resolved.scope;
  }

  // 4) Gateway URL override.
  const urlDefault =
    existing.gatewayUrl ?? (cfg.baseURL !== DEFAULT_BASE_URL ? cfg.baseURL : DEFAULT_BASE_URL);
  out("");
  const urlAnswer = await ask(c.yellow(`Gateway URL [${urlDefault}]: `));
  const urlRaw = (urlAnswer ?? "").trim() || urlDefault;
  // Only persist an override when it differs from the built-in default.
  const gatewayUrl = urlRaw !== DEFAULT_BASE_URL ? urlRaw : undefined;

  // 5) Persist CLI prefs (cli.json).
  const prefs: CliPrefs = { scope };
  if (model) prefs.model = model;
  if (gatewayUrl) prefs.gatewayUrl = gatewayUrl;
  await saveCliPrefs(prefs);
  out(`\n${c.green("✓ saved CLI prefs")} ${c.dim("~/.potluck/cli.json")}`);

  // 6) Offer to write SAFE shared fields back to config.json (opt-in).
  const coordToShare = shared.coordinatorUrl;
  const haveShareable = Boolean(model) || Boolean(coordToShare);
  if (haveShareable) {
    const doShare = isYes(
      await ask(
        c.yellow(
          "Also save the default model" +
            (coordToShare ? " and coordinator URL" : "") +
            " back to the app config so both stay in sync? [y/N] ",
        ),
      ),
      false,
    );
    if (doShare) {
      await saveSharedSafeFields({
        model: model ?? undefined,
        coordinatorUrl: coordToShare ?? undefined,
      });
      out(
        `${c.green("✓ updated shared fields")} ${c.dim(
          "~/.potluck/config.json (active_model_id" +
            (coordToShare ? " + network.coordinator_url" : "") +
            "; gateway untouched)",
        )}`,
      );
    }
  }

  // 7) If the gateway is off, the CLI cannot turn it on — explain how.
  if (!cfg.enabled || !cfg.apiKey) {
    out(
      `\n${c.yellow("Note:")} the gateway is off (or has no key). The CLI cannot enable it.\n` +
        `Open the Potluck app and turn it on: ${c.bold("Settings -> Connect tools")}.`,
    );
  }

  return prefs;
}

export function printSetupHelp(out: (s: string) => void): void {
  out(
    [
      `${c.bold("potluck setup")} — configure CLI defaults`,
      "",
      "Walks you through the default model, the default network scope, and the",
      "gateway URL, then writes ~/.potluck/cli.json. Optionally imports settings",
      "from the Potluck app and writes safe shared fields (default model,",
      "coordinator URL) back to ~/.potluck/config.json. It never touches the",
      "gateway api_key or the enabled flag — the app owns those.",
      "",
      "Runs automatically the first time you use `potluck` in a terminal.",
      "",
    ].join("\n"),
  );
}

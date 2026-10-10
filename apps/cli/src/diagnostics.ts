import { loadCliPrefs, loadConfig, resolveScope } from "./config.js";
import { ControlError, meshSocketPath, requestJSON, runtimeURL, safeURL } from "./control.js";
import { diagnoseProject } from "./project-diagnostics.js";

export interface Check {
  id: string;
  state: "ok" | "warning" | "error";
  message: string;
  action?: string;
}
export interface DiagnosticReport {
  schemaVersion: 1;
  scope: "local" | "mine";
  ready: boolean;
  checks: Check[];
  runtime: { reachable: boolean; version: string | null; model: string | null };
  mesh: { reachable: boolean; paired: boolean | null; connected: boolean | null; peersOnline: number | null; peersKnown: number | null };
  gateway: { endpoint: string; configured: boolean; reachable: boolean; models: string[] };
  performance?: import("./status.js").PerformanceReport | null;
  /** The engine's answer check for the loaded model. Null when the engine has none; only `status` fills it. */
  acceptance?: import("./benchmark-stats.js").Acceptance | null;
}

const string = (v: unknown): string | null => typeof v === "string" ? v : null;
const count = (v: unknown): number | null => typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;
type Probe = { body: Record<string, unknown> } | { code: string };
async function probe(options: Parameters<typeof requestJSON>[0]): Promise<Probe> {
  try { return { body: await requestJSON(options) }; }
  catch (err) { return { code: err instanceof ControlError ? err.code : "unreachable" }; }
}

export async function diagnose(options: { timeout: number; scope?: string; signal?: AbortSignal }): Promise<DiagnosticReport> {
  const [cfg, prefs] = await Promise.all([loadConfig(), loadCliPrefs()]);
  const scope = resolveScope(options.scope, prefs.scope).scope;
  const configured = cfg.enabled && Boolean(cfg.apiKey);
  const [health, mesh, peers, gateway] = await Promise.all([
    probe({ url: `${runtimeURL()}/health`, timeout: options.timeout, signal: options.signal }),
    probe({ socketPath: meshSocketPath(), path: "/status", timeout: options.timeout, signal: options.signal }),
    probe({ socketPath: meshSocketPath(), path: "/peers", timeout: options.timeout, signal: options.signal }),
    configured ? probe({ url: `${cfg.baseURL}/models`, token: cfg.apiKey, timeout: options.timeout, signal: options.signal })
      : Promise.resolve({ code: "disabled" }),
  ]);
  const h = "body" in health ? health.body : null;
  const m = "body" in mesh ? mesh.body : null;
  const g = "body" in gateway ? gateway.body : null;
  const roster = "body" in peers && Array.isArray(peers.body.peers) ? peers.body.peers : null;
  const reachablePeers = roster ? roster.filter((peer: unknown) => peer && typeof peer === "object" && "reachable" in peer && peer.reachable === true).length : null;
  const models = Array.isArray(g?.data) ? g.data.flatMap((row: unknown) => {
    if (!row || typeof row !== "object" || !("id" in row)) return [];
    return typeof row.id === "string" ? [row.id] : [];
  }) : [];
  const report: DiagnosticReport = {
    schemaVersion: 1, scope, ready: false, checks: [],
    runtime: { reachable: h?.status === "ok", version: string(h?.version), model: string(h?.active_model_id) },
    mesh: { reachable: Boolean(m && typeof m.mesh_status === "string"),
      paired: m ? Boolean(m.node_id && m.mesh_ip) : null,
      connected: m ? m.coordinator_connection === "connected" : null,
      peersOnline: reachablePeers, peersKnown: roster?.length ?? count(m?.peers_online_count) },
    gateway: { endpoint: safeURL(cfg.baseURL), configured, reachable: Array.isArray(g?.data), models },
  };
  const checks = report.checks;
  const restore = h?.model_restore && typeof h.model_restore === "object"
    ? h.model_restore as Record<string, unknown> : null;
  if (restore?.status === "loading") checks.push({ id: "model_restore", state: "warning",
    message: "The saved local model is loading.", action: "Wait for loading to finish, then run potluck status again." });
  if (restore?.status === "failed") checks.push({ id: "model_restore", state: "warning",
    message: "The saved local model could not be restored.",
    action: "Check potluck logs --source runtime, then use potluck models load <model-id> to retry or potluck models unload <model-id> to clear it." });
  checks.push(report.runtime.reachable
    ? { id: "runtime", state: "ok", message: "Local runtime is running." }
    : { id: "runtime", state: "error", message: `Local runtime unavailable (${"code" in health ? health.code : "invalid_response"}).`,
      action: "Run potluck up or start the desktop app; check potluck logs --source runtime." });
  checks.push(report.mesh.reachable
    ? { id: "mesh", state: "ok", message: "Mesh service is reachable." }
    : { id: "mesh", state: scope === "mine" ? "error" : "warning",
      message: `Mesh service unavailable (${"code" in mesh ? mesh.code : "invalid_response"}).`,
      action: "Check the mesh service and socket permissions; use --scope local for this computer only." });
  if (report.mesh.reachable) {
    checks.push(report.mesh.paired && report.mesh.connected
      ? { id: "connection", state: "ok", message: "Household enrollment and coordinator connection are active." }
      : { id: "connection", state: scope === "mine" ? "error" : "warning",
        message: report.mesh.paired ? "Paired, but the coordinator is disconnected." : "This machine is not paired.",
        action: report.mesh.paired ? "Check coordinator reachability and potluck logs --source mesh."
          : "Run potluck connect to join your household." });
    if (scope === "mine") checks.push(reachablePeers && reachablePeers > 0
      ? { id: "household_peers", state: "ok", message: `${reachablePeers} other household machine(s) have a working mesh tunnel.` }
      : { id: "household_peers", state: report.runtime.model ? "warning" : "error",
        message: reachablePeers === null ? "Could not verify household peer reachability." : "No other household machines have a working mesh tunnel.",
        action: "Check that the other machine is awake and its mesh is connected. A coordinator connection alone does not prove machine-to-machine connectivity." });
  }
  checks.push(report.gateway.reachable
    ? { id: "gateway", state: "ok", message: "Gateway authentication and model discovery succeeded." }
    : { id: "gateway", state: "error", message: configured
      ? `Gateway check failed (${"code" in gateway ? gateway.code : "invalid_response"}).`
      : "The gateway is disabled or has no configured key.",
      action: configured ? "Check the gateway URL/key and runtime; credentials are never printed."
        : "Run potluck gateway enable (or Settings → Connect tools), or configure an existing gateway with POTLUCK_GATEWAY_URL and POTLUCK_GATEWAY_KEY." });
  checks.push(models.length
    ? { id: "models", state: "ok", message: `${models.length} model(s) advertised by the gateway; availability may change.` }
    : { id: "models", state: "error", message: "No models are advertised by the gateway.",
      action: "Use potluck models list, install, and load; or load a model on an eligible household machine." });
  if (scope === "local" && !report.runtime.model) checks.push({ id: "local_model", state: "error",
    message: "Local scope requires a model loaded on this computer.", action: "Load a local model." });
  checks.push(...await diagnoseProject(cfg, scope, options.timeout, undefined, options.signal));
  report.ready = checks.every(c => c.state !== "error");
  return report;
}

export function formatDiagnostics(report: DiagnosticReport, doctor: boolean): string {
  const lines = [`Potluck ${report.ready ? "ready" : "needs attention"} · scope: ${report.scope}`];
  for (const check of report.checks) {
    lines.push(`${check.state === "ok" ? "✓" : check.state === "warning" ? "!" : "✗"} ${check.message}`);
    if (doctor && check.action) lines.push(`  ${check.action}`);
  }
  if (report.runtime.model) lines.push(`Loaded model: ${report.runtime.model}`);
  if (report.mesh.peersOnline !== null) lines.push(`Other own machines reachable: ${report.mesh.peersOnline}${report.mesh.peersKnown !== null ? ` of ${report.mesh.peersKnown} in the roster` : ""}`);
  lines.push(`Gateway: ${report.gateway.endpoint}`);
  return lines.join("\n");
}

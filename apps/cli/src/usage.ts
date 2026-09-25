import { emitKeypressEvents, type Key } from "node:readline";
import type { ReadStream } from "node:tty";
import type { CliArgs } from "./args.js";
import { usageSnapshot, type UsageDirection, type UsageReport, type UsageView } from "./usage-data.js";
import { renderUsage, type UsageScreen } from "./usage-render.js";

export const usageHelp = `Usage: potluck usage [overview|models|requests|machines]
       potluck usage [view] --once [--window 1|7|30|90] [--direction consumed|served]
       potluck usage [view] --json [--watch] [--interval 1000-60000]
       potluck usage [view] [--theme dark|light] [--timeout 1-30000]

A live, read-only terminal dashboard. On a pipe or with --once, prints one snapshot.
--json emits one document; --json --watch emits newline-delimited documents.
Requires a dashboard-capable local runtime. Public CLI 0.1.1 predates this screen.

1-4 / tab: views      w: 1/7/30/90 days     d: your usage / contribution
up/down: scroll       space: pause         r: refresh        t: dark/light
q / esc / ctrl-c: quit                     ?: show this help

The window is rolling; chart buckets use UTC with partial first/last buckets.
Request history includes the latest 100 records; model comparisons show the top 20.
Counts are recorded on this computer, not merged across your fleet. Served rows
are work this runtime did for others. Machines probes only your owned household.
E2E tok/s is successful reported output / total request time, including prefill;
TTFT and duration averages use successful samples. Missing measurements show --.
GPU utilization, watts, live decode speed and context occupancy aren't measured.
No prompts, completions, credentials or peer identities appear in this view.
NO_COLOR removes color; TERM=dumb uses plain snapshots. Nothing is installed,
loaded, connected, or enabled by viewing usage. Refresh defaults to 2 seconds.
`;
const views: UsageView[] = ["overview", "models", "requests", "machines"];
const windows = [1, 7, 30, 90];
export interface UsageDependencies {
  input: ReadStream;
  output: { isTTY?: boolean; columns?: number; rows?: number; on: NodeJS.WriteStream["on"]; off: NodeJS.WriteStream["off"] };
  env: NodeJS.ProcessEnv;
  sample: (window: number, direction: UsageDirection, household: boolean, signal: AbortSignal) => Promise<UsageReport>;
}
/** Acquire only terminal state we can restore. Also used on job-control suspend. */
export function enterUsageTerminal(input: ReadStream, out: (s: string) => void): () => void {
  const raw = input.isRaw, wasFlowing = input.readableFlowing === true;
  let active = true;
  const restore = () => {
    if (!active) return; active = false;
    try { input.setRawMode(raw); } finally {
      if (!wasFlowing) input.pause();
      out("\x1b[0m\x1b[?25h\x1b[?1049l");
    }
  };
  try { input.setRawMode(true); input.resume(); out("\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H"); }
  catch (error) { restore(); throw error; }
  return restore;
}
export async function runUsage(args: CliArgs, out: (s: string) => void, deps?: UsageDependencies): Promise<number> {
  const input = deps?.input ?? process.stdin, output = deps?.output ?? process.stdout, env = deps?.env ?? process.env;
  const interactive = Boolean(input.isTTY && output.isTTY && env.TERM !== "dumb" && !args.json && !args.once);
  const watch = interactive || Boolean(args.watch);
  const state: UsageScreen = { view: (args.action ?? "overview") as UsageView, window: args.window ?? 7,
    direction: args.direction ?? "consumed", theme: args.theme ?? (env.COLORFGBG?.split(";").at(-1) === "15" ? "light" : "dark"),
    paused: false, offset: 0, report: null, error: false, refreshing: false };
  const controller = new AbortController();
  const sample = deps?.sample ?? ((window, direction, household, signal) => usageSnapshot(args, window, direction, household, signal));
  let help = false, revision = 0, wake: (() => void) | undefined, restore: (() => void) | undefined, suspended = false;
  const notify = () => { wake?.(); };
  const stop = () => { controller.abort(); notify(); };
  const draw = () => {
    if (suspended || controller.signal.aborted) return;
    if (help) {
      const lines = usageHelp.split("\n").slice(0, Math.max(1, (output.rows ?? 38) - 3));
      out("\x1b[2J\x1b[H" + lines.map(l => l.slice(0, Math.max(1, (output.columns ?? 100) - 1))).join("\n") + "\n\n? or esc closes help. q quits.\x1b[J");
      return;
    }
    out((interactive ? "\x1b[H" : "") + renderUsage(state, { width: Math.max(10, (output.columns ?? 110) - 1), height: interactive ? output.rows ?? 38 : 38,
      color: Boolean(output.isTTY && env.NO_COLOR === undefined && env.TERM !== "dumb"), unicode: env.TERM !== "dumb", interactive }) + (interactive ? "\x1b[J" : "\n"));
  };
  const keypress = (_text: string, key: Key = {}) => {
    if (key.name === "q" || (key.ctrl && key.name === "c") || (key.name === "escape" && !help)) { stop(); return; }
    if (key.name === "?" || _text === "?" || (key.name === "escape" && help)) { help = !help; draw(); return; }
    if (help) return;
    if (key.ctrl && key.name === "z" && process.platform !== "win32") { suspend(); return; }
    const old = `${state.view}:${state.window}:${state.direction}`;
    if (key.name && /^[1-4]$/.test(key.name)) state.view = views[Number(key.name) - 1];
    else if (key.name === "tab") state.view = views[(views.indexOf(state.view) + (key.shift ? 3 : 1)) % 4];
    else if (key.name === "w") state.window = windows[(windows.indexOf(state.window) + 1) % windows.length];
    else if (key.name === "d") state.direction = state.direction === "consumed" ? "served" : "consumed";
    else if (key.name === "t") state.theme = state.theme === "dark" ? "light" : "dark";
    else if (key.name === "space") { state.paused = !state.paused; notify(); }
    else if (key.name === "r") { revision++; notify(); }
    else if (["up", "down", "pageup", "pagedown"].includes(key.name ?? "")) {
      const length = state.view === "models" ? state.report?.models.length : state.view === "requests" ? state.report?.recent.length : 0;
      const step = key.name?.startsWith("page") ? 10 : 1;
      state.offset = Math.max(0, Math.min(Math.max(0, (length ?? 0) - 1), state.offset + (key.name?.endsWith("up") ? -step : step)));
    }
    if (`${state.view}:${state.window}:${state.direction}` !== old) {
      // Discard a pending sample if it belongs to the previous filter/view.
      state.offset = 0; revision++; state.report = null; state.error = false; notify();
    }
    draw();
  };
  const suspend = () => {
    if (!interactive || suspended) return;
    suspended = true; restore?.(); restore = undefined;
    process.kill(process.pid, "SIGSTOP");
  };
  const resume = () => {
    if (!suspended || controller.signal.aborted) return;
    suspended = false; restore = enterUsageTerminal(input, out); revision++; draw(); notify();
  };
  const onEnd = () => stop();
  const resize = () => { if (!suspended) { out("\x1b[2J"); draw(); } };
  const wait = (ms: number) => new Promise<void>(resolve => {
    const finish = () => { clearTimeout(timer); wake = undefined; resolve(); };
    const timer = setTimeout(finish, ms); wake = finish;
    if (controller.signal.aborted) finish();
  });
  process.on("SIGINT", stop); process.on("SIGTERM", stop);
  try {
    if (interactive) {
      emitKeypressEvents(input); restore = enterUsageTerminal(input, out);
      input.on("keypress", keypress); input.on("end", onEnd); output.on("resize", resize);
      process.on("SIGTSTP", suspend); process.on("SIGCONT", resume); draw();
    }
    let sampledRevision = -1;
    while (!controller.signal.aborted) {
      if (suspended || (state.paused && sampledRevision === revision)) { await wait(60000); continue; }
      const requestRevision = revision; state.refreshing = true;
      try {
        const report = await sample(state.window, state.direction, state.view === "machines", controller.signal);
        if (controller.signal.aborted) break;
        if (requestRevision !== revision) continue;
        state.report = report; state.error = false;
      } catch {
        if (controller.signal.aborted) break;
        if (requestRevision !== revision) continue;
        state.error = true;
      } finally { state.refreshing = false; }
      sampledRevision = requestRevision;
      if (args.json) out(JSON.stringify(state.error ? { schemaVersion: 1, available: false, error: "usage_unavailable",
        sampledAt: new Date().toISOString(), next: "Start or upgrade the local runtime; see potluck usage --help." } : state.report) + "\n");
      else draw();
      if (!watch) return state.error ? 1 : 0;
      await wait(args.interval ?? 2000);
    }
  } finally {
    controller.abort(); notify();
    process.off("SIGINT", stop); process.off("SIGTERM", stop); process.off("SIGTSTP", suspend); process.off("SIGCONT", resume);
    if (interactive) { input.off("keypress", keypress); input.off("end", onEnd); output.off("resize", resize); }
    restore?.();
  }
  return 0;
}

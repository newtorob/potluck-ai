/** Parse commands before touching config, starting a REPL, or making requests. */
export class UsageError extends Error {}

export type Command = "run" | "contribute" | "launch" | "setup" | "status" | "doctor" | "logs" | "up" | "down" | "serve" | "service" | "models" | "gateway" | "login" | "logout" | "whoami" | "household" | "connect" | "disconnect";
export interface CliArgs {
  help: boolean;
  version: boolean;
  print: boolean;
  yes: boolean;
  noVerify: boolean;
  json: boolean;
  model?: string;
  scope?: string;
  verify?: string;
  task?: string;
  command?: Command;
  action?: string;
  target?: string;
  source: "runtime" | "mesh" | "app";
  lines: number;
  timeout: number;
  wait?: boolean;
  local?: boolean;
  name?: string;
  output?: string;
  inviteFile?: string;
  noInput?: boolean;
  enableGateway?: boolean;
  signIn?: boolean;
  connect?: boolean;
  preferences?: boolean;
  withModel?: boolean;
  message?: string;
  jobs?: number;
  maxTokens?: number;
  minFreeMemory?: number;
  days?: string;
  timezone?: string;
}

const commands = new Set<Command>(["run", "contribute", "launch", "setup", "status", "doctor", "logs", "up", "down", "serve", "service", "models", "gateway", "login", "logout", "whoami", "household", "connect", "disconnect"]);

export function parseArgs(argv: string[]): CliArgs {
  const out: CliArgs = { help: false, version: false, print: false, yes: false,
    noVerify: false, json: false, source: "runtime", lines: 100, timeout: 3000 };
  const positional: string[] = [];
  const seen = new Set<string>();
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const [flag] = arg.split("=", 1);
    const value = (): string => {
      const v = arg.includes("=") ? arg.slice(arg.indexOf("=") + 1) : argv[++i];
      if (!v || v.startsWith("--")) throw new UsageError(`${flag} needs a value.`);
      return v;
    };
    seen.add(flag);
    if (arg === "--help" || arg === "-h") out.help = true;
    else if (arg === "--version" || arg === "-v") out.version = true;
    else if (arg === "--yes" || arg === "-y") out.yes = true;
    else if (arg === "--json") out.json = true;
    else if (arg === "--no-verify") out.noVerify = true;
    else if (arg === "--wait") out.wait = true;
    else if (arg === "--no-wait") out.wait = false;
    else if (arg === "--local") out.local = true;
    else if (arg === "--no-input") out.noInput = true;
    else if (arg === "--enable-gateway") out.enableGateway = true;
    else if (arg === "--sign-in") out.signIn = true;
    else if (arg === "--connect") out.connect = true;
    else if (arg === "--preferences") out.preferences = true;
    else if (arg === "--with-model") out.withModel = true;
    else if (flag === "--jobs" || flag === "--max-tokens") {
      const v = value(), min = flag === "--jobs" ? 0 : 1, max = flag === "--jobs" ? 64 : 32768;
      if (!/^\d+$/.test(v) || Number(v) < min || Number(v) > max) throw new UsageError(`${flag} must be an integer from ${min} to ${max}.`);
      if (flag === "--jobs") out.jobs = Number(v); else out.maxTokens = Number(v);
    }
    else if (flag === "--min-free-memory") {
      const v = value(); if (!/^\d+(?:\.\d+)?$/.test(v) || Number(v) > 1024) throw new UsageError("--min-free-memory must be 0–1024 GiB.");
      out.minFreeMemory = Number(v);
    }
    else if (flag === "--days") out.days = value();
    else if (flag === "--timezone") out.timezone = value();
    else if (flag === "--message") out.message = value();
    else if (flag === "--name") out.name = value();
    else if (flag === "--output") out.output = value();
    else if (flag === "--invite-file") out.inviteFile = value();
    else if (flag === "--model" || flag === "-m") out.model = value();
    else if (flag === "--scope") out.scope = value();
    else if (flag === "--verify") out.verify = value();
    else if (flag === "--print" || flag === "-p") { out.print = true; out.task = value(); }
    else if (flag === "--source") {
      const v = value();
      if (!["runtime", "mesh", "app"].includes(v)) throw new UsageError("--source must be runtime, mesh, or app.");
      out.source = v as CliArgs["source"];
    } else if (flag === "--lines" || flag === "--timeout") {
      const v = value();
      const max = flag === "--lines" ? 1000 : 300000;
      if (!/^\d+$/.test(v) || Number(v) < 1 || Number(v) > max) {
        throw new UsageError(`${flag} must be an integer from 1 to ${max}.`);
      }
      if (flag === "--lines") out.lines = Number(v); else out.timeout = Number(v);
    } else if (arg.startsWith("-")) throw new UsageError(`Unknown option: ${flag}`);
    else positional.push(arg);
  }
  if (positional.length) {
    if (!commands.has(positional[0] as Command)) {
      throw new UsageError("Unknown command. Run potluck --help, or use potluck -p \"task\".");
    }
    out.command = positional[0] as Command;
    if (out.command === "run") {
      if (positional.length > 2) throw new UsageError('Quote your prompt: potluck run "Summarize this".');
      out.task = positional[1];
    } else if (out.command === "contribute") {
      out.action = positional[1] || "status"; out.target = positional[2];
      if (!["status", "on", "off", "schedule", "limits"].includes(out.action) || positional.length > 3 || (out.target && out.action !== "schedule")) throw new UsageError("Use contribute status|on|off|schedule|limits.");
      if (out.action === "schedule" && !out.target && !out.help) throw new UsageError("Use contribute schedule HH:MM-HH:MM or off.");
      if ((out.days || out.timezone) && out.action !== "schedule") throw new UsageError("--days and --timezone require contribute schedule.");
      if ((out.jobs !== undefined || out.maxTokens !== undefined || out.minFreeMemory !== undefined) && out.action !== "limits") throw new UsageError("Limit options require contribute limits.");
    } else if (out.command === "launch") {
      out.action = positional[1];
      if ((!out.help && out.action !== "aider") || positional.length > 2) throw new UsageError("Use potluck launch aider [--model <id>] [--scope local|mine] [--message <prompt>].");
    } else if (out.command === "service") {
      out.action = positional[1] || "status";
      if (positional.length > 2 || !["install", "uninstall", "start", "stop", "restart", "status"].includes(out.action)) throw new UsageError("Use potluck service install|uninstall|start|stop|restart|status.");
    } else if (out.command === "models") {
      out.action = positional[1] || "list";
      if (!["list", "install", "load", "unload", "progress", "cancel", "autoload", "pin", "lock", "sync", "unpin"].includes(out.action)) throw new UsageError("Unknown model action. Run potluck models --help.");
      out.target = positional[2];
      if (positional.length > 3 || (["list", "lock", "sync", "unpin"].includes(out.action) && out.target)
          || (!["list", "autoload", "lock", "sync", "unpin"].includes(out.action) && !out.target && !out.help)) throw new UsageError("Use potluck models <action> <model-id>.");
      if (out.action === "autoload" && out.target && !["on", "off"].includes(out.target)) throw new UsageError("Use potluck models autoload [on|off].");
      if (out.target && (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(out.target) || out.target.length > 200)) throw new UsageError("Use a model ID from potluck models list.");
    } else if (out.command === "gateway") {
      out.action = positional[1] || "status";
      if (positional.length > 2 || !["status", "enable", "disable", "rotate"].includes(out.action)) throw new UsageError("Use potluck gateway status|enable|disable|rotate.");
    } else if (out.command === "household") {
      out.action = positional[1] || "list";
      if (positional.length > 2 || !["list", "create", "invite"].includes(out.action)) throw new UsageError("Use potluck household list|create|invite.");
      if (out.action === "invite" && !out.output && !out.help) throw new UsageError("Use household invite --output <private-file>.");
    } else if (positional.length !== 1) throw new UsageError("Unexpected argument. Run potluck --help.");
  }
  const operation = out.command;
  const allowed: Record<string, string[]> = {
    "--jobs": ["contribute"], "--max-tokens": ["contribute", "run"], "--min-free-memory": ["contribute"], "--days": ["contribute"], "--timezone": ["contribute"],
    "--wait": ["login"], "--no-wait": ["login"], "--local": ["logout"],
    "--name": ["household", "connect", "setup"], "--output": ["household"],
    "--invite-file": ["connect"], "--no-input": ["setup"], "--enable-gateway": ["setup"],
    "--message": ["launch"], "--with-model": ["setup"], "--sign-in": ["setup"], "--connect": ["setup"], "--preferences": ["setup"],
  };
  for (const [flag, supported] of Object.entries(allowed)) {
    if (seen.has(flag) && !supported.includes(out.command || "")) throw new UsageError(`${flag} requires ${supported.join(" or ")}.`);
  }
  if (seen.has("--wait") && seen.has("--no-wait")) throw new UsageError("Choose --wait or --no-wait.");
  if (out.name && (!out.name.trim() || out.name.length > 64 || /[\x00-\x1f\x7f]/.test(out.name))) throw new UsageError("--name must contain 1–64 printable characters.");
  if (out.command === "household" && ((out.name && out.action !== "create") || (out.output && out.action !== "invite"))) throw new UsageError("--name requires household create; --output requires household invite.");
  if (out.preferences && (out.noInput || out.json || out.enableGateway || out.signIn || out.connect || out.name || out.model || out.withModel)) throw new UsageError("--preferences is an interactive preferences-only wizard.");
  if (["up", "down"].includes(out.command || "") && !seen.has("--timeout")) out.timeout = 15000;
  if (out.command === "run" && !seen.has("--timeout")) out.timeout = 120000;
  if (out.command !== "run" && out.timeout > 30000) throw new UsageError("--timeout must be an integer from 1 to 30000 for this command.");
  if (out.command === "launch" && out.json) throw new UsageError("launch runs an external tool; JSON output is not supported.");
  if (out.command === "serve" && (out.json || seen.has("--timeout"))) throw new UsageError("serve runs in the foreground; use service status --json for reports.");
  if (out.command === "models" && !seen.has("--timeout")) out.timeout = 30000;
  if (["setup", "login", "logout", "household", "connect", "disconnect"].includes(out.command || "") && !seen.has("--timeout")) out.timeout = 30000;
  if (out.command && out.version) throw new UsageError("--version must be used without a command.");
  if (out.command === "logs" && seen.has("--timeout")) throw new UsageError("--timeout applies to status, doctor, up, and down.");
  if (out.command && (out.print || (out.model && !["setup", "launch", "run"].includes(out.command)) || out.verify || out.noVerify || out.yes)) {
    throw new UsageError("Agent options cannot be combined with a management command.");
  }
  if (!operation && (out.json || seen.has("--timeout"))) {
    throw new UsageError("--json and --timeout require a management command.");
  }
  if (out.command !== "logs" && (seen.has("--source") || seen.has("--lines"))) {
    throw new UsageError("--source and --lines require logs.");
  }
  if (out.scope && out.command && !["status", "doctor", "launch", "run"].includes(out.command)) {
    throw new UsageError("--scope is supported by status, doctor, and the coding agent.");
  }
  if (operation && out.scope && !["local", "mine"].includes(out.scope)) {
    throw new UsageError("Diagnostic --scope must be local or mine.");
  }
  if (out.verify !== undefined && out.noVerify) throw new UsageError("Choose --verify or --no-verify.");
  return out;
}

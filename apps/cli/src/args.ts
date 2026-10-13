/** Parse commands before touching config, starting a REPL, or making requests. */
export class UsageError extends Error {}

export type Command = "circle" | "usage" | "split" | "batch" | "sessions" | "run" | "contribute" | "launch" | "setup" | "status" | "doctor" | "logs" | "up" | "down" | "serve" | "service" | "models" | "gateway" | "login" | "logout" | "whoami" | "household" | "connect" | "disconnect" | "benchmark";
export interface CliArgs {
  help: boolean;
  version: boolean;
  print: boolean;
  yes: boolean;
  noVerify: boolean;
  json: boolean;
  model?: string;
  scope?: string;
  /** Which trusted circle a circle-scoped run goes to: a name or an ID. */
  circle?: string;
  verify?: string;
  task?: string;
  command?: Command;
  action?: string;
  target?: string;
  source: "runtime" | "mesh" | "app";
  lines: number;
  timeout: number;
  wait?: boolean;
  /** false when --no-browser: print the approval link instead of opening it. */
  browser?: boolean;
  local?: boolean;
  name?: string;
  output?: string;
  inviteFile?: string;
  noInput?: boolean;
  enableGateway?: boolean;
  signIn?: boolean;
  connect?: boolean;
  preferences?: boolean;
  /** benchmark: timed runs after the warm-up call. */
  runs?: number;
  /** benchmark: leave a model it switched to loaded / switch back to the one loaded before. */
  keep?: boolean;
  restore?: boolean;
  /** models test: the longer answer check / run it now even if a stored result exists. */
  full?: boolean;
  force?: boolean;
  withModel?: boolean;
  message?: string;
  jobs?: number;
  maxTokens?: number;
  minFreeMemory?: number;
  days?: string;
  timezone?: string;
  saveSession?: boolean;
  resume?: string;
  watch?: boolean;
  interval?: number;
  files?: string[];
  peers?: string[];
  context?: number;
  once?: boolean;
  window?: 1 | 7 | 30 | 90;
  direction?: "consumed" | "served";
  theme?: "dark" | "light";
}

const commands = new Set<Command>(["circle", "usage", "split", "batch", "sessions", "run", "contribute", "launch", "setup", "status", "doctor", "logs", "up", "down", "serve", "service", "models", "gateway", "login", "logout", "whoami", "household", "connect", "disconnect", "benchmark"]);

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
    else if (arg === "--no-browser") out.browser = false;
    else if (arg === "--local") out.local = true;
    else if (arg === "--no-input") out.noInput = true;
    else if (arg === "--enable-gateway") out.enableGateway = true;
    else if (arg === "--sign-in") out.signIn = true;
    else if (arg === "--connect") out.connect = true;
    else if (arg === "--preferences") out.preferences = true;
    else if (arg === "--once") out.once = true;
    else if (arg === "--keep") out.keep = true;
    else if (arg === "--restore") out.restore = true;
    else if (arg === "--full") out.full = true;
    else if (arg === "--force") out.force = true;
    else if (flag === "--runs") {
      const v = value();
      if (!/^\d+$/.test(v) || Number(v) < 1 || Number(v) > 10) throw new UsageError("--runs must be an integer from 1 to 10.");
      out.runs = Number(v);
    }
    else if (flag === "--window") {
      const v = value();
      if (!["1", "7", "30", "90"].includes(v)) throw new UsageError("--window must be 1, 7, 30, or 90 days.");
      out.window = Number(v) as CliArgs["window"];
    }
    else if (flag === "--direction") {
      const v = value();
      if (!["consumed", "served"].includes(v)) throw new UsageError("--direction must be consumed or served.");
      out.direction = v as CliArgs["direction"];
    }
    else if (flag === "--theme") {
      const v = value();
      if (!["dark", "light"].includes(v)) throw new UsageError("--theme must be dark or light.");
      out.theme = v as CliArgs["theme"];
    }
    else if (arg === "--watch") out.watch = true;
    else if (flag === "--interval") {
      const v = value(); if (!/^\d+$/.test(v) || Number(v) < 1000 || Number(v) > 60000) throw new UsageError("--interval must be 1000–60000 milliseconds.");
      out.interval = Number(v);
    }
    else if (arg === "--save-session") out.saveSession = true;
    else if (flag === "--resume") out.resume = value();
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
    else if (flag === "--peer") {
      const peer = value();
      if (peer.length > 128 || /[\x00-\x1f\x7f/\\]/.test(peer)) throw new UsageError("--peer requires an owned machine name or mesh IP.");
      (out.peers ??= []).push(peer);
    }
    else if (flag === "--context") {
      const v = value();
      if (!/^\d+$/.test(v) || Number(v) < 512 || Number(v) > 8192) throw new UsageError("--context must be 512–8192 tokens.");
      out.context = Number(v);
    }
    else if (flag === "--timezone") out.timezone = value();
    else if (flag === "--message") out.message = value();
    else if (flag === "--name") out.name = value();
    else if (flag === "--output") out.output = value();
    else if (flag === "--invite-file") out.inviteFile = value();
    else if (flag === "--model" || flag === "-m") out.model = value();
    else if (flag === "--scope") out.scope = value();
    else if (flag === "--circle") out.circle = value();
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
    if (out.command === "usage") {
      out.action = positional[1] || "overview";
      if (!["overview", "models", "requests", "machines"].includes(out.action) || positional.length > 2) throw new UsageError("Use potluck usage [overview|models|requests|machines].");
    } else if (out.command === "split") {
      out.action = positional[1] || "status";
      out.target = positional[2];
      if (!["status", "worker", "plan", "start", "stop"].includes(out.action) || positional.length > 3) throw new UsageError("Use potluck split status|worker|plan|start|stop.");
      if (!out.help) {
        if (out.action === "worker" && !["on", "off"].includes(out.target || "")) throw new UsageError("Use potluck split worker on|off.");
        if (["plan", "start"].includes(out.action)) {
          if (!out.target || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}$/.test(out.target)) throw new UsageError("Choose a pinned model ID.");
          if (!out.peers?.length || out.peers.length > 3 || new Set(out.peers).size !== out.peers.length) throw new UsageError("Select 1–3 distinct workers with --peer.");
        } else if (out.peers || out.context !== undefined) throw new UsageError("--peer and --context require split plan or start.");
        if (["status", "stop"].includes(out.action) && out.target) throw new UsageError("Unexpected split argument.");
      }
    } else if (out.command === "batch") {
      out.action = positional[1] || "list";
      if (!["submit", "status", "list", "resume", "cancel", "results", "delete"].includes(out.action)) throw new UsageError("Use batch submit|status|list|resume|cancel|results|delete.");
      if (out.action === "submit") {
        out.files = positional.slice(2);
        if (!out.help && (!out.files.length || out.files.length > 64 || !out.scope)) throw new UsageError("Use batch submit <file> [file...] --scope local|mine. Select 1–64 text files explicitly.");
      } else {
        out.target = positional[2];
        if (positional.length > 3 || (out.action === "list" && out.target) || (!out.help && out.action !== "list" && !out.target)) throw new UsageError("Use batch <action> <job-id>, or batch list.");
        if (out.target && !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(out.target)) throw new UsageError("Use the batch job ID returned by submit.");
      }
      if (out.jobs !== undefined && (out.action !== "submit" || out.jobs < 1 || out.jobs > 8)) throw new UsageError("Batch --jobs must be 1–8 and requires submit.");
      if (out.maxTokens !== undefined && (out.action !== "submit" || out.maxTokens < 64 || out.maxTokens > 2048)) throw new UsageError("Batch --max-tokens must be 64–2048 and requires submit.");
      if (out.scope && !["submit", "resume"].includes(out.action)) throw new UsageError("Batch --scope requires submit or resume.");
      if (out.output && out.action !== "results") throw new UsageError("Batch --output requires results.");
    } else if (out.command === "sessions") {
      out.action = positional[1] || "list"; out.target = positional[2];
      if (!["list", "delete"].includes(out.action) || positional.length > 3 || (out.target && out.action !== "delete") || (!out.help && out.action === "delete" && !out.target)) throw new UsageError("Use sessions list or sessions delete <session-id>.");
    } else if (out.command === "run") {
      if (positional.length > 2) throw new UsageError('Quote your prompt: potluck run "Summarize this".');
      out.task = positional[1];
    } else if (out.command === "contribute") {
      out.action = positional[1] || "status"; out.target = positional[2];
      if (!["status", "on", "off", "share", "schedule", "limits"].includes(out.action) || positional.length > 3 || (out.target && !["schedule", "share"].includes(out.action))) throw new UsageError("Use contribute status|on|off|share|schedule|limits.");
      if (out.action === "share" && !["mine", "circles", "everyone"].includes(out.target || "") && !out.help) throw new UsageError("Use contribute share mine|circles|everyone.");
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
      if (!["list", "available", "install", "load", "unload", "progress", "cancel", "autoload", "pin", "lock", "sync", "unpin", "test"].includes(out.action)) throw new UsageError("Unknown model action. Run potluck models --help.");
      out.target = positional[2];
      if (positional.length > 3 || (["list", "available", "lock", "sync", "unpin"].includes(out.action) && out.target)
          || (!["list", "available", "autoload", "lock", "sync", "unpin", "test"].includes(out.action) && !out.target && !out.help)) throw new UsageError("Use potluck models <action> <model-id>.");
      if (out.action === "autoload" && out.target && !["on", "off"].includes(out.target)) throw new UsageError("Use potluck models autoload [on|off].");
      if (out.target && (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(out.target) || out.target.length > 200)) throw new UsageError("Use a model ID from potluck models list.");
    } else if (out.command === "gateway") {
      out.action = positional[1] || "status";
      if (positional.length > 2 || !["status", "enable", "disable", "rotate"].includes(out.action)) throw new UsageError("Use potluck gateway status|enable|disable|rotate.");
    } else if (out.command === "circle") {
      out.action = positional[1] || "list"; out.target = positional[2];
      if (!["list", "create", "invite", "join", "leave", "delete"].includes(out.action) || positional.length > 3) throw new UsageError("Use potluck circle list|create|invite|join|leave|delete.");
      if (out.action === "list" ? out.target !== undefined : !out.target && !out.help) {
        throw new UsageError(out.action === "list" ? "circle list takes no name." : out.action === "create" ? 'Name the circle: potluck circle create "Friends".'
          : out.action === "join" ? "Paste the invite code or link: potluck circle join ABCD-EFGH-JKMN." : `Name the circle: potluck circle ${out.action} "Friends".`);
      }
    } else if (out.command === "household") {
      out.action = positional[1] || "list";
      if (positional.length > 2 || !["list", "create", "invite"].includes(out.action)) throw new UsageError("Use potluck household list|create|invite.");
      if (out.action === "invite" && !out.output && !out.help) throw new UsageError("Use household invite --output <private-file>.");
    } else if (positional.length !== 1) throw new UsageError("Unexpected argument. Run potluck --help.");
  }
  if (out.command && (out.saveSession || out.resume)) throw new UsageError("--save-session and --resume apply to the coding agent, not management commands.");
  if (out.saveSession && out.resume) throw new UsageError("--resume already saves the continued session.");
  if ((out.watch || out.interval !== undefined) && out.command !== "status" && out.command !== "usage" && !(out.command === "batch" && out.action === "status")) throw new UsageError("--watch and --interval require status, batch status, or usage.");
  if (out.interval !== undefined && !out.watch && out.command !== "usage") throw new UsageError("--interval requires --watch.");
  if ((out.peers || out.context !== undefined) && out.command !== "split") throw new UsageError("--peer and --context require split.");
  if (out.command === "split" && !seen.has("--timeout")) out.timeout = 30000;
  if (out.once && (out.watch || out.interval !== undefined)) throw new UsageError("Choose --once or live refresh options.");
  const operation = out.command;
  const allowed: Record<string, string[]> = {
    "--once": ["usage"], "--window": ["usage"], "--direction": ["usage"], "--theme": ["usage"],
    "--circle": ["run"],
    "--jobs": ["contribute", "batch"], "--max-tokens": ["contribute", "run", "batch"], "--min-free-memory": ["contribute"], "--days": ["contribute"], "--timezone": ["contribute"],
    "--wait": ["login"], "--no-wait": ["login"], "--no-browser": ["login", "setup"], "--local": ["logout"],
    "--name": ["household", "connect", "setup"], "--output": ["household", "batch"],
    "--invite-file": ["connect"], "--no-input": ["setup"], "--enable-gateway": ["setup"],
    "--message": ["launch"], "--with-model": ["setup"], "--sign-in": ["setup"], "--connect": ["setup"], "--preferences": ["setup"],
    "--runs": ["benchmark"], "--keep": ["benchmark"], "--restore": ["benchmark"],
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
  // A benchmark waits on model loads and 64-token generations, which a slow CPU can stretch past 30 s.
  if (out.command === "benchmark" && !seen.has("--timeout")) out.timeout = 120000;
  // The answer check generates text, which a slow CPU can stretch past 30 s.
  const modelTest = out.command === "models" && out.action === "test";
  if (modelTest && !seen.has("--timeout")) out.timeout = 120000;
  if (out.command !== "run" && out.command !== "benchmark" && !modelTest && out.timeout > 30000) throw new UsageError("--timeout must be an integer from 1 to 30000 for this command.");
  if (out.command === "launch" && out.json) throw new UsageError("launch runs an external tool; JSON output is not supported.");
  if (out.command === "serve" && (out.json || seen.has("--timeout"))) throw new UsageError("serve runs in the foreground; use service status --json for reports.");
  if (out.command === "models" && !modelTest && !seen.has("--timeout")) out.timeout = 30000;
  if ((out.full || out.force) && !modelTest) throw new UsageError("--full and --force require models test.");
  if (["setup", "login", "logout", "household", "connect", "disconnect", "circle"].includes(out.command || "") && !seen.has("--timeout")) out.timeout = 30000;
  if (out.command && out.version) throw new UsageError("--version must be used without a command.");
  if (out.command === "logs" && seen.has("--timeout")) throw new UsageError("--timeout applies to status, doctor, up, and down.");
  // --yes confirms joining, leaving or deleting a circle; elsewhere it is an agent option.
  const circleConfirm = out.command === "circle" && ["join", "leave", "delete"].includes(out.action || "");
  if (out.command && (out.print || (out.model && !["setup", "launch", "run", "benchmark"].includes(out.command)) || out.verify || out.noVerify || (out.yes && !circleConfirm && out.command !== "benchmark"))) {
    throw new UsageError("Agent options cannot be combined with a management command.");
  }
  if (!operation && (out.json || seen.has("--timeout"))) {
    throw new UsageError("--json and --timeout require a management command.");
  }
  if (out.command !== "logs" && (seen.has("--source") || seen.has("--lines"))) {
    throw new UsageError("--source and --lines require logs.");
  }
  if (out.scope && out.command && !["status", "doctor", "launch", "run", "batch", "benchmark"].includes(out.command)) {
    throw new UsageError("--scope is supported by status, doctor, benchmark, run, batch, launch, and the coding agent.");
  }
  // A single prompt can go to a trusted circle. Everything else that takes a
  // scope stays on the member's own machines.
  if (operation === "run" && out.scope === "circle") {
    /* allowed */
  } else if (operation && out.scope && !["local", "mine"].includes(out.scope)) {
    throw new UsageError(operation === "run" ? "Run --scope must be local, mine or circle." : "Diagnostic --scope must be local or mine.");
  }
  if (out.keep && out.restore) throw new UsageError("Choose --keep or --restore.");
  if (out.command === "benchmark" && out.model !== undefined && !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}$/.test(out.model)) throw new UsageError("Use a model ID from potluck models list.");
  if (out.circle !== undefined && out.scope !== "circle") throw new UsageError("--circle requires --scope circle.");
  if (out.verify !== undefined && out.noVerify) throw new UsageError("Choose --verify or --no-verify.");
  return out;
}

import { execFile } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { constants } from "node:fs";
import { lstat, mkdir, open, rename, unlink } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { setTimeout as delay } from "node:timers/promises";
import { runtimeURL } from "./control.js";
import { dataDir } from "./paths.js";
import { RuntimeError } from "./runtime.js";

const execute = promisify(execFile);
const marker = "Managed by Potluck CLI";
const xml = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&apos;");
// systemd expands % specifiers and $ variables even inside quoted arguments.
const unitQuote = (s: string, command = false) => '"' + s.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/%/g, "%%").replace(/\$/g, command ? "$$" : "$" ) + '"';
export interface ServiceSpec { platform: "darwin" | "linux"; label: string; file: string; content: string; log: string }

export function serviceSpec(platform = process.platform, home = homedir(), env = process.env): ServiceSpec {
  if (platform !== "darwin" && platform !== "linux") throw new RuntimeError("User services are available on macOS and Linux.");
  const data = dataDir();
  const label = "ai.trypotluck.runtime" + (data === join(homedir(), ".potluck") ? "" : "-" + createHash("sha256").update(data).digest("hex").slice(0, 12));
  const command = [process.execPath, fileURLToPath(new URL("./index.js", import.meta.url)), "serve"];
  const environment: Record<string, string> = { HOME: home, POTLUCK_DATA_DIR: data,
    POTLUCK_SIDECAR_URL: runtimeURL(env), PATH: env.PATH || `${dirname(process.execPath)}:/usr/bin:/bin:/usr/sbin:/sbin` };
  // No account/gateway/control secrets go into service definitions.
  for (const key of ["POTLUCK_RUNTIME_BIN", "POTLUCK_BIND_HOST", "POTLUCK_CLOUD_API_URL", "POTLUCK_COORDINATOR_URL", "POTLUCK_LOCAL_API_SOCKET", "LD_LIBRARY_PATH"])
    if (env[key]) environment[key] = env[key]!;
  if (Object.values(environment).some(v => /[\x00-\x1f\x7f]/.test(v))) throw new RuntimeError("Service configuration cannot contain control characters.");
  if (environment.POTLUCK_RUNTIME_BIN && !isAbsolute(environment.POTLUCK_RUNTIME_BIN)) throw new RuntimeError("POTLUCK_RUNTIME_BIN must be an absolute executable path.");
  const log = join(data, "logs/runtime.log");
  if (platform === "darwin") return { platform, label, log, file: join(home, "Library/LaunchAgents", label + ".plist"), content:
    `<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<!-- ${marker} -->\n<plist version="1.0"><dict>\n<key>Label</key><string>${label}</string>\n<key>ProgramArguments</key><array>${command.map(v => `<string>${xml(v)}</string>`).join("")}</array>\n<key>EnvironmentVariables</key><dict>${Object.entries(environment).map(([k, v]) => `<key>${k}</key><string>${xml(v)}</string>`).join("")}</dict>\n<key>RunAtLoad</key><true/>\n<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>\n<key>ThrottleInterval</key><integer>5</integer>\n<key>ExitTimeOut</key><integer>60</integer>\n<key>Umask</key><integer>63</integer>\n<key>StandardOutPath</key><string>${xml(log)}</string>\n<key>StandardErrorPath</key><string>${xml(log)}</string>\n</dict></plist>\n` };
  const config = env.XDG_CONFIG_HOME || join(home, ".config");
  if (!isAbsolute(config)) throw new RuntimeError("XDG_CONFIG_HOME must be an absolute path.");
  return { platform, label, log, file: join(config, "systemd/user", label + ".service"), content:
    `# ${marker}\n[Unit]\nDescription=Potluck local runtime\nAfter=network.target\n\n[Service]\nType=simple\nExecStart=${command.map(v => unitQuote(v, true)).join(" ")}\n${Object.entries(environment).map(([k, v]) => `Environment=${unitQuote(k + "=" + v)}`).join("\n")}\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=60\nKillMode=mixed\nUMask=0077\nNoNewPrivileges=true\n\n[Install]\nWantedBy=default.target\n` };
}

async function managedFile(spec: ServiceSpec): Promise<boolean> {
  try {
    const file = await open(spec.file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const stat = await file.stat();
      if (!stat.isFile() || stat.uid !== process.getuid?.() || stat.size > 65536 || (stat.mode & 0o022)) throw new Error();
      if (!(await file.readFile("utf8")).includes(marker)) throw new Error();
      return true;
    } finally { await file.close(); }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw new RuntimeError("Service file belongs to another installer or has unsafe permissions. It was left unchanged.");
  }
}

async function writeService(spec: ServiceSpec): Promise<void> {
  await managedFile(spec);
  await mkdir(dirname(spec.file), { recursive: true, mode: 0o700 });
  const parent = await lstat(dirname(spec.file));
  if (!parent.isDirectory() || parent.uid !== process.getuid?.() || (parent.mode & 0o022)) throw new RuntimeError("Unsafe service directory.");
  await mkdir(dirname(spec.log), { recursive: true, mode: 0o700 });
  const logs = await lstat(dirname(spec.log));
  if (!logs.isDirectory() || logs.uid !== process.getuid?.() || (logs.mode & 0o077)) throw new RuntimeError("Runtime logs require a private directory owned by you.");
  const log = await open(spec.log, constants.O_WRONLY | constants.O_APPEND | constants.O_CREAT | constants.O_NOFOLLOW | constants.O_NONBLOCK, 0o600);
  try {
    const info = await log.stat();
    if (!info.isFile() || info.uid !== process.getuid?.() || (info.mode & 0o077)) throw new RuntimeError("Runtime log must be a private file owned by you.");
  } finally { await log.close(); }
  const temporary = spec.file + "." + randomBytes(8).toString("hex");
  const file = await open(temporary, "wx", 0o600);
  try { await file.writeFile(spec.content); await file.sync(); }
  finally { await file.close(); }
  try { await rename(temporary, spec.file); }
  finally { await unlink(temporary).catch(() => {}); }
}

export async function manageService(action: string, timeout: number): Promise<{ schemaVersion: 1; manager: string; state: string; detail: string }> {
  if (process.getuid?.() === 0) throw new RuntimeError("Run potluck service as your own user, without sudo.");
  if (process.env.POTLUCK_HOMEBREW === "1") {
    const brew = process.env.POTLUCK_BREW_BIN;
    if (!brew || !isAbsolute(brew)) throw new RuntimeError("Homebrew manages this installation. Use brew services start|stop|restart|info potluck.");
    if (dataDir() !== join(homedir(), ".potluck")) throw new RuntimeError("Homebrew services use the default data directory. For an isolated runtime, use potluck serve or potluck up.");
    try {
      const verb = ({ install: "start", uninstall: "stop", status: "info" } as Record<string, string>)[action] || action;
      const result = await execute(brew, ["services", verb, "potluck", ...(action === "status" ? ["--json"] : [])],
        { timeout: 70000, maxBuffer: 65536, env: { ...process.env, HOMEBREW_NO_AUTO_UPDATE: "1" } });
      const info = action === "status" ? JSON.parse(result.stdout)[0] : null;
      return { schemaVersion: 1, manager: "homebrew", state: info?.status || (["stop", "uninstall"].includes(action) ? "stopped" : "started"),
        detail: "Managed by brew services. Stopping also disables automatic startup; models, settings and the formula remain installed. Runtime readiness: potluck status." };
    } catch { throw new RuntimeError("Homebrew could not complete the service request. Check brew services info potluck and potluck logs."); }
  }
  const spec = serviceSpec();
  const domain = `gui/${process.getuid!()}`;
  const target = `${domain}/${spec.label}`;
  const unit = spec.label + ".service";
  const call = async (args: string[], probe = false): Promise<string | null> => {
    try { return (await execute(spec.platform === "darwin" ? "/bin/launchctl" : "systemctl", args,
      { timeout: Math.max(timeout, 15000), maxBuffer: 65536 })).stdout; }
    catch (error) {
      if (probe && typeof (error as { code?: unknown }).code === "number") return null;
      throw new RuntimeError(spec.platform === "darwin"
        ? "launchd could not complete the request. Run from your logged-in macOS session; check potluck logs and service status."
        : "systemd could not complete the request. A user systemd session is required; check systemctl --user status " + unit + ".");
    }
  };
  const loaded = () => call(spec.platform === "darwin" ? ["print", target] : ["--user", "show", unit, "--property=LoadState,ActiveState,UnitFileState", "--no-pager"], true);
  const stop = async () => {
    if (spec.platform === "darwin") {
      if (await loaded() !== null) {
        await call(["bootout", target]);
        const deadline = Date.now() + 60000;
        while (await loaded() !== null) {
          if (Date.now() >= deadline) throw new RuntimeError("The service is still stopping. Check potluck logs before retrying.");
          await delay(100);
        }
      }
    }
    else if (await managedFile(spec)) await call(["--user", "stop", unit]);
  };
  if (action === "status") {
    const installed = await managedFile(spec);
    const status = await loaded();
    const active = spec.platform === "darwin" ? status !== null && /state = running/.test(status)
      : status !== null && /^ActiveState=active$/m.test(status);
    return { schemaVersion: 1, manager: spec.platform === "darwin" ? "launchd" : "systemd", state: active ? "running" : installed ? "installed" : "not-installed",
      detail: `${spec.file}. Runtime readiness: potluck status. ${spec.platform === "linux" ? "Service logs: journalctl --user -u " + unit : "Logs: potluck logs"}` };
  }
  if (action !== "install" && !(await managedFile(spec))) {
    if (action === "uninstall" || action === "stop") return { schemaVersion: 1, manager: spec.platform, state: "not-installed", detail: "No service installed. Your data is unchanged." };
    throw new RuntimeError("Install the user service first: potluck service install.");
  }
  if (action === "install") {
    // Stop only a service definition already created by this installer.
    if (await managedFile(spec)) await stop();
    else {
      const status = await loaded();
      if (status !== null && (spec.platform === "darwin" || /^LoadState=(?!not-found$).+$/m.test(status)))
        throw new RuntimeError("Another installer manages this service label. Stop it through that installer.");
    }
    await writeService(spec);
    if (spec.platform === "linux") { await call(["--user", "daemon-reload"]); await call(["--user", "enable", unit]); }
  }
  if (action === "stop" || action === "restart" || action === "uninstall") await stop();
  if (action === "uninstall") {
    if (spec.platform === "linux") await call(["--user", "disable", unit]);
    await unlink(spec.file);
    if (spec.platform === "linux") await call(["--user", "daemon-reload"]);
  } else if (["install", "start", "restart"].includes(action)) {
    if (spec.platform === "darwin") {
      if (await loaded() === null) await call(["bootstrap", domain, spec.file]);
      await call(["kickstart", target]);
    } else await call(["--user", "start", unit]);
  }
  return { schemaVersion: 1, manager: spec.platform === "darwin" ? "launchd" : "systemd",
    state: action === "uninstall" ? "not-installed" : action === "stop" ? "stopped" : "started",
    detail: action === "uninstall" ? "Service removed. Models, accounts, settings and mesh enrollment are unchanged."
      : "Use potluck status to check runtime readiness. An existing app/CLI runtime keeps ownership until it exits." };
}

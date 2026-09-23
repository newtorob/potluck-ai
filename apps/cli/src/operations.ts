import type { CliArgs } from "./args.js";
import { diagnose, formatDiagnostics } from "./diagnostics.js";
import { plainText, readLogs } from "./logs.js";
import { startRuntime, stopRuntime } from "./runtime.js";
import { manageGateway, manageModels } from "./manage.js";
import { connect, disconnect, formatMember, household, login, logout, memberStatus } from "./member.js";
import { runMachineSetup } from "./operational-setup.js";
import { serveRuntime } from "./serve.js";
import { manageService } from "./service.js";

export function operationHelp(command: string): string {
  if (command === "serve") return "Usage: potluck serve\nRun the shared runtime in the foreground for launchd, systemd, or brew services. Waits while another launcher owns it. Ctrl-C stops only this launcher's child.\n";
  if (command === "service") return "Usage: potluck service [install|uninstall|start|stop|restart|status] [--json]\nManage the current user's runtime service on macOS/Linux. install enables startup at login and starts it. stop keeps login startup enabled; uninstall removes it and keeps your data.\nHomebrew installations use brew services. Linux startup without a login requires administrator-configured user lingering.\n";
  if (command === "setup") return "Usage: potluck setup [--no-input] [--enable-gateway] [--sign-in] [--connect] [--name <machine-name>] [--json]\nStart the local engine and resume setup. Gateway access, account login, and household connection require opt-in.\nWith --json/--no-input only explicitly requested steps run. Browser approval can happen on another device.\nUse potluck setup --preferences for coding-agent defaults.\n";
  if (command === "login") return "Usage: potluck login [--wait|--no-wait] [--json]\nApprove the displayed code in a browser on any device. Interactive terminals wait by default; JSON and piped invocations return the pending code.\nRun again with --wait after approval to finish. Ctrl-C leaves the request resumable until expiry.\n";
  if (command === "logout") return "Usage: potluck logout [--local] [--json]\nRevoke this member session and remove local sign-in. --local removes it here without server revocation. Mesh enrollment is separate; use disconnect to unpair.\n";
  if (command === "whoami") return "Usage: potluck whoami [--json]\nShow the locally stored member profile without printing credentials.\n";
  if (command === "household") return "Usage: potluck household list [--json]\n       potluck household create [--name <household-name>] [--json]\n       potluck household invite --output <new-private-file> [--json]\nInvitations are one-use credentials, saved with permissions 0600 and never printed.\n";
  if (command === "connect") return "Usage: potluck connect [--invite-file <private-file>] [--name <machine-name>] [--json]\nConnect through the installed mesh daemon using your signed-in household or a private invitation file.\n";
  if (command === "disconnect") return "Usage: potluck disconnect [--json]\nRemove this machine's local mesh enrollment and keys. The old coordinator entry remains until cleanup.\n";
  if (command === "gateway") return "Usage: potluck gateway [status|enable|disable|rotate] [--json]\nEnable preserves an existing key; rotate invalidates the old key. Keys are never printed.\n";
  if (command === "models") return "Usage: potluck models list [--json]\n       potluck models install|load|unload|progress|cancel <model-id> [--json] [--timeout 1-30000]\n       potluck models autoload [on|off] [--json]\n       potluck models pin <model-id> [--json]\n       potluck models lock|sync|unpin [--json]\nPin records installed GGUF bytes in potluck.lock. sync installs/verifies those exact bytes. lock shows the project pin; unpin explicitly removes it.\nInstall starts a background download; use progress to check it. Successful local loads restore on engine restart; unload clears the choice.\n";
  if (command === "up" || command === "down") return `Usage: potluck ${command} [--timeout 1-30000] [--json]\n${command === "up" ? "Start or attach to the local runtime (macOS/Linux)." : "Gracefully stop a CLI-owned runtime (macOS/Linux)."}\n`;
  return command === "logs"
    ? "Usage: potluck logs [--source runtime|mesh|app] [--lines 1-1000] [--json]\nReads a bounded log tail and redacts common credentials. Does not follow logs.\n"
    : `Usage: potluck ${command} [--scope local|mine] [--timeout 1-30000] [--json]\nRead-only checks of runtime, mesh, gateway authentication, and models.\nExit 0: ready; 1: needs attention; 2: invalid command/options.\n`;
}

export async function runOperation(args: CliArgs, out: (value: string) => void): Promise<number> {
  if (args.help) { out(operationHelp(args.command!)); return 0; }
  if (args.command === "serve") return serveRuntime();
  if (args.command === "service") {
    const report = await manageService(args.action!, args.timeout);
    out(args.json ? JSON.stringify(report) + "\n" : plainText(`Service ${report.state} (${report.manager}).\n${report.detail}\n`));
    return 0;
  }
  if (args.command === "setup") {
    const report = await runMachineSetup(args);
    out(args.json ? JSON.stringify(report) + "\n" : plainText(`Setup ${report.state}.\n${report.account && (report.account as Record<string, unknown>).state === "pending" ? formatMember(report.account as Record<string, unknown>) + "\n" : ""}${report.next}\n`));
    return report.state === "configured" ? 0 : 1;
  }
  if (["login", "logout", "whoami", "household", "connect", "disconnect"].includes(args.command!)) {
    const report = await (args.command === "login" ? login(args, text => process.stderr.write(plainText(text)))
      : args.command === "logout" ? logout(args) : args.command === "whoami" ? memberStatus(args)
      : args.command === "household" ? household(args) : args.command === "connect" ? connect(args) : disconnect(args));
    out(args.json ? JSON.stringify(report) + "\n" : plainText(formatMember(report)) + "\n");
    return ["denied", "expired"].includes(String(report.state)) || (args.command === "whoami" && !report.signedIn) ? 1 : 0;
  }
  if (args.command === "models" || args.command === "gateway") {
    const report = await (args.command === "models" ? manageModels(args) : manageGateway(args));
    const human = args.command === "gateway"
      ? `Gateway ${report.enabled ? "enabled" : "disabled"}. Key ${report.keyConfigured ? "configured" : "missing"}.${report.rotated ? " Previous key invalidated." : ""}`
      : report.action === "autoload"
        ? `Startup model restore ${report.enabled ? "enabled" : "disabled"}. Saved local model: ${report.model || "none"}. Applies on the next engine start.`
      : Array.isArray(report.models)
        ? report.models.map(m => `${m.id}\t${m.installed ? "installed" : "available"}${m.name ? `\t${m.name}` : ""}`).join("\n") || "No models available."
        : `${report.model}: ${report.status || "request completed"}${typeof report.progress === "number" ? ` (${report.progress}%)` : ""}${report.sha256 ? `\nSHA-256: ${report.sha256}` : ""}${report.path ? `\nLock: ${report.path}` : ""}${report.next ? `\n${report.next}` : ""}`;
    out(args.json ? JSON.stringify(report) + "\n" : plainText(human) + "\n");
    return report.status === "error" || report.status === "failed" ? 1 : 0;
  }
  if (args.command === "up" || args.command === "down") {
    const report = await (args.command === "up" ? startRuntime(args.timeout) : stopRuntime(args.timeout));
    out(args.json ? JSON.stringify(report) + "\n" : `Runtime ${report.state}${report.owner ? ` (${report.owner}, PID ${report.pid})` : ""}.\n`);
    return 0;
  }
  if (args.command === "logs") {
    const report = await readLogs(args.source, args.lines);
    out(args.json ? JSON.stringify(report) + "\n"
      : plainText(report.error ? `Logs: ${report.error}\n${report.action || "Check the log file permissions."}\n`
        : `${report.path}\n${report.lines.join("\n")}\n${report.truncated ? "(earlier log output omitted)\n" : ""}`));
    return report.error ? 1 : 0;
  }
  const report = await diagnose(args);
  out(args.json ? JSON.stringify(report) + "\n" : plainText(formatDiagnostics(report, args.command === "doctor")) + "\n");
  return report.ready ? 0 : 1;
}

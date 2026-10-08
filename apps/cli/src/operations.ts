import { launchAider } from "./launch.js";
import { runBatch } from "./batch.js";
import { runSplit } from "./split.js";
import { formatAvailability, type Availability } from "./availability.js";
import { manageContribution, formatContribution } from "./contribute.js";
import { runInference } from "./inference.js";
import { manageSessions } from "./sessions.js";
import { runUsage, usageHelp } from "./usage.js";
import { runStatus } from "./status.js";
import type { CliArgs } from "./args.js";
import { diagnose, formatDiagnostics } from "./diagnostics.js";
import { plainText, readLogs } from "./logs.js";
import { ensureRuntime, startRuntime, stopRuntime } from "./runtime.js";
import { manageGateway, manageModels } from "./manage.js";
import { connect, disconnect, formatMember, household, login, logout, memberStatus } from "./member.js";
import { formatCircles, manageCircles } from "./circle.js";
import { runMachineSetup } from "./operational-setup.js";
import { serveRuntime } from "./serve.js";
import { manageService } from "./service.js";

export function operationHelp(command: string): string {
  if (command === "usage") return usageHelp;
  if (command === "split") return "Usage: potluck split worker on|off [--json]\n       potluck split plan|start <pinned-model> --peer <owned-machine> [--peer <another>] [--context 512-8192] [--json]\n       potluck split status|stop [--json]\nRun one pinned GGUF across your own paired machines. Requires a split-capable runtime on each machine. Unload each worker's local model, then enable worker sharing explicitly. Run plan/start on the head computer holding the exact potluck.lock weights; peers are selected from your current mesh roster. Start loads in the background; status reports ready or failed. No cloud fallback or automatic model downloads. Stop releases the group. Runtime restart or interrupted inference requires an explicit start; turning a worker off interrupts its current split. This is a capacity feature, not a speed guarantee.\n";
  if (command === "batch") return "Usage: potluck batch submit <file> [file...] --scope local|mine [--jobs 1-8] [--max-tokens 64-2048] [--json]\n       potluck batch status <id> [--watch] [--interval 1000-60000] [--json]\n       potluck batch list|resume|cancel|results|delete [id] [--json]\n       potluck batch results <id> --output <new-file> [--json]\nSummarizes 1–64 explicitly selected UTF-8 files (16 KiB each, 256 KiB total), with no tools or file edits. Requires potluck.lock and a running batch-capable runtime. Submit snapshots input privately on this computer; the runtime schedules work after the terminal exits. Mine scope sends content only to your own eligible computers. Completed results survive interruption; partial attempts retry at most three times per file. Runtime restart pauses work; resume requires the original pin and scope and reuses saved input. Ctrl-C stops watching, not the batch. Cancel stops new work and drains active requests. Delete removes the saved job, inputs and outputs.\n";
  if (command === "status") return "Usage: potluck status [--scope local|mine] [--json] [--timeout 1-30000]\n       potluck status --watch [--interval 1000-60000] [--json]\nLive runtime, own-machine availability, recorded latency and last benchmark metrics. Watch emits newline-delimited JSON or refreshes the terminal, without overlapping polls. Ctrl-C stops watching. Missing measurements are unavailable, not zero.\n";
  if (command === "sessions") return "Usage: potluck sessions list [--json]\n       potluck sessions delete <session-id> [--json]\nOpt in with potluck --save-session or potluck --resume <id>. History is stored privately on this machine and requires a project model pin. Resume from the original working directory. Each resume forks a new session; saved tool calls are never replayed.\n";
  if (command === "run") return 'Usage: potluck run ["prompt"] [--model <id>] [--scope local|mine|circle] [--circle <name>] [--max-tokens 1-32768] [--timeout 1-300000] [--json]\nPlain inference with no file-editing tools. With --scope circle the prompt goes to a machine in one of your trusted circles, which sees it; name the circle with --circle unless you have only one, and pass a --model the circle is offering. A project with a model lock cannot use a circle. Piped stdin is appended to the prompt (combined limit 1 MiB). Project model locks apply. Default timeout: 120000 ms. Text streams to stdout; JSON is emitted only after a complete response. Exit 124: timeout; 130: cancelled; 1: inference failed; 2: invalid input.\n';
  if (command === "contribute") return "Usage: potluck contribute status|on|off [--json]\n       potluck contribute share mine|circles|everyone\n       potluck contribute limits [--jobs 0-64] [--max-tokens 1-32768] [--min-free-memory GiB]\n       potluck contribute schedule HH:MM-HH:MM [--days mon,tue,...] [--timezone Area/City]\n       potluck contribute schedule off\nControls coordinator jobs served by this runtime. Schedule boundaries stop new admissions; existing jobs finish. Overnight windows belong to the starting weekday. Free-memory checks are an admission threshold, not a process memory ceiling. No GPU-percentage throttle is claimed. Off stops the contributor connection. Share chooses who else this machine answers while contribution is on: mine is your own machines only, circles adds members of your trusted circles, everyone adds the open pool where this build has it.\n";
  if (command === "launch") return "Usage: potluck launch aider [--model <id>] [--scope local|mine] [--message <prompt>]\nLaunch installed Aider through a temporary authenticated local bridge that enforces the selected model, scope and project lock. Existing tool config files remain unchanged.\n";
  if (command === "serve") return "Usage: potluck serve\nRun the shared runtime in the foreground for launchd, systemd, or brew services. Waits while another launcher owns it. Ctrl-C stops only this launcher's child.\n";
  if (command === "service") return "Usage: potluck service [install|uninstall|start|stop|restart|status] [--json]\nManage the current user's runtime service on macOS/Linux. install enables startup at login and starts it. stop keeps login startup enabled; uninstall removes it and keeps your data.\nHomebrew installations use brew services. Linux startup without a login requires administrator-configured user lingering.\n";
  if (command === "setup") return "Usage: potluck setup [--no-input] [--with-model|--model <id>] [--enable-gateway] [--sign-in] [--no-browser] [--connect] [--name <machine-name>] [--json]\nStart the local engine and resume setup. Interactive setup recommends, downloads, loads and tests a fitting model; unattended model setup requires --with-model or --model. Gateway access, account login, and household connection require opt-in.\nWith --json/--no-input only explicitly requested steps run. Browser approval can happen on another device.\nUse potluck setup --preferences for coding-agent defaults.\n";
  if (command === "login") return "Usage: potluck login [--wait|--no-wait] [--no-browser] [--json]\nStarts the Potluck engine in the background if it is not running, then opens your browser to approve this terminal with the code filled in. Over SSH, without a display, or with --no-browser, it prints a link to open on any device instead.\nInteractive terminals wait by default; JSON and piped invocations return the pending code. Run again with --wait after approval to finish. Ctrl-C leaves the request resumable until expiry.\n";
  if (command === "logout") return "Usage: potluck logout [--local] [--json]\nRevoke this member session and remove local sign-in. --local removes it here without server revocation. Mesh enrollment is separate; use disconnect to unpair.\n";
  if (command === "whoami") return "Usage: potluck whoami [--json]\nShow the locally stored member profile without printing credentials.\n";
  if (command === "circle") return 'Usage: potluck circle list [--json]\n       potluck circle create "<name>"\n       potluck circle invite "<circle>"\n       potluck circle join <invite-code-or-link> [--yes]\n       potluck circle leave "<circle>" --yes\n       potluck circle delete "<circle>" --yes\nTrusted circles are people who lend each other spare compute. Invite makes a code that works once. Join shows what the invite is for and joins only with --yes. List shows who is in each circle and which models its other members are offering now. To lend this machine: potluck contribute share circles, then potluck contribute on. To borrow: potluck run --scope circle.\n';
  if (command === "household") return "Usage: potluck household list [--json]\n       potluck household create [--name <household-name>] [--json]\n       potluck household invite --output <new-private-file> [--json]\nInvitations are one-use credentials, saved with permissions 0600 and never printed.\n";
  if (command === "connect") return "Usage: potluck connect [--invite-file <private-file>] [--name <machine-name>] [--json]\nConnect through the installed mesh daemon using your signed-in household or a private invitation file.\n";
  if (command === "disconnect") return "Usage: potluck disconnect [--json]\nRemove this machine's local mesh enrollment and keys. The old coordinator entry remains until cleanup.\n";
  if (command === "gateway") return "Usage: potluck gateway [status|enable|disable|rotate] [--json]\nEnable preserves an existing key; rotate invalidates the old key. Keys are never printed.\n";
  if (command === "models") return "Usage: potluck models list|available [--json]\n       potluck models install|load|unload|progress|cancel <model-id> [--json] [--timeout 1-30000]\n       potluck models autoload [on|off] [--json]\n       potluck models pin <model-id> [--json]\n       potluck models lock|sync|unpin [--json]\navailable shows live owned-machine models and split workers; checks the current project pin without changing it.\nPin records installed GGUF bytes in potluck.lock. sync installs/verifies those exact bytes. lock shows the project pin; unpin explicitly removes it.\nInstall starts a background download; use progress to check it. Successful local loads restore on engine restart; unload clears the choice.\n";
  if (command === "up" || command === "down") return `Usage: potluck ${command} [--timeout 1-30000] [--json]\n${command === "up" ? "Start or attach to the local runtime (macOS/Linux)." : "Gracefully stop a CLI-owned runtime (macOS/Linux)."}\n`;
  return command === "logs"
    ? "Usage: potluck logs [--source runtime|mesh|app] [--lines 1-1000] [--json]\nReads a bounded log tail and redacts common credentials. Does not follow logs.\n"
    : `Usage: potluck ${command} [--scope local|mine] [--timeout 1-30000] [--json]\nRead-only checks of runtime, mesh, gateway authentication, and models.\nExit 0: ready; 1: needs attention; 2: invalid command/options.\n`;
}

export async function runOperation(args: CliArgs, out: (value: string) => void): Promise<number> {
  if (args.help) { out(operationHelp(args.command!)); return 0; }
  if (args.command === "usage") return runUsage(args, out);
  if (args.command === "split") return runSplit(args, out);
  if (args.command === "batch") return runBatch(args, out);
  if (args.command === "status") return runStatus(args, out);
  if (args.command === "run") return runInference(args, out);
  if (args.command === "sessions") {
    const report = await manageSessions(args.action!, args.target);
    out(args.json ? JSON.stringify(report) + "\n" : plainText(report.deleted ? `Deleted session ${report.deleted}.\n` :
      ((report.sessions as Array<Record<string, unknown>>).map(s => `${s.id}\t${s.model}\t${s.scope}\t${s.updatedAt}\t${s.cwd}`).join("\n") || "No saved sessions.") + `\n${report.unreadable ? `${report.unreadable} unreadable session(s) skipped.\n` : ""}`));
    return 0;
  }
  if (args.command === "circle") {
    await ensureRuntime(text => process.stderr.write(plainText(text)));
    const report = await manageCircles(args);
    out(args.json ? JSON.stringify(report) + "\n" : plainText(formatCircles(report)));
    return 0;
  }
  if (args.command === "contribute") {
    const report = await manageContribution(args);
    out(args.json ? JSON.stringify(report) + "\n" : plainText(formatContribution(report)));
    return ["error", "rejected"].includes(String(report.state)) ? 1 : 0;
  }
  if (args.command === "launch") return launchAider(args);
  if (args.command === "serve") return serveRuntime();
  if (args.command === "service") {
    const report = await manageService(args.action!, args.timeout);
    out(args.json ? JSON.stringify(report) + "\n" : plainText(`Service ${report.state} (${report.manager}).\n${report.detail}\n`));
    return 0;
  }
  if (args.command === "setup") {
    const report = await runMachineSetup(args);
    out(args.json ? JSON.stringify(report) + "\n" : plainText(`Setup ${report.state}.\n${report.account && (report.account as Record<string, unknown>).state === "pending" ? formatMember(report.account as Record<string, unknown>) + "\n" : ""}${report.next}\n`));
    return ["configured", "model_ready"].includes(String(report.state)) ? 0 : 1;
  }
  if (["login", "logout", "whoami", "household", "connect", "disconnect"].includes(args.command!)) {
    // disconnect talks to the mesh daemon directly; the rest need the engine.
    if (args.command !== "disconnect") await ensureRuntime(text => process.stderr.write(plainText(text)));
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
      : report.action === "available"
        ? formatAvailability(report as unknown as Availability)
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

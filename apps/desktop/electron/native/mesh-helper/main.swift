// mesh-helper — tiny CLI wrapping SMAppService for the Potluck mesh
// LaunchDaemon (Label "ai.trypotluck.mesh").
//
// WHY THIS BINARY EXISTS AND WHERE IT MUST LIVE
// ----------------------------------------------
// SMAppService.daemon(plistName:) resolves the embedded LaunchDaemon plist at
// Contents/Library/LaunchDaemons/<plistName> RELATIVE TO THE CALLING
// PROCESS'S OWN MAIN BUNDLE (CFBundleGetMainBundle()). For a bare Mach-O
// executable with no Info.plist of its own, that resolution only lands on
// the enclosing .app when the executable sits at the canonical
// <Something>.app/Contents/MacOS/<executable> location — which is why this
// binary is packaged (via electron-builder's mac.extraFiles, see
// apps/desktop/electron/package.json) as a SIBLING of the main Electron
// executable: Contents/MacOS/mesh-helper, next to Contents/MacOS/Potluck AI.
// Placing it under Contents/Resources/ instead would NOT resolve correctly.
//
// main.js's mesh:daemon-status / mesh:enable / mesh:open-login-items IPC
// handlers shell out to this binary (execFile) and parse stdout. See
// getMeshHelperPath() in main.js for the exact path it is invoked at.
//
// CONTRACT (stdout / exit code — keep in sync with main.js's parsing):
//
//   status
//     Prints exactly one of: notRegistered | requiresApproval | enabled | notFound
//     to stdout, exit 0. Never fails — even "no such daemon" is a normal,
//     expected status (notRegistered / notFound), not an error.
//
//   register
//     Calls SMAppService.register(). On success, prints the RESULTING status
//     token (same vocabulary as `status`) to stdout, exit 0.
//     IMPORTANT: requiresApproval is a SUCCESSFUL outcome here — the daemon
//     IS registered, it just needs the user to flip it on in System
//     Settings > Login Items & Extensions. It is not treated as an error.
//     On failure (e.g. code-signing / Team ID mismatch, plist validation
//     error): message to stderr, exit 1.
//
//   unregister
//     Calls SMAppService.unregister(). Prints the resulting status token to
//     stdout, exit 0 on success. On failure: stderr, exit 1.
//
//   open-login-items
//     Calls SMAppService.openSystemSettingsLoginItems() (fire-and-forget,
//     does not throw). Prints "opened" to stdout, exit 0.
//
//   anything else / missing argument
//     Usage message to stderr, exit 64 (EX_USAGE).

import Foundation
import ServiceManagement

/// Must exactly match the plist file name (and its internal Label key)
/// embedded at Contents/Library/LaunchDaemons/ — see
/// packaging/daemons/ai.trypotluck.mesh.plist and the mac.extraFiles entry
/// in apps/desktop/electron/package.json that places it there.
let daemonPlistName = "ai.trypotluck.mesh.plist"

/// Maps an SMAppService.Status case to the stable machine-readable token
/// main.js parses from stdout. Keep in sync with computeMacDaemonStatus()
/// in main.js.
func statusToken(_ status: SMAppService.Status) -> String {
    switch status {
    case .notRegistered:
        return "notRegistered"
    case .enabled:
        return "enabled"
    case .requiresApproval:
        return "requiresApproval"
    case .notFound:
        return "notFound"
    @unknown default:
        return "notFound"
    }
}

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(code)
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    fail("usage: mesh-helper <register|unregister|status|open-login-items>", code: 64)
}

let service = SMAppService.daemon(plistName: daemonPlistName)

switch args[1] {
case "status":
    print(statusToken(service.status))
    exit(0)

case "register":
    do {
        try service.register()
        // register() can succeed while landing in .requiresApproval — that
        // is expected, not an error. Report whatever status resulted.
        print(statusToken(service.status))
        exit(0)
    } catch {
        fail("register failed: \(error.localizedDescription)")
    }

case "unregister":
    do {
        try service.unregister()
        print(statusToken(service.status))
        exit(0)
    } catch {
        fail("unregister failed: \(error.localizedDescription)")
    }

case "open-login-items":
    SMAppService.openSystemSettingsLoginItems()
    print("opened")
    exit(0)

default:
    fail("unknown subcommand: \(args[1])", code: 64)
}

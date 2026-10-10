# mesh-install-helper.ps1 — the ONE fixed script the Potluck desktop app
# elevates (via Start-Process -Verb RunAs, a single UAC prompt) on Windows.
#
# SECURITY MODEL — READ THIS BEFORE CHANGING ANYTHING
# ------------------------------------------------------
# This is the Windows counterpart of packaging/linux/mesh-install-helper.sh
# (run via pkexec there) and the mesh-helper Swift CLI on macOS (which wraps
# SMAppService instead of an elevation prompt, since SMAppService's whole
# point is to avoid one). Electron (running as the logged-in user,
# unprivileged) elevates:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File mesh-install-helper.ps1 `
#       install -CoordinatorUrl <url> -DerpUrl <url> -MeshBin <path>
#
# via a single Start-Process -Verb RunAs (main.js's mesh:enable handler —
# apps/desktop/electron/main.js). Two things keep this from becoming an
# arbitrary-elevated-exec hole:
#
#   1. Every argument this script accepts is validated below (subcommand
#      whitelist, URL shape, mesh-binary source path) BEFORE it touches disk
#      or the Task Scheduler. main.js only ever passes FIXED production
#      constants or values it computed itself (getWindowsMeshBinPath()) —
#      never anything renderer-supplied. Nothing here is Invoke-Expression'd
#      or run through a shell that could reinterpret metacharacters.
#   2. Unlike the Linux/.deb case — where the mesh binary is placed at
#      /usr/lib/potluck/ by a ROOT postinst script before the app ever runs —
#      electron-builder's NSIS installer defaults to a PER-USER install
#      directory (%LOCALAPPDATA%\Programs\Potluck AI), which the same
#      logged-in (non-admin) user who'd click "Enable mesh" can also write
#      to. Trusting "the source path looks right" the way the Linux script
#      trusts its root-owned prefixes would NOT be a real privilege
#      boundary here. Instead, THIS elevated script COPIES the validated
#      mesh binary into an admin-owned location
#      (C:\ProgramData\Potluck\mesh\potluck-mesh.exe, ACL'd below to
#      Administrators + SYSTEM only) and points the scheduled task at THAT
#      copy — not at the original per-user resources path. A future
#      re-install (app update, or the user re-clicking Enable) re-copies;
#      nothing about the running service depends on the per-user directory
#      remaining untampered after this step completes.
#      Because the task runs that copy, an app update does not touch the file
#      the running daemon watches for replacement (cmd/potluck-mesh/
#      selfrestart.go): only this script replaces it, after stopping the
#      process. So on Windows the daemon does not restart itself after an
#      update. The app compares the daemon's exe_sha256 with the binary it
#      shipped and offers "Restart mesh service", which runs this script.
#
# WHY A SCHEDULED TASK, NOT A REAL WINDOWS SERVICE
# ---------------------------------------------------
# potluck-mesh.exe is a plain console binary — it does not implement the
# Windows Service Control (SCM) protocol (StartServiceCtrlDispatcher /
# a service control handler), so `sc.exe start` on it fails ("Cannot start
# service ...", SCM kills it after ~30s for never registering a control
# handler). Retrofitting a real SCM-compliant service wrapper is a real Go
# change (golang.org/x/sys/windows/svc) that is out of scope for this pass.
# A SYSTEM-run, "At startup" + "Run with highest privileges" Scheduled Task
# needs none of that: Task Scheduler just launches the .exe directly, exactly
# like running it from a console, so the existing binary works unmodified.
# This was verified end-to-end on a real Windows box (see the deliverable
# report): the daemon runs as NT AUTHORITY\SYSTEM and survives reboots
# (AtStartup trigger).
#
# HOW IT COMES BACK AFTER IT EXITS
# ------------------------------------
# The sidecar exits non-zero on purpose when its binary is replaced (see
# cmd/potluck-mesh/selfrestart.go), and may crash. Task Scheduler's
# "restart on failure" setting (RestartCount below) is capped at 3 restarts
# per trigger and is not documented to treat a program's non-zero exit code
# as a failure, so the task does not rely on it alone. It also carries a
# repetition trigger that fires every minute with MultipleInstances set to
# IgnoreNew: while the daemon runs the ticks do nothing, and within a minute
# of any exit the next tick starts it again. A definition change only
# reaches a machine when this script runs again (the app's "Restart mesh
# service" button), because Register-ScheduledTask is only called here.
#
# WHY THE PIPE IS STILL REACHABLE
# -----------------------------------
# The daemon runs as SYSTEM, same "needs elevation for the TUN/Wintun
# adapter" reasoning as root on macOS/Linux. Reachability for the
# interactively logged-in desktop user is fixed on the Go side:
# internal/localapi/listener_windows.go's platformListen() now grants the
# named pipe's DACL to BOTH the running (SYSTEM) account and the active
# console session's user SID (via WTSGetActiveConsoleSessionId ->
# WTSQueryUserToken), so Electron — running as that same logged-in user —
# can connect. Nothing in this script needs to know or pass along who that
# user is; the Go binary works it out at pipe-bind time, every time it
# (re)starts.
#
# USAGE
# ------
#   mesh-install-helper.ps1 install -CoordinatorUrl URL -DerpUrl URL -MeshBin PATH [-NodeName NAME]
#   mesh-install-helper.ps1 uninstall
#   mesh-install-helper.ps1 status
#
# STDOUT CONTRACT (kept in sync with main.js's runWindowsMeshHelper parsing):
#   install    -> prints exactly "installed" on success, exit 0.
#                 On failure: message to stderr, exit 1 (non-zero).
#   uninstall  -> prints exactly "uninstalled" on success, exit 0.
#   status     -> prints one of: active | inactive | not-installed
#                 (does NOT require elevation; provided for operator/CLI
#                 parity with the Linux/mac scripts — main.js's
#                 computeWindowsDaemonStatus queries the scheduled task
#                 directly instead of invoking this script, since reading
#                 task state needs no privilege).

[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('install', 'uninstall', 'status')]
    [string]$Command,

    [Parameter(Mandatory = $false)]
    [string]$CoordinatorUrl,

    [Parameter(Mandatory = $false)]
    [string]$DerpUrl,

    [Parameter(Mandatory = $false)]
    [string]$MeshBin,

    [Parameter(Mandatory = $false)]
    [string]$NodeName
)

$ErrorActionPreference = 'Stop'

$TaskName    = 'PotluckMesh'
$KeyDir      = 'C:\ProgramData\Potluck\mesh'
$InstallDir  = 'C:\ProgramData\Potluck\mesh-bin'
$InstalledBin = Join-Path $InstallDir 'potluck-mesh.exe'
$KeyPath     = Join-Path $KeyDir 'wg.key'

function Fail {
    param([string]$Message)
    Write-Error $Message
    exit 1
}

function Require-Admin {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Fail 'must run elevated (invoke via Start-Process -Verb RunAs, not directly)'
    }
}

# ----------------------------------------------------------------------------
# Argument validation — every value that ends up in the scheduled task's
# action or on disk is checked here BEFORE Do-Install runs. Fail closed.
# Mirrors packaging/linux/mesh-install-helper.sh's validate_* functions.
# ----------------------------------------------------------------------------

# Accepts http(s):// or tcp(+tls|+insecure):// URLs with a plain
# host[:port][/path] — deliberately conservative, same grammar as the Linux
# script's validate_url.
function Test-MeshUrl {
    param([string]$Value, [string]$Label)
    if ($Value -notmatch '^(https?|tcp(\+tls|\+insecure)?)://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]*)?$') {
        Fail "invalid $Label`: $Value"
    }
}

function Test-NodeName {
    param([string]$Value)
    if ($Value -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') {
        Fail "invalid node name: $Value"
    }
}

# Validates the SOURCE mesh binary main.js pointed us at (the packaged app's
# own resources\mesh\potluck-mesh.exe). Not a hard privilege boundary on its
# own (see the header comment) — the real boundary is that we COPY it to an
# admin-owned location below rather than exec it in place.
function Test-MeshBinSource {
    param([string]$Value)
    if (-not (Test-Path -LiteralPath $Value -PathType Leaf)) {
        Fail "mesh binary not found: $Value"
    }
    $resolved = (Resolve-Path -LiteralPath $Value).ProviderPath
    if ([System.IO.Path]::GetFileName($resolved) -ne 'potluck-mesh.exe') {
        Fail "mesh binary must be named potluck-mesh.exe: $resolved"
    }
    return $resolved
}

# wintun.dll is a runtime LoadLibrary dependency of the mesh binary on
# Windows (see third_party/windows/README.md in the Go module) — it must sit
# next to potluck-mesh.exe or TUN creation fails at startup. Packaged next
# to the exe by prepack:mesh (apps/desktop/electron/package.json), so it's
# always a sibling of $MeshBin in a real packaged install.
function Test-WintunSource {
    param([string]$MeshBinResolved)
    $dllPath = Join-Path (Split-Path -Parent $MeshBinResolved) 'wintun.dll'
    if (-not (Test-Path -LiteralPath $dllPath -PathType Leaf)) {
        Fail "wintun.dll not found next to mesh binary: $dllPath"
    }
    return $dllPath
}

# ----------------------------------------------------------------------------
# Install
# ----------------------------------------------------------------------------

function Do-Install {
    Require-Admin

    if (-not $CoordinatorUrl) { Fail '-CoordinatorUrl is required' }
    if (-not $DerpUrl) { Fail '-DerpUrl is required' }
    if (-not $MeshBin) { Fail '-MeshBin is required' }

    Test-MeshUrl -Value $CoordinatorUrl -Label 'coordinator URL'
    Test-MeshUrl -Value $DerpUrl -Label 'DERP URL'
    if ($NodeName) { Test-NodeName -Value $NodeName }
    $sourceBin = Test-MeshBinSource -Value $MeshBin
    $sourceWintun = Test-WintunSource -MeshBinResolved $sourceBin

    # Stop any existing task/process before overwriting the binary it's
    # running from (a locked .exe can't be overwritten on Windows).
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
    Get-Process -Name 'potluck-mesh' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

    # Key + binary directories, ACL'd to Administrators + SYSTEM only —
    # matches the Linux script's `install -d -m 0700 -o root -g root`.
    foreach ($dir in @($KeyDir, $InstallDir)) {
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $acl.SetAccessRuleProtection($true, $false) # disable inheritance, drop inherited ACEs
        foreach ($principal in @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM')) {
            $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
                $principal, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
            $acl.AddAccessRule($rule)
        }
        Set-Acl -Path $dir -AclObject $acl
    }

    # An existing task from this script's current definition has a repetition
    # trigger. If a tick started the old binary after the stop above, stop it
    # again right before the copy so the copy does not hit a locked file.
    Get-Process -Name 'potluck-mesh' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Copy-Item -LiteralPath $sourceBin -Destination $InstalledBin -Force
    Copy-Item -LiteralPath $sourceWintun -Destination (Join-Path $InstallDir 'wintun.dll') -Force

    $extraArgs = ''
    if ($NodeName) { $extraArgs = "--node-name $NodeName" }

    $exeArgs = "--coordinator-url $CoordinatorUrl --derp-url $DerpUrl --log-format json --wg-private-key-path `"$KeyPath`" $extraArgs".Trim()

    # cmd.exe /c wrapper: Register-ScheduledTaskAction's -Execute launches the
    # target directly (no shell), which is what we want for the real daemon
    # process — no wrapper needed here, unlike the debug logging setup used
    # during manual testing. Kept simple and direct.
    $action = New-ScheduledTaskAction -Execute $InstalledBin -Argument $exeArgs
    $atStartup = New-ScheduledTaskTrigger -AtStartup
    # Keeps the daemon coming back after any exit, including the deliberate
    # non-zero exit when its binary is replaced (see the header comment). With
    # MultipleInstances IgnoreNew a tick while the daemon runs does nothing.
    $keepAlive = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    # RestartCount/RestartInterval = Task Scheduler's own crash recovery, kept
    # as a second path next to the repetition trigger above.
    $settings = New-ScheduledTaskSettingsSet `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atStartup, $keepAlive) `
        -Principal $principal -Settings $settings -Force | Out-Null

    Start-ScheduledTask -TaskName $TaskName

    # Confirm it actually came up rather than optimistically declaring
    # success — same discipline as do_install() in the Linux script.
    Start-Sleep -Seconds 2
    $proc = Get-Process -Name 'potluck-mesh' -ErrorAction SilentlyContinue
    if (-not $proc) {
        Fail "$TaskName scheduled task did not start potluck-mesh.exe"
    }

    Write-Output 'installed'
}

# ----------------------------------------------------------------------------
# Uninstall
# ----------------------------------------------------------------------------

function Do-Uninstall {
    Require-Admin
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-Process -Name 'potluck-mesh' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Output 'uninstalled'
}

# ----------------------------------------------------------------------------
# Status (no elevation required; provided for operator/CLI parity)
# ----------------------------------------------------------------------------

function Do-Status {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Output 'not-installed'
        return
    }
    if ($task.State -eq 'Running') {
        Write-Output 'active'
    } else {
        Write-Output 'inactive'
    }
}

switch ($Command) {
    'install'   { Do-Install }
    'uninstall' { Do-Uninstall }
    'status'    { Do-Status }
}

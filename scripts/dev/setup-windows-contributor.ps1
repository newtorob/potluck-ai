#Requires -Version 5.1
<#
.SYNOPSIS
    One-shot Windows bootstrap for the Potluck contributor sidecar (Phase G m1).

.DESCRIPTION
    Run once per Windows machine after `git clone`. Idempotent - safe to re-run.

    Auto-detects whether to use the Mac's LAN IP or its Tailscale IP based on
    what's reachable from this box. No need to hand-edit the URL per machine.

    What it does:
      1. Checks Python 3.11 + Git are present
      2. Probes both candidate coordinator hosts (LAN first, Tailscale fallback);
         picks whichever responds and writes that into .env
      3. Runs scripts\install-contributor.py to create the venv + install deps
      4. Writes apps\desktop\python\.env with the dev smoke-test values
      5. Drops Start-PotluckContributor.cmd on the user's Desktop
         (double-click it to launch the sidecar; close the window to stop)

.NOTES
    Hardcoded for Rob's network. Edit CandidateHosts at the top if the Mac's
    LAN or Tailscale IP changes, or rotate the dev seed.
#>

param(
    [switch]$NoDesktopShortcut
)

# ---------- HARDCODED CONFIG (edit if IPs / keys change) ---------------------

# In probe order - first one to answer /transparency/fleet wins.
# Override with POTLUCK_COORDINATOR_HOST (host or host:port) when the
# coordinator moves to Fly.io or the Mac's DHCP-assigned LAN IP rotates:
#   $env:POTLUCK_COORDINATOR_HOST = "potluck-coordinator.fly.dev"
#   .\setup-windows-contributor.ps1
$CandidateHosts = if ($env:POTLUCK_COORDINATOR_HOST) {
    @($env:POTLUCK_COORDINATOR_HOST)
} else {
    @(
        "192.168.4.74",     # dev Mac LAN IP (DHCP — set a static lease or use the env override above)
        "100.101.102.103"      # dev Mac Tailscale IP (inbound to the Mac is currently broken; Fly.io is the fix)
    )
}
$CoordinatorPort = "8400"
$BinaryHash      = "59b74f2ea0e8b6f23f3672336f90c97435b1dfa13ef7aabc1994b61c54c7d65a"
$MemberId        = "00000000-0000-0000-0000-000000000001"

# ---------- helpers ----------------------------------------------------------

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Pass($msg) { Write-Host "    [ok]   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    [warn] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "    [fail] $msg" -ForegroundColor Red; exit 1 }

$RepoRoot   = Split-Path -Parent $PSScriptRoot
$SidecarDir = Join-Path $RepoRoot "apps\desktop\python"
$VenvPython = Join-Path $SidecarDir ".venv\Scripts\python.exe"
$EnvFile    = Join-Path $SidecarDir ".env"

# ---------- 1. Python 3.11 ---------------------------------------------------

Step "Checking Python (3.11+)"
# Accept any Python 3.11 or newer. Windows commonly ships 3.12/3.13; the
# contributor module has no native-extension deps that pin to 3.11 (inference
# is delegated to Ollama), so 3.12+ is fine. Prefer the newest match found.
$pythonExe = $null
foreach ($candidate in @("py -3.11", "py -3.12", "py -3.13", "python3.11", "python")) {
    try {
        $parts = $candidate.Split(" ")
        $cmd = $parts[0]
        $args_ = $parts[1..($parts.Length - 1)]
        $ver = & $cmd @args_ -c "import sys; print('.'.join(map(str, sys.version_info[:2])))" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) {
            $verParts = $ver.Split(".")
            $major = [int]$verParts[0]
            $minor = [int]$verParts[1]
            if ($major -eq 3 -and $minor -ge 11) {
                $pythonExe = $candidate
                Pass "found Python $ver via '$pythonExe'"
                break
            }
        }
    } catch { }
}
if (-not $pythonExe) {
    Fail "Python 3.11+ not found. Install via 'winget install Python.Python.3.11' (then open a new shell), or grab the python.org installer."
}

# ---------- 2. Git -----------------------------------------------------------

Step "Checking git"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "git not on PATH. 'winget install Git.Git', open a new shell, re-run."
}
Pass "git is present"

# ---------- 3. Coordinator probe ---------------------------------------------

Step "Probing coordinator endpoints"
$CoordinatorHost = $null
foreach ($host_ in $CandidateHosts) {
    $url = "http://${host_}:${CoordinatorPort}/transparency/fleet"
    try {
        # 10s timeout: PS 5.1's first Invoke-WebRequest in a fresh process can
        # spend 5+ seconds on .NET init + system-proxy auto-detection before
        # the actual network call begins. 3s isn't enough; 10s leaves headroom.
        $resp = Invoke-WebRequest -Uri $url -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -eq 200) {
            $CoordinatorHost = $host_
            Pass "reachable on $host_"
            break
        } else {
            Warn "$host_ returned HTTP $($resp.StatusCode)"
        }
    } catch {
        # Surface the exception message so future failures aren't opaque.
        Warn "no answer on ${host_}: $($_.Exception.Message)"
    }
}

if (-not $CoordinatorHost) {
    $tailscaleHint = ""
    if (-not (Get-Command tailscale -ErrorAction SilentlyContinue)) {
        $tailscaleHint = "`n  - Tailscale isn't installed on this box. If the dev Mac isn't on your LAN,`n    install Tailscale (https://tailscale.com/download/windows) and sign in to`n    the same account as the Mac, then re-run this script."
    }
    Fail @"
Coordinator unreachable on any candidate host.

Tried: $($CandidateHosts -join ', ')

Common causes:
  - Coordinator container stopped on the dev Mac: check 'docker ps' there.
  - Dev Mac IPs changed. On the dev Mac:
      LAN:       'ipconfig getifaddr en0'
      Tailscale: 'tailscale ip -4'
    Edit `$CandidateHosts at the top of this script with the new value(s).
  - Windows Defender Firewall blocking outbound 8400 (rare; usually outbound
    is open by default).$tailscaleHint
"@
}

$CoordinatorUrl = "ws://${CoordinatorHost}:${CoordinatorPort}/peers/connect"

# ---------- 4. Install via cross-platform installer --------------------------

Step "Running scripts\install-contributor.py (creates venv + installs deps)"
$installer = Join-Path $RepoRoot "scripts\install-contributor.py"
if (-not (Test-Path $installer)) {
    Fail "missing $installer. Did the git pull succeed?"
}
$parts = $pythonExe.Split(" ")
$pyCmd = $parts[0]
$pyArgs = $parts[1..($parts.Length - 1)] + @($installer)
& $pyCmd @pyArgs
if ($LASTEXITCODE -ne 0) {
    Fail "install-contributor.py failed. Scroll up for the error."
}
if (-not (Test-Path $VenvPython)) {
    Fail "venv python missing at $VenvPython after install. Something went sideways."
}
Pass "venv ready at $VenvPython"

# ---------- 5. Write .env ----------------------------------------------------

Step "Writing $EnvFile"

# Merge the basic 3 vars into any existing .env, preserving whatever
# inference keys (or other custom keys) the user has already added via
# scripts\setup-windows-inference.ps1 or by hand. Earlier versions of
# this script overwrote .env wholesale, which clobbered the inference
# config on re-runs. Don't do that.
$basicKeys = @(
    "POTLUCK_COORDINATOR_URL",
    "POTLUCK_BINARY_HASH",
    "POTLUCK_MEMBER_ID"
)
$existing = @()
if (Test-Path $EnvFile) {
    $existing = Get-Content $EnvFile | Where-Object {
        $line = $_
        # Drop only the basic keys + the auto-generated header comment;
        # keep everything else (inference config, user-added vars, etc.).
        if ($line -match "^# Generated by scripts\\setup-windows-contributor\.ps1") { return $false }
        foreach ($k in $basicKeys) {
            if ($line -match "^$k=") { return $false }
        }
        return $true
    }
}

$updated = @(
    "# Generated by scripts\setup-windows-contributor.ps1 - do not commit.",
    "POTLUCK_COORDINATOR_URL=$CoordinatorUrl",
    "POTLUCK_BINARY_HASH=$BinaryHash",
    "POTLUCK_MEMBER_ID=$MemberId"
) + $existing

# UTF-8 without BOM so pydantic-settings reads it cleanly
[System.IO.File]::WriteAllLines($EnvFile, $updated, [System.Text.UTF8Encoding]::new($false))
$preservedCount = ($existing | Where-Object { $_ -match "^POTLUCK_" }).Count
if ($preservedCount -gt 0) {
    Pass "wrote .env (host=$CoordinatorHost; preserved $preservedCount existing inference/custom var(s))"
} else {
    Pass "wrote .env (host=$CoordinatorHost)"
}

# ---------- 6. Smoke-launch + verify -----------------------------------------

Step "Smoke-launching sidecar (15s, then auto-kill)"
$smokeJob = Start-Job -ScriptBlock {
    param($py, $cwd)
    Set-Location $cwd
    & $py -m potluck_local.contributor --verbose 2>&1
} -ArgumentList $VenvPython, $SidecarDir

Start-Sleep -Seconds 15
Stop-Job -Job $smokeJob -ErrorAction SilentlyContinue
$smokeOutput = Receive-Job -Job $smokeJob -Keep
Remove-Job -Job $smokeJob -Force -ErrorAction SilentlyContinue

if ($smokeOutput -match "session_opened") {
    Pass "session_opened observed - handshake worked"
} elseif ($smokeOutput -match "binary_hash_mismatch|not in contributor_releases") {
    Fail "coordinator rejected binary_hash. Either the .env didn't load or the seed isn't in contributor_releases on the Mac."
} elseif ($smokeOutput -match "missing POTLUCK_BINARY_HASH") {
    Fail ".env didn't load. pydantic-settings should pick up apps\desktop\python\.env automatically - check the file exists with the 3 vars."
} else {
    Warn "didn't see session_opened in 15s. Last lines of output:"
    $smokeOutput | Select-Object -Last 10 | ForEach-Object { Write-Host "    $_" }
    Warn "If those lines look healthy (e.g. 'connecting' but no error), the handshake just hadn't completed in the timeout - try the launcher and watch live."
}

# ---------- 7. Desktop double-click launcher ---------------------------------

if (-not $NoDesktopShortcut) {
    Step "Creating Start-PotluckContributor.cmd on Desktop"
    $desktop = [Environment]::GetFolderPath("Desktop")
    $launcher = Join-Path $desktop "Start-PotluckContributor.cmd"

    # Generate the launcher with absolute paths baked in. The original
    # scripts\start-windows-contributor.cmd uses %~dp0 to find the repo
    # which only works when the .cmd lives in scripts/. Copying it to
    # the Desktop breaks that resolution (%~dp0 becomes the Desktop path).
    # Generating per-machine fixes that.
    $launcherContent = @"
@echo off
REM Generated by setup-windows-contributor.ps1 on $(Get-Date -Format "yyyy-MM-dd HH:mm:ss").
REM Repo paths are baked in below. Re-run setup-windows-contributor.ps1
REM if you move the repo to a different location.

setlocal
set "SIDECAR_DIR=$SidecarDir"
set "VENV_PY=$VenvPython"

if not exist "%VENV_PY%" (
    echo.
    echo ERROR: venv python not found at %VENV_PY%
    echo Re-run scripts\setup-windows-contributor.ps1 to recreate the venv.
    echo.
    pause
    exit /b 1
)

if not exist "%SIDECAR_DIR%\.env" (
    echo.
    echo ERROR: %SIDECAR_DIR%\.env is missing.
    echo Re-run scripts\setup-windows-contributor.ps1 to write it.
    echo.
    pause
    exit /b 1
)

cd /d "%SIDECAR_DIR%"
echo Starting Potluck contributor (Ctrl-C or close window to stop)...
echo.

"%VENV_PY%" -m potluck_local.contributor --verbose

echo.
echo Sidecar exited. Press any key to close this window.
pause >nul
"@
    [System.IO.File]::WriteAllText(
        $launcher, $launcherContent, [System.Text.UTF8Encoding]::new($false)
    )
    Pass "wrote $launcher (double-click to launch)"
}

# ---------- 8. Done ----------------------------------------------------------

Write-Host ""
Step "Setup complete"
Write-Host @"

To start the sidecar manually:
    cd $SidecarDir
    .\.venv\Scripts\python.exe -m potluck_local.contributor --verbose

Or double-click 'Start-PotluckContributor' on your Desktop.

To stop: close the launcher window, or Ctrl-C in the terminal.

Per-peer Ed25519 key lives at:
    %LOCALAPPDATA%\Potluck\contributor\peer-key.pem

"@

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
$CandidateHosts = @(
    "192.168.4.76",     # dev Mac LAN IP
    "100.101.102.103"      # dev Mac Tailscale IP
)
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

Step "Checking Python 3.11"
$pythonExe = $null
foreach ($candidate in @("py -3.11", "python3.11", "python")) {
    try {
        $parts = $candidate.Split(" ")
        $cmd = $parts[0]
        $args_ = $parts[1..($parts.Length - 1)]
        $ver = & $cmd @args_ -c "import sys; print('.'.join(map(str, sys.version_info[:2])))" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver -eq "3.11") {
            $pythonExe = $candidate
            break
        }
    } catch { }
}
if (-not $pythonExe) {
    Fail "Python 3.11 not found. Install via 'winget install Python.Python.3.11' (then open a new shell), or grab the python.org installer."
}
Pass "found Python 3.11 via '$pythonExe'"

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
        $resp = Invoke-WebRequest -Uri $url -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -eq 200) {
            $CoordinatorHost = $host_
            Pass "reachable on $host_"
            break
        } else {
            Warn "$host_ returned HTTP $($resp.StatusCode)"
        }
    } catch {
        Warn "no answer on $host_"
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
$envContent = @"
# Generated by scripts\setup-windows-contributor.ps1 - do not commit.
POTLUCK_COORDINATOR_URL=$CoordinatorUrl
POTLUCK_BINARY_HASH=$BinaryHash
POTLUCK_MEMBER_ID=$MemberId
"@
# UTF-8 without BOM so pydantic-settings reads it cleanly
[System.IO.File]::WriteAllText($EnvFile, $envContent, [System.Text.UTF8Encoding]::new($false))
Pass "wrote .env (host=$CoordinatorHost)"

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
    $launcherSrc = Join-Path $RepoRoot "scripts\start-windows-contributor.cmd"
    if (Test-Path $launcherSrc) {
        Copy-Item $launcherSrc $launcher -Force
        Pass "wrote $launcher (double-click to launch)"
    } else {
        Warn "scripts\start-windows-contributor.cmd not found - skipping desktop shortcut"
    }
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

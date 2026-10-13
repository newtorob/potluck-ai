#Requires -Version 5.1
<#
.SYNOPSIS
    Potluck household contributor setup for Windows (desktop).

.DESCRIPTION
    Sister script to scripts/setup-household.sh. Installs everything a
    Windows machine needs to join a Potluck household + serve inference,
    and builds the Electron desktop app so the user gets a normal
    GUI experience (not a CLI tool).

    What it does:
      1. Installs prerequisites via winget: Git, Python 3.11+, Go 1.22+,
         Node.js, pnpm, WireGuard tools (for wintun driver).
      2. Builds the potluck-mesh Go sidecar.
      3. Sets up the Python sidecar venv + installs deps.
      4. Builds the Electron desktop app (pnpm install + build).
      5. Drops a desktop shortcut "Potluck.lnk" that launches the
         full stack (mesh sidecar + Python sidecar + Electron app)
         with a single double-click.

    Designed for a Windows desktop that is also used for gaming:
      - Interactive, opt-in launcher (not a Windows Service). The user
        starts Potluck when they step away from gaming; closes it when
        they want to game.
      - No code-signing cert required because we build the Electron app
        locally on the user's machine - Windows trusts locally-compiled
        binaries fully. Skips the SmartScreen warning entirely.

    Modes:
        .\setup-household.ps1                # full setup + create shortcut
        .\setup-household.ps1 -SetupOnly     # install + build, no shortcut
        .\setup-household.ps1 -RunNow        # full setup + immediately launch
        .\setup-household.ps1 -Stop          # stop running processes
        .\setup-household.ps1 -Status        # show what's running

    Re-runnable - every step skips work that's already done.

.NOTES
    Windows scripting notes:
      - ASCII-only file (no em-dashes, smart quotes, etc.)
      - PATH only refreshes in new shells; we probe install dirs after winget
      - Desktop shortcut paths are baked in absolute (not %~dp0-relative)
      - .env merging not overwriting
      - Surface exception messages, never silent failures
#>

[CmdletBinding()]
param(
    [switch]$SetupOnly,
    [switch]$RunNow,
    [switch]$Stop,
    [switch]$Status,
    [switch]$Help
)

if ($Help) {
    Get-Help $MyInvocation.MyCommand.Path -Full
    exit 0
}

$ErrorActionPreference = "Stop"

# ----------------------------------------------------------------------------
# Constants + paths
# ----------------------------------------------------------------------------

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot   = Split-Path -Parent $ScriptDir
$MeshDir    = Join-Path $RepoRoot "apps\desktop\go\potluck-mesh"
$MeshBin    = Join-Path $MeshDir "dist\potluck-mesh.exe"
$PyDir      = Join-Path $RepoRoot "apps\desktop\python"
$VenvDir    = Join-Path $PyDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip    = Join-Path $VenvDir "Scripts\pip.exe"
$FrontendDir = Join-Path $RepoRoot "apps\desktop\frontend"
$ElectronDir = Join-Path $RepoRoot "apps\desktop\electron"

$StateDir = Join-Path $env:LOCALAPPDATA "Potluck"
$LogDir   = Join-Path $StateDir "logs"
$PidDir   = Join-Path $StateDir "run"

$MeshPidFile = Join-Path $PidDir "mesh.pid"
$PyPidFile   = Join-Path $PidDir "python.pid"
$AppPidFile  = Join-Path $PidDir "app.pid"
$MeshLog     = Join-Path $LogDir "mesh.log"
$PyLog       = Join-Path $LogDir "python.log"
$AppLog      = Join-Path $LogDir "app.log"

$PythonPort = if ($env:POTLUCK_PYTHON_PORT) { [int]$env:POTLUCK_PYTHON_PORT } else { 8321 }
$MeshPipe   = "\\.\pipe\potluck-mesh"

New-Item -ItemType Directory -Path $LogDir, $PidDir -Force | Out-Null

# ----------------------------------------------------------------------------
# Output helpers (ASCII only, no em-dashes, no smart quotes)
# ----------------------------------------------------------------------------

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Pass($msg) { Write-Host "    [ok]   $msg" -ForegroundColor Green }
function Info($msg) { Write-Host "    $msg" -ForegroundColor DarkGray }
function Warn($msg) { Write-Host "    [warn] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "    [fail] $msg" -ForegroundColor Red; exit 1 }

function Ask($prompt, $default = "") {
    if ($default) {
        $line = Read-Host "    $prompt [$default]"
        if ([string]::IsNullOrWhiteSpace($line)) { return $default }
        return $line
    }
    return Read-Host "    $prompt"
}

# Invoke-Native: run an external command tolerantly.
#
# PowerShell with $ErrorActionPreference = 'Stop' treats any stderr output
# from a native command (npm notice lines, 'go: downloading <dep>' lines,
# pip warnings, etc.) as a fatal NativeCommandError. This wrapper relaxes
# the policy for the duration of the call and reports the real exit code
# back. Callers should check the return value (or Test-Path on a known
# build artifact) rather than relying on PowerShell exception flow.
function Invoke-Native {
    param(
        [Parameter(Mandatory)][scriptblock]$Block,
        [string]$Label = "command"
    )
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Block
        return $LASTEXITCODE
    } catch {
        # Genuine exception (not just stderr-as-error). Surface it.
        Write-Host "    [warn] ${Label}: $($_.Exception.Message)" -ForegroundColor Yellow
        return 1
    } finally {
        $ErrorActionPreference = $prevEAP
    }
}

# ----------------------------------------------------------------------------
# Process lifecycle helpers
# ----------------------------------------------------------------------------

function Is-Running($pidFile) {
    if (-not (Test-Path $pidFile)) { return $false }
    $processId = Get-Content $pidFile -ErrorAction SilentlyContinue
    if (-not $processId) { return $false }
    try {
        $proc = Get-Process -Id $processId -ErrorAction Stop
        return ($null -ne $proc)
    } catch { return $false }
}

function Stop-IfRunning($pidFile, $label) {
    if (Is-Running $pidFile) {
        $processId = Get-Content $pidFile
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
            Pass "$label stopped (was pid $processId)"
        } catch {
            Warn "${label}: could not stop pid ${processId}: $($_.Exception.Message)"
        }
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    } else {
        Info "$label wasn't running"
    }
}

# ----------------------------------------------------------------------------
# Stop / Status modes (handled before any prereq checks)
# ----------------------------------------------------------------------------

if ($Stop) {
    Step "Stopping Potluck processes"
    Stop-IfRunning $AppPidFile  "Electron app"
    Stop-IfRunning $PyPidFile   "Python sidecar"
    Stop-IfRunning $MeshPidFile "Mesh sidecar"
    exit 0
}

if ($Status) {
    Step "Potluck process status"
    if (Is-Running $MeshPidFile) {
        Pass "Mesh sidecar UP (pid $(Get-Content $MeshPidFile))"
    } else { Info "Mesh sidecar DOWN" }
    if (Is-Running $PyPidFile) {
        Pass "Python sidecar UP (pid $(Get-Content $PyPidFile))"
        try {
            $h = Invoke-RestMethod "http://127.0.0.1:${PythonPort}/health" -TimeoutSec 2 -ErrorAction Stop
            if ($h.active_model_id) { Info "Loaded model: $($h.active_model_id)" }
        } catch {}
    } else { Info "Python sidecar DOWN" }
    if (Is-Running $AppPidFile) {
        Pass "Electron app UP (pid $(Get-Content $AppPidFile))"
    } else { Info "Electron app DOWN" }
    exit 0
}

# ----------------------------------------------------------------------------
# Prerequisite install via winget
# ----------------------------------------------------------------------------

function Test-Winget {
    return $null -ne (Get-Command winget -ErrorAction SilentlyContinue)
}

function Install-IfMissing($commandName, $wingetId, $extraProbePaths = @()) {
    # Returns the full path to the executable if found OR installed; throws on failure.
    $exe = Get-Command $commandName -ErrorAction SilentlyContinue
    if ($exe) { return $exe.Source }

    # Probe well-known install locations (PATH may not have refreshed yet
    # in this shell).
    foreach ($p in $extraProbePaths) {
        if (Test-Path $p) {
            $env:PATH = (Split-Path -Parent $p) + ";" + $env:PATH
            return $p
        }
    }

    if (-not (Test-Winget)) {
        Fail "winget not available. Install 'App Installer' from the Microsoft Store and re-run, or install $commandName manually."
    }

    Info "installing $wingetId via winget (this may take a minute)..."
    & winget install --silent --accept-source-agreements --accept-package-agreements --id $wingetId | Out-Null
    # winget exits non-zero in some 'already installed' cases - we re-probe
    # rather than trust the exit code.

    $exe = Get-Command $commandName -ErrorAction SilentlyContinue
    if ($exe) {
        Pass "$commandName installed"
        return $exe.Source
    }
    foreach ($p in $extraProbePaths) {
        if (Test-Path $p) {
            $env:PATH = (Split-Path -Parent $p) + ";" + $env:PATH
            Pass "$commandName installed at $p"
            return $p
        }
    }
    Fail "$commandName still not found after winget install. Open a NEW PowerShell window and re-run -SetupOnly to refresh PATH."
}

function Install-Prerequisites {
    Step "Installing prerequisites via winget"

    if (-not (Test-Winget)) {
        Fail "winget is not available. winget ships with Windows 10 1809+ and Windows 11. Install 'App Installer' from the Microsoft Store, then re-run."
    }
    Info "winget is available"

    # Git
    Install-IfMissing "git" "Git.Git" @(
        "$env:ProgramFiles\Git\cmd\git.exe"
    ) | Out-Null

    # Python 3.11+
    $pythonProbe = @(
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:ProgramFiles\Python311\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles\Python313\python.exe"
    )
    Install-IfMissing "python" "Python.Python.3.11" $pythonProbe | Out-Null

    # Go 1.22+
    Install-IfMissing "go" "GoLang.Go" @(
        "$env:ProgramFiles\Go\bin\go.exe"
    ) | Out-Null

    # Node.js (for pnpm + Electron build)
    Install-IfMissing "node" "OpenJS.NodeJS.LTS" @(
        "$env:ProgramFiles\nodejs\node.exe"
    ) | Out-Null

    # pnpm - installed via npm rather than winget so we get the version
    # that matches the lockfiles in the repo.
    $pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
    if (-not $pnpm) {
        Info "installing pnpm globally via npm..."
        # npm emits 'notice' lines to stderr; with $ErrorActionPreference =
        # 'Stop' (set at the top of this script) PowerShell treats those as
        # fatal NativeCommandErrors. We relax the policy just for this call
        # and verify success via Get-Command afterward (npm's exit code
        # may also be non-zero in some 'already up to date' cases).
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & npm install --global pnpm 2>&1 | Out-Null
        } catch {
            # Swallow - we verify with Get-Command below, which is the
            # authoritative check anyway.
        }
        $ErrorActionPreference = $prevEAP

        $pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
        if (-not $pnpm) {
            $probe = "$env:APPDATA\npm\pnpm.cmd"
            if (Test-Path $probe) {
                $env:PATH = "$env:APPDATA\npm;" + $env:PATH
                $pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
            }
        }
        if (-not $pnpm) {
            Fail "pnpm not found after npm install. Open a NEW PowerShell window and re-run -SetupOnly to refresh PATH, or manually 'npm install --global pnpm'."
        }
        Pass "pnpm installed"
    } else {
        Pass "pnpm is present"
    }

    # WireGuard (for wintun driver - required by the userspace WireGuard
    # implementation in potluck-mesh). One-time admin install.
    $wgInstalled = $false
    if (Get-Command wg -ErrorAction SilentlyContinue) {
        $wgInstalled = $true
    } elseif (Test-Path "$env:ProgramFiles\WireGuard\wg.exe") {
        $wgInstalled = $true
    }
    if (-not $wgInstalled) {
        Info "installing WireGuard (includes wintun driver)..."
        & winget install --silent --accept-source-agreements --accept-package-agreements --id WireGuard.WireGuard | Out-Null
        Pass "WireGuard installed"
    } else {
        Pass "WireGuard is present (includes wintun driver)"
    }
}

# ----------------------------------------------------------------------------
# Build steps
# ----------------------------------------------------------------------------

function Build-MeshSidecar {
    Step "Building potluck-mesh"
    Push-Location $MeshDir
    try {
        if (-not (Test-Path "dist")) { New-Item -ItemType Directory -Path "dist" | Out-Null }
        # Go prints 'go: downloading <dep>' to stderr while fetching modules.
        # PowerShell's strict mode treats that as a fatal error; Invoke-Native
        # relaxes the policy so the build can complete normally.
        $exitCode = Invoke-Native -Label "go build" -Block {
            & go build -o "dist\potluck-mesh.exe" ".\cmd\potluck-mesh"
        }
        if (-not (Test-Path $MeshBin)) {
            Fail "mesh binary missing at $MeshBin after build (go exit $exitCode). Check the output above for compile errors."
        }
        Pass "built $MeshBin"
    } finally {
        Pop-Location
    }
}

# Test whether the venv can import llama_cpp.
function Test-LlamaCppInstalled {
    if (-not (Test-Path $VenvPython)) { return $false }
    $check = & $VenvPython -c "import llama_cpp; print('OK')" 2>$null
    return ($check -eq "OK")
}

# Install llama-cpp-python via a fallback chain. Critical because the
# default pip path on Windows hits a C++ source build that requires MSVC,
# which most users don't have. We try three increasingly heavyweight
# routes:
#   1. PyPI with --prefer-binary  (fast if there's a wheel for this Python)
#   2. abetlen's custom wheel index  (covers more Python-version combos)
#   3. Source build  (only works if MSVC Build Tools are installed)
function Install-LlamaCppPython {
    Step "Installing llama-cpp-python (has a fallback chain - this is the historically-painful one)"
    if (Test-LlamaCppInstalled) {
        Pass "llama_cpp already importable; skipping reinstall"
        return
    }

    # Attempt 1: PyPI with --prefer-binary
    Info "attempt 1/3: PyPI pre-built wheel (--prefer-binary)"
    Invoke-Native -Label "pip install llama-cpp-python (PyPI)" -Block {
        & $VenvPip install --prefer-binary llama-cpp-python
    } | Out-Null
    if (Test-LlamaCppInstalled) {
        Pass "llama-cpp-python installed via PyPI"
        return
    }
    Warn "PyPI install did not produce a working llama_cpp"

    # Attempt 2: abetlen's custom wheel index (CPU-only wheels)
    Info "attempt 2/3: abetlen.github.io custom wheel index"
    Invoke-Native -Label "pip install llama-cpp-python (abetlen)" -Block {
        & $VenvPip install --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu llama-cpp-python
    } | Out-Null
    if (Test-LlamaCppInstalled) {
        Pass "llama-cpp-python installed via abetlen wheel index"
        return
    }
    Warn "abetlen wheel index install did not produce a working llama_cpp"

    # Attempt 3: source build (needs MSVC Build Tools)
    Info "attempt 3/3: source build (requires Visual Studio Build Tools)"
    Invoke-Native -Label "pip install llama-cpp-python (source)" -Block {
        & $VenvPip install llama-cpp-python
    } | Out-Null
    if (Test-LlamaCppInstalled) {
        Pass "llama-cpp-python built from source"
        return
    }

    # All three failed. Surface the actionable next step explicitly.
    Fail @"
llama-cpp-python could not be installed via any of three paths:
  1. PyPI pre-built wheel (--prefer-binary)
  2. abetlen.github.io custom wheel index
  3. Source build (needs MSVC)

The most reliable fix is installing Microsoft Visual Studio Build Tools (~4 GB, ~15 min):
  winget install --silent --override "--passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended" Microsoft.VisualStudio.2022.BuildTools

After it installs, close PowerShell, open a fresh admin window, cd back to the repo, and re-run this script.

Alternatively, install a Python version that has a published wheel. PyPI currently ships wheels for cp310 / cp311 / cp312 amd64. If your Python is 3.13 (which is newer than the latest llama-cpp-python wheel), downgrade:
  winget install Python.Python.3.11
  Remove-Item -Recurse -Force apps\desktop\python\.venv
  py -3.11 -m venv apps\desktop\python\.venv
  # Re-run this script.
"@
}

function Setup-PythonVenv {
    Step "Setting up Python venv at $VenvDir"
    if (-not (Test-Path $VenvDir)) {
        Invoke-Native -Label "python -m venv" -Block { & python -m venv $VenvDir } | Out-Null
        if (-not (Test-Path $VenvPython)) {
            Fail "venv creation failed. Make sure Python is on PATH (open a new shell + re-run)."
        }
        Pass "venv created"
    } else {
        Pass "venv already exists"
    }
    Info "upgrading pip..."
    Invoke-Native -Label "pip upgrade" -Block {
        & $VenvPip install --upgrade pip
    } | Out-Null

    # Install llama-cpp-python via the smart fallback chain before the
    # rest of requirements.txt. If it succeeds, the subsequent
    # `-r requirements.txt` install sees llama_cpp is already satisfied
    # and skips it. If it fails, we Fail() with actionable advice.
    Install-LlamaCppPython

    Info "installing remaining requirements.txt (visible progress - no --quiet)..."
    # No --quiet: silent installs make hangs indistinguishable from
    # progress. The output is verbose but at least you can tell if pip is
    # doing something or stuck.
    $rc = Invoke-Native -Label "pip install -r requirements.txt" -Block {
        & $VenvPip install --prefer-binary -r (Join-Path $PyDir "requirements.txt")
    }
    if ($rc -ne 0) {
        Fail "pip install -r requirements.txt failed (exit $rc). Scroll up for the actual error."
    }
    Info "installing potluck_local in editable mode..."
    Invoke-Native -Label "pip install -e" -Block {
        & $VenvPip install -e $PyDir
    } | Out-Null
    Pass "Python sidecar deps installed"
}

function Build-ElectronApp {
    Step "Building Electron desktop app (this can take 5-10 minutes the first time)"

    # Frontend deps + production build. No --silent on pnpm - silent makes
    # hangs look like progress. The output is noisy but legible.
    Info "installing frontend deps (visible progress)..."
    Push-Location $FrontendDir
    try {
        $rc = Invoke-Native -Label "pnpm install (frontend)" -Block {
            & pnpm install
        }
        if ($rc -ne 0) { Fail "pnpm install in frontend failed (exit $rc). Scroll up for the actual error." }
        Info "building frontend bundle (tsc -b + vite build)..."
        $rc = Invoke-Native -Label "pnpm build (frontend)" -Block { & pnpm build }
        if ($rc -ne 0) {
            Fail @"
pnpm build in frontend failed (exit $rc).

Common causes + fixes:
  - TypeScript error: scroll up to find a 'src/...:NNN:NN - error TSXXXX' line.
    Tell me the file + line and I will fix it.
  - Out of memory: bump Node's heap. Try:
      \$env:NODE_OPTIONS = '--max-old-space-size=8192'
    then re-run -SetupOnly.
  - Stale build artifacts: try
      Remove-Item -Recurse -Force apps\desktop\frontend\dist
      Remove-Item -Recurse -Force apps\desktop\frontend\node_modules\.vite
    then re-run -SetupOnly.

Output was visible in this terminal (no --silent flag) so the actual error should be in your scrollback.
"@
        }
        Pass "frontend built"
    } finally {
        Pop-Location
    }

    # Electron deps (the install step downloads Electron itself - large)
    Info "installing Electron deps (downloads Electron runtime ~150MB)..."
    Push-Location $ElectronDir
    try {
        $rc = Invoke-Native -Label "pnpm install (electron)" -Block {
            & pnpm install
        }
        if ($rc -ne 0) { Fail "pnpm install in electron failed (exit $rc). Scroll up." }
        Pass "Electron deps installed"
    } finally {
        Pop-Location
    }
}

# ----------------------------------------------------------------------------
# Desktop shortcut creation
# ----------------------------------------------------------------------------

function Create-DesktopShortcut {
    Step "Creating Desktop shortcut 'Potluck.lnk'"

    # The launcher script lives next to this setup script. Absolute paths
    # baked in so the shortcut survives the user copying it elsewhere.
    $launcherScript = Join-Path $ScriptDir "start-household-windows.ps1"
    if (-not (Test-Path $launcherScript)) {
        # Write the launcher script if it doesn't exist yet. Keeping it
        # as a separate file (rather than inlining into the .lnk Target)
        # lets users update Potluck without re-running setup - just git
        # pull and the next double-click picks up the new launcher.
        $launcherContent = @"
# Potluck launcher - double-click target.
# Auto-generated by setup-household.ps1. Safe to re-generate.
`$ErrorActionPreference = "Continue"
`$setup = "$($MyInvocation.MyCommand.Path)"
& powershell.exe -ExecutionPolicy Bypass -NoLogo -File `$setup -RunNow
"@
        Set-Content -LiteralPath $launcherScript -Value $launcherContent -Encoding ASCII
    }

    $desktopPath = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktopPath "Potluck.lnk"

    $wshell = New-Object -ComObject WScript.Shell
    $shortcut = $wshell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = "powershell.exe"
    $shortcut.Arguments = "-ExecutionPolicy Bypass -NoLogo -File `"$launcherScript`""
    $shortcut.WorkingDirectory = $RepoRoot
    $shortcut.WindowStyle = 1  # normal window (1 = normal, 7 = minimized)
    $shortcut.Description = "Launch Potluck household contributor"
    # Use the Electron app icon if available; otherwise default PowerShell icon.
    $iconCandidates = @(
        (Join-Path $ElectronDir "icons\icon.ico"),
        (Join-Path $RepoRoot "apps\desktop\electron\icons\icon.ico")
    )
    foreach ($i in $iconCandidates) {
        if (Test-Path $i) {
            $shortcut.IconLocation = $i
            break
        }
    }
    $shortcut.Save()
    Pass "shortcut created at $shortcutPath"
    Info "double-click the desktop shortcut to launch Potluck"
}

# ----------------------------------------------------------------------------
# Launch / run mode
# ----------------------------------------------------------------------------

function Start-MeshSidecar {
    Step "Starting potluck-mesh sidecar"
    if (Is-Running $MeshPidFile) {
        Pass "mesh sidecar already running (pid $(Get-Content $MeshPidFile))"
        return
    }

    # Coordinator URL prompt (or use stored value from a prior run)
    $coordEnvFile = Join-Path $StateDir "coordinator.url"
    $coordUrl = $env:POTLUCK_COORDINATOR_URL
    if (-not $coordUrl -and (Test-Path $coordEnvFile)) {
        $coordUrl = (Get-Content $coordEnvFile -Raw).Trim()
    }
    if (-not $coordUrl) {
        $coordUrl = Ask "Laptop coordinator URL" "http://192.168.1.10:8400"
        Set-Content -LiteralPath $coordEnvFile -Value $coordUrl -Encoding ASCII
    }

    # Probe reachability so failures are immediate + actionable.
    Info "verifying coordinator at $coordUrl..."
    try {
        $resp = Invoke-WebRequest -Uri "$coordUrl/health" -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -ne 200) {
            Fail "coordinator returned HTTP $($resp.StatusCode). Make sure 'make up' is running on the laptop."
        }
    } catch {
        Warn "coordinator unreachable: $($_.Exception.Message)"
        Warn "  - On the laptop, run 'make up' to start the coordinator."
        Warn "  - Confirm the laptop's LAN IP with 'ipconfig getifaddr en0' (Mac) or 'ip -4 addr show' (Linux)."
        Warn "  - Windows Defender Firewall may be blocking outbound 8400 (rare)."
        Fail "fix coordinator reachability and re-run"
    }
    Pass "coordinator reachable"

    # DERP relay URL - the fallback when direct UDP punch fails (hairpin
    # NAT, symmetric NAT, restrictive firewalls). Defaults to the production
    # relay (services/derp, tcp+tls per internal/derp/client.go); it is not
    # derived from the coordinator URL, since a self-hosted/LAN coordinator
    # does not imply a self-hosted relay. Override with $env:POTLUCK_DERP_URL,
    # e.g. tcp+insecure://<lan-host>:8443 for LAN-only dev against a local
    # services/derp run without TLS.
    $derpUrl = $env:POTLUCK_DERP_URL
    if (-not $derpUrl) {
        $derpUrl = "tcp+tls://derp.trypotluck.ai:443"
    }
    Info "derp relay URL: $derpUrl"

    # The mesh sidecar needs admin to open the wintun adapter (one-time per
    # boot). We use Start-Process -Verb RunAs to trigger a UAC prompt.
    Info "starting mesh sidecar (UAC prompt may appear for wintun access)..."
    $proc = Start-Process -FilePath $MeshBin `
        -ArgumentList "--coordinator-url", $coordUrl, "--derp-url", $derpUrl `
        -RedirectStandardOutput $MeshLog `
        -RedirectStandardError "$MeshLog.err" `
        -WindowStyle Hidden `
        -PassThru `
        -Verb RunAs
    Set-Content -LiteralPath $MeshPidFile -Value $proc.Id -Encoding ASCII

    # Wait up to 20s for the named pipe to appear.
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $MeshPipe) {
            Pass "mesh sidecar up (pid $($proc.Id), pipe $MeshPipe)"
            return
        }
        Start-Sleep -Milliseconds 500
    }
    Warn "mesh sidecar didn't open the named pipe in 20s. Recent log:"
    Get-Content $MeshLog -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "      | $_" }
    Fail "mesh sidecar failed to start"
}

function Start-PythonSidecar {
    Step "Starting Python sidecar"
    if (Is-Running $PyPidFile) {
        Pass "python sidecar already running (pid $(Get-Content $PyPidFile))"
        return
    }
    Info "starting on 127.0.0.1:$PythonPort..."
    $proc = Start-Process -FilePath $VenvPython `
        -ArgumentList "-m", "potluck_local.app", "--port", $PythonPort `
        -WorkingDirectory $PyDir `
        -RedirectStandardOutput $PyLog `
        -RedirectStandardError "$PyLog.err" `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $PyPidFile -Value $proc.Id -Encoding ASCII

    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$PythonPort/health" -TimeoutSec 1 -UseBasicParsing -ErrorAction Stop
            if ($resp.StatusCode -eq 200) {
                Pass "python sidecar healthy (pid $($proc.Id))"
                return
            }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    Warn "python sidecar didn't return /health in 20s. Recent log:"
    Get-Content $PyLog -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "      | $_" }
    Fail "python sidecar failed to start"
}

function Launch-ElectronApp {
    Step "Launching Electron desktop app"
    if (Is-Running $AppPidFile) {
        Pass "Electron app already running (pid $(Get-Content $AppPidFile))"
        return
    }
    Info "starting pnpm dev (Vite + Electron)..."
    # 'pnpm dev' in the electron dir runs Vite + Electron concurrently.
    # We exclude the dev:python step because our Python sidecar is already
    # running from Start-PythonSidecar above.
    $proc = Start-Process -FilePath "pnpm" `
        -ArgumentList "exec", "electron", "." `
        -WorkingDirectory $ElectronDir `
        -RedirectStandardOutput $AppLog `
        -RedirectStandardError "$AppLog.err" `
        -WindowStyle Normal `
        -PassThru
    Set-Content -LiteralPath $AppPidFile -Value $proc.Id -Encoding ASCII
    Pass "Electron app launching (pid $($proc.Id))"
    Info "you can pair this machine into a household via the app's Household view"
}

# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

Step "Potluck household contributor - Windows setup"
Info "repo root: $RepoRoot"

# Pre-flight checks: catch the environmental issues that historically
# caused setup to fail mid-install (admin, long-paths, network, etc.).
# If preflight reports a hard failure we exit immediately - cheaper to
# fix prereqs upfront than to discover them 15 minutes into a build.
$preflightScript = Join-Path $ScriptDir "setup-household-preflight.ps1"
if (Test-Path $preflightScript) {
    Step "Running pre-flight checks"
    & $preflightScript -AutoFix
    if ($LASTEXITCODE -ne 0) {
        Fail "pre-flight checks failed (see above). Fix the listed issues + re-run this script."
    }
} else {
    Warn "preflight script not found at $preflightScript; skipping checks (continuing at your own risk)"
}

# Setup phase
Install-Prerequisites
Build-MeshSidecar
Setup-PythonVenv
Build-ElectronApp

if (-not $SetupOnly) {
    Create-DesktopShortcut
}

# Run phase (only if -RunNow flag is set OR user explicitly asks during interactive)
$shouldRunNow = $RunNow.IsPresent
if (-not $SetupOnly -and -not $RunNow) {
    $ans = Ask "Launch Potluck now? (y/n)" "y"
    $shouldRunNow = ($ans.ToLower() -in @("y", "yes"))
}

if ($shouldRunNow) {
    Start-MeshSidecar
    Start-PythonSidecar
    Launch-ElectronApp

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " Potluck household contributor is RUNNING " -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Logs:" -ForegroundColor DarkGray
    Write-Host "    Mesh:    $MeshLog"
    Write-Host "    Python:  $PyLog"
    Write-Host "    Electron: $AppLog"
    Write-Host ""
    Write-Host "  Use the Electron app's Household view to pair this machine"
    Write-Host "  into an existing household, or to start a new one."
    Write-Host ""
    Write-Host "  To stop everything:   $($MyInvocation.MyCommand.Path) -Stop"
    Write-Host "  To check status:      $($MyInvocation.MyCommand.Path) -Status"
    Write-Host "  Or close the Electron app window + use Task Manager to kill the sidecars."
    Write-Host ""
} else {
    Step "Setup complete"
    Info "double-click the 'Potluck' shortcut on your Desktop to launch"
    Info "or re-run with -RunNow to launch right now"
}

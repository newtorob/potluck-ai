#Requires -Version 5.1
<#
.SYNOPSIS
    Pre-flight environment check for Potluck household contributor setup on Windows.

.DESCRIPTION
    Runs in under a minute. Catches the environmental issues that
    historically cause setup-household.ps1 to fail mid-install:

      1. Not running as administrator (winget + registry + wintun need it)
      2. Windows long-path support disabled (pip + node_modules blow past 260 chars)
      3. Insufficient free disk space (Electron runtime + builds need ~6 GB)
      4. Network unreachable (DNS, PyPI, GitHub, npm registry, Electron's CDN)
      5. Coordinator at the Mac LAN IP unreachable (no point installing if we
         can't pair afterward)
      6. PowerShell execution policy too restrictive for the main script
      7. Stale partial state from a previous failed run that should be cleaned

    Each check returns one of:
      OK     - all good
      WARN   - non-fatal, but worth knowing
      FAIL   - blocks setup; explains how to fix in plain English

    Exit code: 0 if no FAIL conditions, 1 otherwise.

.NOTES
    Designed to be called by setup-household.ps1 at the very top, OR run
    standalone to diagnose before kicking off an install:

        .\scripts\setup-household-preflight.ps1
        .\scripts\setup-household-preflight.ps1 -CoordinatorUrl http://192.168.1.10:8400
        .\scripts\setup-household-preflight.ps1 -AutoFix    # try to fix what we can
#>

[CmdletBinding()]
param(
    [string]$CoordinatorUrl,
    [switch]$AutoFix,
    [switch]$Help
)

if ($Help) {
    Get-Help $MyInvocation.MyCommand.Path -Full
    exit 0
}

# ASCII-only output. No em-dashes, no smart quotes.

if ([Console]::OutputEncoding.WebName -ne 'utf-8') {
    # Force UTF-8 output where possible; harmless if it fails.
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
}

# ----------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------

function Header($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function OK($msg)     { Write-Host "    [ok]   $msg" -ForegroundColor Green }
function Warn($msg)   { Write-Host "    [warn] $msg" -ForegroundColor Yellow }
function Fail($msg)   { Write-Host "    [fail] $msg" -ForegroundColor Red }
function Info($msg)   { Write-Host "    $msg" -ForegroundColor DarkGray }

$script:Failures = @()
$script:Warnings = @()

function Record-Fail($checkName, $message, $fix) {
    Fail $message
    if ($fix) { Info "fix: $fix" }
    $script:Failures += [PSCustomObject]@{ Check = $checkName; Message = $message; Fix = $fix }
}

function Record-Warn($checkName, $message) {
    Warn $message
    $script:Warnings += [PSCustomObject]@{ Check = $checkName; Message = $message }
}

# ----------------------------------------------------------------------------
# Check 1: Administrator privileges
# ----------------------------------------------------------------------------

function Check-Admin {
    Header "Check 1: Administrator privileges"
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]$current
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if ($isAdmin) {
        OK "running as administrator"
        return $true
    }

    Record-Fail "admin" "PowerShell is not elevated" `
        "Close this window. Find PowerShell (or Terminal) in the Start menu, right-click it, choose 'Run as administrator'. Then cd back to the repo and re-run this script."
    return $false
}

# ----------------------------------------------------------------------------
# Check 2: Long-path support
# ----------------------------------------------------------------------------

function Check-LongPaths {
    Header "Check 2: Windows long-path support"
    $regPath = "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem"
    $regName = "LongPathsEnabled"

    try {
        $val = Get-ItemProperty -Path $regPath -Name $regName -ErrorAction Stop
        if ($val.LongPathsEnabled -eq 1) {
            OK "long paths enabled in registry"
            return $true
        }
    } catch {
        # Property doesn't exist - same as disabled.
    }

    # Not enabled. If admin + AutoFix, try to set it.
    if ($AutoFix) {
        $current = [Security.Principal.WindowsIdentity]::GetCurrent()
        $isAdmin = ([Security.Principal.WindowsPrincipal]$current).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if ($isAdmin) {
            Info "AutoFix: enabling LongPathsEnabled via registry"
            try {
                New-ItemProperty -Path $regPath -Name $regName -Value 1 -PropertyType DWORD -Force | Out-Null
                OK "long paths enabled (a fresh PowerShell window is required for it to take effect)"
                Record-Warn "long-paths-fresh-shell" "long paths just got enabled; CLOSE this PowerShell and open a new admin one before continuing"
                return $true
            } catch {
                Record-Fail "long-paths" "could not write registry: $($_.Exception.Message)" `
                    "ensure you're elevated, or set it manually: New-ItemProperty -Path '$regPath' -Name '$regName' -Value 1 -PropertyType DWORD -Force"
                return $false
            }
        }
    }

    Record-Fail "long-paths" "Windows long-path support is disabled" `
        "In an admin PowerShell, run: New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name 'LongPathsEnabled' -Value 1 -PropertyType DWORD -Force . Then CLOSE that window and open a fresh admin shell."
    return $false
}

# ----------------------------------------------------------------------------
# Check 3: Free disk space
# ----------------------------------------------------------------------------

function Check-DiskSpace {
    Header "Check 3: Free disk space"
    $drive = (Get-Item $env:USERPROFILE).PSDrive
    $freeGB = [math]::Round($drive.Free / 1GB, 1)

    if ($freeGB -ge 8) {
        OK "$freeGB GB free on $($drive.Name): (need >= 6 GB)"
        return $true
    }
    if ($freeGB -ge 6) {
        Record-Warn "disk-space" "$freeGB GB free on $($drive.Name): (recommended >= 8 GB; will work but tight)"
        return $true
    }
    Record-Fail "disk-space" "only $freeGB GB free on $($drive.Name): (need at least 6 GB for builds + Electron runtime)" `
        "free up space, or relocate the repo to a drive with more free space"
    return $false
}

# ----------------------------------------------------------------------------
# Check 4: Network endpoints
# ----------------------------------------------------------------------------

function Test-EndpointReachable {
    param([string]$Url, [int]$TimeoutSec = 8)
    try {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $resp = Invoke-WebRequest -Uri $Url -Method Head -TimeoutSec $TimeoutSec -UseBasicParsing -ErrorAction Stop
        $ErrorActionPreference = $prev
        return ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 500)
    } catch {
        return $false
    }
}

function Check-Network {
    Header "Check 4: Network endpoints (PyPI, GitHub, npm)"
    $allOk = $true

    $endpoints = @(
        @{ Url = "https://pypi.org/simple/"; Name = "PyPI" },
        @{ Url = "https://github.com/"; Name = "GitHub" },
        @{ Url = "https://registry.npmjs.org/"; Name = "npm registry" },
        @{ Url = "https://abetlen.github.io/llama-cpp-python/whl/cpu/"; Name = "abetlen llama-cpp-python wheels" }
    )

    foreach ($ep in $endpoints) {
        if (Test-EndpointReachable -Url $ep.Url) {
            OK "$($ep.Name) reachable"
        } else {
            Record-Warn "network-$($ep.Name.ToLower())" "$($ep.Name) unreachable at $($ep.Url)"
            $allOk = $false
        }
    }

    if (-not $allOk) {
        Info "VPN, corporate proxy, or DNS issue is the usual cause. Disable VPN for the install + re-run."
    }
    return $allOk
}

# ----------------------------------------------------------------------------
# Check 5: Coordinator (Mac/laptop)
# ----------------------------------------------------------------------------

function Check-Coordinator {
    Header "Check 5: Laptop coordinator reachable"
    if (-not $CoordinatorUrl) {
        # Try to load from previous run if we saved it.
        $coordEnvFile = Join-Path $env:LOCALAPPDATA "Potluck\coordinator.url"
        if (Test-Path $coordEnvFile) {
            $CoordinatorUrl = (Get-Content $coordEnvFile -Raw).Trim()
            Info "using saved coordinator URL: $CoordinatorUrl"
        }
    }
    if (-not $CoordinatorUrl) {
        Record-Warn "coordinator-skipped" "no -CoordinatorUrl provided; skipping. You'll be prompted later by the main installer."
        return $true
    }

    $healthUrl = $CoordinatorUrl.TrimEnd('/') + "/health"
    if (Test-EndpointReachable -Url $healthUrl -TimeoutSec 5) {
        OK "coordinator alive at $healthUrl"
        return $true
    }

    Record-Fail "coordinator" "cannot reach coordinator at $healthUrl" `
        "1) Confirm 'make up' is running on your Mac. 2) Re-check the Mac's LAN IP with 'ipconfig getifaddr en0'. 3) macOS firewall: System Settings > Network > Firewall - disable for the smoke test, OR allow inbound on port 8400."
    return $false
}

# ----------------------------------------------------------------------------
# Check 6: Execution policy
# ----------------------------------------------------------------------------

function Check-ExecutionPolicy {
    Header "Check 6: PowerShell execution policy"
    $policy = Get-ExecutionPolicy -Scope Process
    if ($policy -in @('Bypass', 'Unrestricted', 'RemoteSigned')) {
        OK "process policy is $policy (scripts can run)"
        return $true
    }
    # Auto-fix is trivial here: set it for THIS process only.
    if ($AutoFix) {
        Set-ExecutionPolicy -Scope Process Bypass -Force
        OK "set Process execution policy to Bypass (session-only)"
        return $true
    }
    Record-Fail "execution-policy" "execution policy is $policy (blocks scripts)" `
        "run 'Set-ExecutionPolicy -Scope Process Bypass -Force' in this window before continuing"
    return $false
}

# ----------------------------------------------------------------------------
# Check 7: Existing artifacts (stale or in-progress)
# ----------------------------------------------------------------------------

function Check-StaleArtifacts {
    Header "Check 7: Existing build artifacts"
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $venvPython = Join-Path $repoRoot "apps\desktop\python\.venv\Scripts\python.exe"
    $meshBin = Join-Path $repoRoot "apps\desktop\go\potluck-mesh\dist\potluck-mesh.exe"
    $electronDeps = Join-Path $repoRoot "apps\desktop\electron\node_modules"

    $found = @()
    if (Test-Path $venvPython) { $found += "Python venv" }
    if (Test-Path $meshBin) { $found += "Go mesh binary" }
    if (Test-Path $electronDeps) { $found += "Electron node_modules" }

    if ($found.Count -eq 0) {
        OK "fresh checkout, no existing artifacts"
    } else {
        OK "found existing artifacts: $($found -join ', ') (setup script will skip rebuilding these)"
    }
    return $true
}

# ----------------------------------------------------------------------------
# Run all checks
# ----------------------------------------------------------------------------

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Potluck household contributor - Windows pre-flight checks  " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$adminOk      = Check-Admin
$longPathsOk  = Check-LongPaths
$diskOk       = Check-DiskSpace
$networkOk    = Check-Network
$coordOk      = Check-Coordinator
$policyOk     = Check-ExecutionPolicy
$artifactsOk  = Check-StaleArtifacts

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan

if ($script:Failures.Count -gt 0) {
    Write-Host " $($script:Failures.Count) BLOCKING issue(s) - fix and re-run preflight " -ForegroundColor Red
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    foreach ($f in $script:Failures) {
        Write-Host "  - $($f.Check): $($f.Message)" -ForegroundColor Red
        Write-Host "    fix: $($f.Fix)" -ForegroundColor DarkGray
    }
    Write-Host ""
    Write-Host "  After fixing, re-run:  .\scripts\setup-household-preflight.ps1"
    Write-Host "  Add -AutoFix to auto-fix what's safe to fix (registry, exec policy)"
    Write-Host ""
    exit 1
}

if ($script:Warnings.Count -gt 0) {
    Write-Host " all checks passed ($($script:Warnings.Count) warnings) " -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    foreach ($w in $script:Warnings) {
        Write-Host "  - $($w.Check): $($w.Message)" -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "  Safe to continue. Run:  .\scripts\setup-household.ps1"
    Write-Host ""
    exit 0
}

Write-Host " ALL CHECKS PASSED " -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Ready to install. Run:  .\scripts\setup-household.ps1"
Write-Host ""
exit 0

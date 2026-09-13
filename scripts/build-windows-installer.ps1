# build-windows-installer.ps1 - cut the Windows installer from a clean checkout.
#
# The Makefile's installer-bundle target drives the macOS and Linux builds
# through pnpm's prepack chain, which is POSIX shell (rm -rf, mkdir -p, bash).
# On Windows pnpm runs scripts through cmd.exe, so that chain does not work and
# the Windows release was being assembled by hand. Hand assembly is how 0.1.3
# shipped a CPU-only llama.cpp backend without anyone noticing: nothing checked.
#
# This script runs the same steps explicitly, in order, and fails loudly when
# the GPU backend set is missing from the bundle.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\build-windows-installer.ps1
#
#   -SkipDeps     reuse the existing venv and node_modules
#   -AllowDirty   build from uncommitted changes (never for a release)
#
# Requires: Python 3.11+, Node 20+, pnpm, Go, Git. No C toolchain: the Python
# sidecar is packaged from prebuilt wheels and the mesh sidecar needs no cgo.

param(
  [switch]$SkipDeps,
  [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot "apps\desktop\python"
$Frontend = Join-Path $RepoRoot "apps\desktop\frontend"
$Electron = Join-Path $RepoRoot "apps\desktop\electron"
$Mesh = Join-Path $RepoRoot "apps\desktop\go\potluck-mesh"
$Venv = Join-Path $Python ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# PowerShell treats native-command stderr as a terminating error under
# $ErrorActionPreference = "Stop", and pip, go and electron-builder all write
# progress there. Scope it down and check the exit code instead.
function Invoke-Native {
  param([string]$File, [string[]]$Arguments, [string]$WorkDir = $RepoRoot)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    Push-Location $WorkDir
    & $File @Arguments
    $code = $LASTEXITCODE
  } finally {
    Pop-Location
    $ErrorActionPreference = $prev
  }
  if ($code -ne 0) { throw "$File $($Arguments -join ' ') failed with exit code $code" }
}

Step "Checking the working tree"
Push-Location $RepoRoot
$dirty = @(git status --porcelain)
$sha = (git rev-parse --short HEAD)
Pop-Location
if ($dirty.Count -gt 0 -and -not $AllowDirty) {
  Write-Host "Working tree has $($dirty.Count) uncommitted change(s):" -ForegroundColor Red
  $dirty | Select-Object -First 20 | ForEach-Object { Write-Host "  $_" }
  throw "Refusing to build a release from a dirty tree. Commit, or pass -AllowDirty."
}
Write-Host "  HEAD $sha, $($dirty.Count) uncommitted change(s)"

if (-not $SkipDeps) {
  Step "Python environment"
  if (-not (Test-Path $VenvPy)) { Invoke-Native "py" @("-3.11", "-m", "venv", $Venv) $Python }
  Invoke-Native $VenvPy @("-m", "pip", "install", "--upgrade", "pip") $Python
  Invoke-Native $VenvPy @("-m", "pip", "install", "-r", "requirements.txt") $Python
  Invoke-Native $VenvPy @("-m", "pip", "install", "-r", "requirements-build.txt") $Python
}

Step "GPU backend set (pinned by URL + SHA-256)"
Invoke-Native $VenvPy @("scripts\vendor_gpu_backend.py") $Python

Step "Python sidecars"
Invoke-Native $VenvPy @("build.py") $Python
Invoke-Native $VenvPy @("build-mcp.py") $Python

if (-not $SkipDeps) {
  Step "Node dependencies"
  Invoke-Native "pnpm" @("install") $Frontend
  Invoke-Native "pnpm" @("install") $Electron
}

Step "Frontend bundle"
Invoke-Native "pnpm" @("exec", "vite", "build") $Frontend

Step "Staging sidecars into the Electron resources"
$Resources = Join-Path $Electron "resources"
$PyResources = Join-Path $Resources "python"
if (Test-Path $PyResources) { Remove-Item -Recurse -Force $PyResources }
New-Item -ItemType Directory -Force -Path $PyResources | Out-Null
Copy-Item (Join-Path $Python "dist\potluck-local") $PyResources -Recurse
Copy-Item (Join-Path $Python "dist\potluck-mcp") $PyResources -Recurse

Step "Mesh sidecar"
Invoke-Native "go" @("build", "-ldflags", "-s -w", "-o", "dist\potluck-mesh.exe", ".\cmd\potluck-mesh") $Mesh
$MeshResources = Join-Path $Resources "mesh"
if (Test-Path $MeshResources) { Remove-Item -Recurse -Force $MeshResources }
New-Item -ItemType Directory -Force -Path $MeshResources | Out-Null
Copy-Item (Join-Path $Mesh "dist\potluck-mesh.exe") $MeshResources
# wintun.dll drives the WireGuard adapter; wireguard-go loads it by name from
# beside the executable. See apps/desktop/go/potluck-mesh/third_party/windows.
Copy-Item (Join-Path $Mesh "third_party\windows\wintun.dll") $MeshResources

Step "Verifying the bundled inference backends"
$LlamaRoot = Join-Path $PyResources "potluck-local\_internal\llama_cpp"
$BackendDirs = @(Get-ChildItem -Path $LlamaRoot -Directory -Filter "lib-*" -ErrorAction SilentlyContinue)
if ($BackendDirs.Count -eq 0) {
  throw "No GPU backend set in the bundle at $LlamaRoot. This installer would be CPU only."
}
foreach ($d in $BackendDirs) {
  $dlls = @(Get-ChildItem $d.FullName -Filter *.dll)
  $mb = [math]::Round((($dlls | Measure-Object Length -Sum).Sum / 1MB), 1)
  Write-Host "  $($d.Name): $($dlls.Count) libraries, $mb MB"
}
$cpuLib = Join-Path $LlamaRoot "lib"
if (-not (Test-Path (Join-Path $cpuLib "llama.dll"))) {
  throw "The CPU fallback backend is missing from $cpuLib."
}
Write-Host "  lib: CPU fallback present"

Step "Build stamp"
Invoke-Native "node" @("scripts\generate-build-info.js") $Electron
Get-Content (Join-Path $Electron "build-info.json")

Step "Installer"
$env:CSC_IDENTITY_AUTO_DISCOVERY = "false"
Invoke-Native "pnpm" @("exec", "electron-builder", "--win") $Electron
# Windows ships unsigned, so the published hash is how a user verifies the file.
Invoke-Native "node" @("scripts\hash-release.js") $Electron

Step "Artifacts"
$out = Join-Path $Electron "out"
Get-ChildItem $out -Filter "*.exe" | ForEach-Object {
  $sha256 = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
  $sha512 = [Convert]::ToBase64String(
    [System.Security.Cryptography.SHA512]::Create().ComputeHash(
      [System.IO.File]::ReadAllBytes($_.FullName)))
  Write-Host ""
  Write-Host "  $($_.Name)"
  Write-Host "  bytes  : $($_.Length)"
  Write-Host "  sha256 : $sha256"
  Write-Host "  sha512 : $sha512"
}
Write-Host "`nBUILD_OK $sha"

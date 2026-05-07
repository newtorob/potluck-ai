#Requires -Version 5.1
<#
.SYNOPSIS
    One-shot Windows inference setup for the Potluck contributor (Phase G m3
    step 5). Installs llama-cpp-python with CUDA support, downloads a GGUF
    model, configures the contributor's .env, smoke-tests the load.

.DESCRIPTION
    Run AFTER scripts\setup-windows-contributor.ps1. Idempotent.

    What it does:
      1. Verifies the contributor venv exists (setup-windows-contributor.ps1 ran).
      2. Detects CUDA driver version via nvidia-smi.
      3. Installs llama-cpp-python from the matching CUDA wheel index;
         falls back to lower CUDA versions if the first try fails.
      4. Downloads the GGUF (defaults to qwen-2.5-3b-instruct-q4 ~1.8 GB).
      5. Appends inference config to apps\desktop\python\.env.
      6. Smoke-tests by loading the model + generating 5 tokens locally
         (no coordinator round-trip), so you know inference works on this
         box BEFORE you hit it from the network.
      7. Tells you to restart the contributor.

.PARAMETER ModelSlug
    The model identifier the coordinator will route by. Must match what the
    coordinator dispatches. Default: qwen-2.5-3b-instruct-q4 (matches the
    Mac's existing model, so cross-machine routing tests "just work").

.PARAMETER ModelRepo / ModelFile
    Hugging Face source. Default targets a widely-available repackage of
    Qwen2.5-3B-Instruct.

.PARAMETER NoModel
    Skip the model download (useful if you've already copied a GGUF over).
    You'll still need POTLUCK_MODEL_PATH set in .env.

.PARAMETER NoSmokeTest
    Skip the local smoke-load. Useful if the model is huge and you want to
    avoid loading it twice (once here + once when the contributor runs).
#>

param(
    [string]$ModelSlug = "qwen-2.5-3b-instruct-q4",
    [string]$ModelRepo = "bartowski/Qwen2.5-3B-Instruct-GGUF",
    [string]$ModelFile = "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
    [int]$NCtx = 4096,
    [int]$NGpuLayers = -1,
    [switch]$NoModel,
    [switch]$NoSmokeTest
)

# ---------- helpers ----------------------------------------------------------

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Pass($msg) { Write-Host "    [ok]   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    [warn] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "    [fail] $msg" -ForegroundColor Red; exit 1 }

$RepoRoot   = Split-Path -Parent $PSScriptRoot
$SidecarDir = Join-Path $RepoRoot "apps\desktop\python"
$VenvPython = Join-Path $SidecarDir ".venv\Scripts\python.exe"
$VenvPip    = Join-Path $SidecarDir ".venv\Scripts\pip.exe"
$EnvFile    = Join-Path $SidecarDir ".env"
$ModelDir   = Join-Path $HOME ".potluck\models\$ModelSlug"
$ModelPath  = Join-Path $ModelDir "model.gguf"

# ---------- 1. Pre-flight ----------------------------------------------------

Step "Pre-flight"
if (-not (Test-Path $VenvPython)) {
    Fail "venv missing at $VenvPython - run scripts\setup-windows-contributor.ps1 first."
}
if (-not (Test-Path $EnvFile)) {
    Fail ".env missing at $EnvFile - run scripts\setup-windows-contributor.ps1 first."
}
Pass "venv + .env present"

# ---------- 2. Detect CUDA version -------------------------------------------

Step "Detecting CUDA driver"
$cudaMajor = $null
$cudaMinor = $null
try {
    $nvOutput = & nvidia-smi 2>$null
    if ($LASTEXITCODE -ne 0) { throw "nvidia-smi exited $LASTEXITCODE" }

    $cudaLine = $nvOutput | Select-String -Pattern "CUDA Version:\s*(\d+)\.(\d+)" -List
    if ($cudaLine -and $cudaLine.Matches.Count -gt 0) {
        $cudaMajor = [int]$cudaLine.Matches[0].Groups[1].Value
        $cudaMinor = [int]$cudaLine.Matches[0].Groups[2].Value
        Pass "driver supports CUDA $cudaMajor.$cudaMinor"
    } else {
        Warn "nvidia-smi ran but no CUDA Version line found. Falling back to CPU build."
    }
} catch {
    Warn "nvidia-smi not found or failed: $($_.Exception.Message)"
    Warn "No NVIDIA GPU detected - falling back to CPU build (slow!)."
}

# ---------- 3. Install llama-cpp-python --------------------------------------

Step "Installing llama-cpp-python"

# Wheel candidates in preference order: highest CUDA <= driver, then fallbacks.
# abetlen publishes wheels at https://abetlen.github.io/llama-cpp-python/whl/cuXXX/
$wheelCandidates = @()
if ($cudaMajor -ne $null) {
    $allCuda = @("126", "125", "124", "123", "122", "121", "118")
    foreach ($cu in $allCuda) {
        $wheelMajor = [int]($cu.Substring(0, 1) + $cu.Substring(1, 1))  # 126 -> 12
        $wheelMinor = [int]$cu.Substring(2, 1)                           # 126 -> 6
        if ($wheelMajor -lt $cudaMajor -or
            ($wheelMajor -eq $cudaMajor -and $wheelMinor -le $cudaMinor)) {
            $wheelCandidates += "https://abetlen.github.io/llama-cpp-python/whl/cu$cu"
        }
    }
}
# Always include the plain PyPI as last-resort CPU build
$wheelCandidates += $null

$installed = $false
foreach ($wheel in $wheelCandidates) {
    if ($wheel -ne $null) {
        Write-Host "    trying $wheel"
        & $VenvPip install --upgrade --quiet llama-cpp-python --extra-index-url $wheel 2>$null
    } else {
        Write-Host "    trying PyPI (CPU build)"
        & $VenvPip install --upgrade --quiet llama-cpp-python 2>$null
    }
    if ($LASTEXITCODE -eq 0) {
        # Verify it imports
        $ver = & $VenvPython -c "import llama_cpp; print(llama_cpp.__version__)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) {
            Pass "llama-cpp-python $ver installed"
            $installed = $true
            break
        }
    }
    Warn "wheel/index didn't work, trying next"
}

if (-not $installed) {
    Fail "Couldn't install llama-cpp-python from any candidate. Try manually: pip install llama-cpp-python"
}

# ---------- 4. Download model ------------------------------------------------

if (-not $NoModel) {
    if (Test-Path $ModelPath) {
        $sizeMB = [math]::Round((Get-Item $ModelPath).Length / 1MB, 1)
        Pass "model already present at $ModelPath ($sizeMB MB) - skipping download"
    } else {
        Step "Downloading model from Hugging Face ($ModelRepo / $ModelFile)"
        New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null

        # Use huggingface_hub for the download. It handles resume + progress.
        & $VenvPip install --upgrade --quiet huggingface-hub 2>$null
        if ($LASTEXITCODE -ne 0) { Fail "couldn't install huggingface-hub" }

        $dlScript = @"
from huggingface_hub import hf_hub_download
import shutil, sys
try:
    src = hf_hub_download(repo_id='$ModelRepo', filename='$ModelFile')
    shutil.copy(src, r'$ModelPath')
    print('OK')
except Exception as e:
    print('ERR', e, file=sys.stderr)
    sys.exit(1)
"@
        & $VenvPython -c $dlScript
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $ModelPath)) {
            Fail "model download failed. Check the repo/file params or download manually to $ModelPath."
        }
        $sizeMB = [math]::Round((Get-Item $ModelPath).Length / 1MB, 1)
        Pass "downloaded $sizeMB MB to $ModelPath"
    }
} else {
    Warn "skipping download per -NoModel; expecting $ModelPath to already exist"
    if (-not (Test-Path $ModelPath)) {
        Fail "no model at $ModelPath and -NoModel was passed - put the GGUF there manually."
    }
}

# ---------- 5. Update .env ---------------------------------------------------

Step "Updating $EnvFile with inference config"

# Read existing .env, strip any prior inference-related lines, append fresh ones.
$inferenceKeys = @(
    "POTLUCK_INFERENCE_ENABLED",
    "POTLUCK_MODEL_SLUG",
    "POTLUCK_MODEL_PATH",
    "POTLUCK_N_GPU_LAYERS",
    "POTLUCK_N_CTX"
)

$existing = @()
if (Test-Path $EnvFile) {
    $existing = Get-Content $EnvFile | Where-Object {
        $line = $_
        -not ($inferenceKeys | Where-Object { $line -match "^$_=" })
    }
}

$updated = $existing + @(
    "",
    "# Inference (added by scripts\setup-windows-inference.ps1)",
    "POTLUCK_INFERENCE_ENABLED=true",
    "POTLUCK_MODEL_SLUG=$ModelSlug",
    "POTLUCK_MODEL_PATH=$ModelPath",
    "POTLUCK_N_GPU_LAYERS=$NGpuLayers",
    "POTLUCK_N_CTX=$NCtx"
)

# UTF-8 without BOM so pydantic-settings reads cleanly
[System.IO.File]::WriteAllLines(
    $EnvFile, $updated, [System.Text.UTF8Encoding]::new($false)
)
Pass "wrote inference config (5 keys)"

# ---------- 6. Smoke-test ----------------------------------------------------

if (-not $NoSmokeTest) {
    Step "Smoke-testing local model load (5-token generation)"
    Write-Host "    cold-loading model into VRAM, this takes 10-60s..."

    $smokeScript = @"
import sys, time
from llama_cpp import Llama
t0 = time.monotonic()
try:
    llm = Llama(
        model_path=r'$ModelPath',
        n_ctx=$NCtx,
        n_gpu_layers=$NGpuLayers,
        verbose=False,
    )
except Exception as e:
    print('LOAD_ERR', e, file=sys.stderr)
    sys.exit(2)
load_s = time.monotonic() - t0

t1 = time.monotonic()
out = llm.create_completion(prompt='Hi', max_tokens=5, temperature=0.1)
gen_s = time.monotonic() - t1
text = out['choices'][0]['text'].strip()

# Counter-only output, no prompt content
print(f'OK load_s={load_s:.1f} gen_s={gen_s:.2f} tokens={len(text.split())}')
"@

    & $VenvPython -c $smokeScript
    if ($LASTEXITCODE -ne 0) {
        Fail "smoke test failed. Check the error above. Common causes: model path wrong, VRAM exhausted, llama-cpp-python wheel/CUDA mismatch."
    }
    Pass "smoke test passed"
} else {
    Warn "skipping smoke test per -NoSmokeTest"
}

# ---------- 7. Done ----------------------------------------------------------

Write-Host ""
Step "Inference setup complete"
Write-Host @"

Model:      $ModelSlug
Path:       $ModelPath
GPU layers: $NGpuLayers (-1 = all to GPU)
Context:    $NCtx tokens

To activate: restart the contributor sidecar.
   - If you have Start-PotluckContributor on the desktop, close that
     window (if open) and double-click it again.
   - First job after restart will cold-load the model into VRAM (10-60s).
     Subsequent jobs are fast.

To verify from the dev Mac after restart:
   curl -X POST http://<this-windows-tailnet-or-lan-ip>:8400/jobs ...
   (or just run a test from the Mac targeting model_slug=$ModelSlug;
    the coordinator will route to whichever peer serves that model.)

If something looks wrong, re-run with -NoModel -NoSmokeTest after fixing
to skip the slow steps.

"@

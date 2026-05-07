#Requires -Version 5.1
<#
.SYNOPSIS
    One-shot Windows inference setup for the Potluck contributor (Phase G m3).
    Auto-detects your GPU vendor and picks the right backend:

      - NVIDIA   -> llama-cpp-python with CUDA wheels (in-process inference)
      - AMD      -> Ollama (single .exe, handles Vulkan/ROCm internally)
      - Intel    -> Ollama (CPU build, optionally Vulkan in newer Ollama)
      - No GPU   -> Ollama (CPU build, slow but works)

.DESCRIPTION
    Run AFTER scripts\setup-windows-contributor.ps1. Idempotent.

    What it does:
      1. Detects GPU vendor from Get-CimInstance Win32_VideoController.
      2. Picks a backend (overrideable via -Backend).
      3. Runs the per-backend install:
         - NVIDIA: detects CUDA driver, installs llama-cpp-python from the
           matching prebuilt wheel index, downloads a GGUF from Hugging Face.
         - Ollama: installs Ollama via winget if missing, waits for the
           daemon, pulls the model.
      4. Writes the matching keys to apps\desktop\python\.env.
      5. Smoke-tests the inference path locally (no coordinator round-trip).
      6. Tells you to restart the contributor sidecar.

.PARAMETER Backend
    Force a specific backend. Default: auto-detect from GPU vendor.
    Values: "auto", "llama-cpp", "ollama".

.PARAMETER ModelSlug
    Model identifier. Per-backend defaults if omitted:
      llama-cpp -> "qwen-2.5-3b-instruct-q4"
      ollama    -> "qwen2.5:3b-instruct-q4_K_M"

.PARAMETER ModelRepo / ModelFile
    Hugging Face source for llama-cpp path (ignored on Ollama path).
#>

param(
    [ValidateSet("auto", "llama-cpp", "ollama")]
    [string]$Backend = "auto",

    [string]$ModelSlug = "",
    [string]$ModelRepo = "bartowski/Qwen2.5-3B-Instruct-GGUF",
    [string]$ModelFile = "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
    [int]$NCtx = 4096,
    [int]$NGpuLayers = -1,
    [string]$OllamaUrl = "http://127.0.0.1:11434",
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

# ---------- 1. Pre-flight ----------------------------------------------------

Step "Pre-flight"
if (-not (Test-Path $VenvPython)) {
    Fail "venv missing at $VenvPython - run scripts\setup-windows-contributor.ps1 first."
}
if (-not (Test-Path $EnvFile)) {
    Fail ".env missing at $EnvFile - run scripts\setup-windows-contributor.ps1 first."
}
Pass "venv + .env present"

# ---------- 2. Detect GPU + pick backend -------------------------------------

Step "Detecting GPU vendor"
$gpuVendor = "none"
try {
    $videoControllers = Get-CimInstance Win32_VideoController -ErrorAction Stop
    foreach ($v in $videoControllers) {
        $name = $v.Name
        if ($name -match "NVIDIA|GeForce|Quadro|Tesla|RTX|GTX") {
            $gpuVendor = "nvidia"; break
        } elseif ($name -match "AMD|Radeon|RX |Vega|Navi") {
            $gpuVendor = "amd"; break
        } elseif ($name -match "Intel.*(Arc|Iris|UHD|HD Graphics)") {
            if ($gpuVendor -eq "none") { $gpuVendor = "intel" }
            # don't break - prefer discrete (NVIDIA/AMD) if also present
        }
    }
    Write-Host "    detected:" -NoNewline
    foreach ($v in $videoControllers) { Write-Host " '$($v.Name)'" -NoNewline }
    Write-Host ""
    Pass "vendor: $gpuVendor"
} catch {
    Warn "Get-CimInstance failed: $($_.Exception.Message)"
    Warn "Treating as no-GPU; will install Ollama (CPU mode)."
}

# Auto-pick backend from vendor unless overridden.
$chosenBackend = $Backend
if ($chosenBackend -eq "auto") {
    if ($gpuVendor -eq "nvidia") {
        $chosenBackend = "llama-cpp"
    } else {
        # AMD, Intel, or no GPU all go through Ollama. Ollama handles the
        # vendor-specific backend choice (Vulkan/ROCm/CPU) internally.
        $chosenBackend = "ollama"
    }
    Pass "auto-selected backend: $chosenBackend"
} else {
    Pass "using backend: $chosenBackend (override)"
}

# Per-backend default model slug.
if ($ModelSlug -eq "") {
    if ($chosenBackend -eq "ollama") {
        $ModelSlug = "qwen2.5:3b-instruct-q4_K_M"
    } else {
        $ModelSlug = "qwen-2.5-3b-instruct-q4"
    }
}

# ---------- 3a. Llama-cpp path (NVIDIA) --------------------------------------

function Install-LlamaCpp {
    Step "Detecting CUDA driver"
    $cudaMajor = $null; $cudaMinor = $null
    try {
        $nvOutput = & nvidia-smi 2>$null
        if ($LASTEXITCODE -ne 0) { throw "nvidia-smi exited $LASTEXITCODE" }
        $cudaLine = $nvOutput | Select-String -Pattern "CUDA Version:\s*(\d+)\.(\d+)" -List
        if ($cudaLine -and $cudaLine.Matches.Count -gt 0) {
            $cudaMajor = [int]$cudaLine.Matches[0].Groups[1].Value
            $cudaMinor = [int]$cudaLine.Matches[0].Groups[2].Value
            Pass "driver supports CUDA $cudaMajor.$cudaMinor"
        } else {
            Warn "nvidia-smi ran but no CUDA Version line found. Trying CPU build."
        }
    } catch {
        Warn "nvidia-smi not found or failed: $($_.Exception.Message)"
        Warn "Will try CPU build (slow!)."
    }

    Step "Installing llama-cpp-python"
    $wheelCandidates = @()
    if ($cudaMajor -ne $null) {
        $allCuda = @("126", "125", "124", "123", "122", "121", "118")
        foreach ($cu in $allCuda) {
            $wheelMajor = [int]($cu.Substring(0, 1) + $cu.Substring(1, 1))
            $wheelMinor = [int]$cu.Substring(2, 1)
            if ($wheelMajor -lt $cudaMajor -or
                ($wheelMajor -eq $cudaMajor -and $wheelMinor -le $cudaMinor)) {
                $wheelCandidates += "https://abetlen.github.io/llama-cpp-python/whl/cu$cu"
            }
        }
    }
    $wheelCandidates += $null  # PyPI CPU fallback

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
            $ver = & $VenvPython -c "import llama_cpp; print(llama_cpp.__version__)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver) {
                Pass "llama-cpp-python $ver installed"
                $installed = $true; break
            }
        }
        Warn "wheel/index didn't work, trying next"
    }
    if (-not $installed) {
        Fail "Couldn't install llama-cpp-python from any candidate."
    }

    # Download model
    $modelDir   = Join-Path $HOME ".potluck\models\$ModelSlug"
    $modelPath  = Join-Path $modelDir "model.gguf"
    if (-not $NoModel -and -not (Test-Path $modelPath)) {
        Step "Downloading model from Hugging Face ($ModelRepo / $ModelFile)"
        New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
        & $VenvPip install --upgrade --quiet huggingface-hub 2>$null
        if ($LASTEXITCODE -ne 0) { Fail "couldn't install huggingface-hub" }
        $dlScript = @"
from huggingface_hub import hf_hub_download
import shutil, sys
try:
    src = hf_hub_download(repo_id='$ModelRepo', filename='$ModelFile')
    shutil.copy(src, r'$modelPath')
    print('OK')
except Exception as e:
    print('ERR', e, file=sys.stderr)
    sys.exit(1)
"@
        & $VenvPython -c $dlScript
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $modelPath)) {
            Fail "model download failed."
        }
        $sizeMB = [math]::Round((Get-Item $modelPath).Length / 1MB, 1)
        Pass "downloaded $sizeMB MB to $modelPath"
    } elseif (Test-Path $modelPath) {
        Pass "model already at $modelPath"
    }

    # Return the inference config keys to write to .env
    return @{
        POTLUCK_INFERENCE_ENABLED = "true"
        POTLUCK_INFERENCE_BACKEND = "llama-cpp"
        POTLUCK_MODEL_SLUG        = $ModelSlug
        POTLUCK_MODEL_PATH        = $modelPath
        POTLUCK_N_GPU_LAYERS      = $NGpuLayers
        POTLUCK_N_CTX             = $NCtx
    }
}

# ---------- 3b. Ollama path (AMD / Intel / no GPU) ---------------------------

function Install-Ollama {
    Step "Checking Ollama install"
    $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollamaCmd) {
        Step "Installing Ollama (winget install Ollama.Ollama)"
        $wingetCmd = Get-Command winget -ErrorAction SilentlyContinue
        if (-not $wingetCmd) {
            Fail @"
Ollama isn't installed and winget isn't available.

Install Ollama manually:
  1. Download from https://ollama.com/download/windows
  2. Run the installer (default settings are fine)
  3. Open a NEW PowerShell window so PATH refreshes
  4. Re-run this script
"@
        }
        & winget install --id Ollama.Ollama --accept-source-agreements --accept-package-agreements -h
        if ($LASTEXITCODE -ne 0) {
            Fail "winget install failed. Try installing manually from https://ollama.com/download/windows then re-run."
        }
        # winget puts ollama on PATH but the current shell hasn't refreshed.
        # Add the typical install path so the rest of the script can use it.
        $candidatePath = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
        if (Test-Path $candidatePath) {
            $env:PATH = "$env:LOCALAPPDATA\Programs\Ollama;$env:PATH"
        }
        if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
            Fail @"
Ollama installed but not on PATH in this shell.

Open a NEW PowerShell window and re-run this script. The next session
will pick up the updated PATH automatically.
"@
        }
        Pass "Ollama installed"
    } else {
        Pass "Ollama already on PATH"
    }

    Step "Waiting for Ollama daemon at $OllamaUrl"
    # Ollama's installer auto-starts the daemon. Poll the API until it
    # responds; bail after ~30s.
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $r = Invoke-WebRequest -Uri "$OllamaUrl/api/tags" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) {
        Fail @"
Ollama daemon not reachable at $OllamaUrl after 30 seconds.

Try:
  - Open the Ollama tray icon and confirm it's running
  - Restart Ollama from the Start menu
  - Run 'ollama serve' in a separate terminal as a fallback
Then re-run this script.
"@
    }
    Pass "Ollama daemon is up"

    if (-not $NoModel) {
        Step "Pulling model: ollama pull $ModelSlug"
        Write-Host "    this downloads ~2 GB on first run, faster on re-runs"
        & ollama pull $ModelSlug
        if ($LASTEXITCODE -ne 0) {
            Fail "ollama pull failed. Check the model slug ($ModelSlug) is valid; see https://ollama.com/library"
        }
        Pass "model pulled"
    }

    return @{
        POTLUCK_INFERENCE_ENABLED = "true"
        POTLUCK_INFERENCE_BACKEND = "ollama"
        POTLUCK_MODEL_SLUG        = $ModelSlug
        POTLUCK_OLLAMA_URL        = $OllamaUrl
        POTLUCK_N_CTX             = $NCtx
    }
}

# ---------- Run the chosen path ---------------------------------------------

if ($chosenBackend -eq "llama-cpp") {
    $inferenceConfig = Install-LlamaCpp
} else {
    $inferenceConfig = Install-Ollama
}

# ---------- 4. Update .env ---------------------------------------------------

Step "Updating $EnvFile"

# Read existing .env, strip ALL inference-related keys, append fresh ones.
$inferenceKeys = @(
    "POTLUCK_INFERENCE_ENABLED",
    "POTLUCK_INFERENCE_BACKEND",
    "POTLUCK_MODEL_SLUG",
    "POTLUCK_MODEL_PATH",
    "POTLUCK_N_GPU_LAYERS",
    "POTLUCK_N_CTX",
    "POTLUCK_OLLAMA_URL"
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
    "# Backend: $chosenBackend (auto-detected from GPU vendor: $gpuVendor)"
)
foreach ($k in $inferenceConfig.Keys | Sort-Object) {
    $updated += "$k=$($inferenceConfig[$k])"
}

[System.IO.File]::WriteAllLines(
    $EnvFile, $updated, [System.Text.UTF8Encoding]::new($false)
)
Pass "wrote .env ($($inferenceConfig.Keys.Count) keys)"

# ---------- 5. Smoke test ----------------------------------------------------

if (-not $NoSmokeTest) {
    Step "Smoke-testing inference (5-token generation)"
    if ($chosenBackend -eq "llama-cpp") {
        Write-Host "    cold-loading model into VRAM, takes 10-60s..."
        $modelPath = $inferenceConfig.POTLUCK_MODEL_PATH
        $smokeScript = @"
import sys, time
from llama_cpp import Llama
t0 = time.monotonic()
try:
    llm = Llama(model_path=r'$modelPath', n_ctx=$NCtx, n_gpu_layers=$NGpuLayers, verbose=False)
except Exception as e:
    print('LOAD_ERR', e, file=sys.stderr); sys.exit(2)
load_s = time.monotonic() - t0
t1 = time.monotonic()
out = llm.create_completion(prompt='Hi', max_tokens=5, temperature=0.1)
gen_s = time.monotonic() - t1
print(f'OK load_s={load_s:.1f} gen_s={gen_s:.2f}')
"@
        & $VenvPython -c $smokeScript
        if ($LASTEXITCODE -ne 0) { Fail "llama-cpp smoke test failed" }
        Pass "llama-cpp smoke test passed"
    } else {
        # Ollama smoke: hit the API directly.
        $body = @{
            model  = $ModelSlug
            prompt = "Hi"
            stream = $false
            options = @{ num_predict = 5; temperature = 0.1 }
        } | ConvertTo-Json -Depth 5
        try {
            $t0 = Get-Date
            $r = Invoke-RestMethod -Uri "$OllamaUrl/api/generate" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 120
            $elapsed = (Get-Date) - $t0
            $tok = if ($r.eval_count) { $r.eval_count } else { "?" }
            Pass "Ollama smoke test passed (gen_s=$([math]::Round($elapsed.TotalSeconds, 1)) tokens=$tok)"
        } catch {
            Fail "Ollama smoke test failed: $($_.Exception.Message)"
        }
    }
} else {
    Warn "skipping smoke test per -NoSmokeTest"
}

# ---------- 6. Done ----------------------------------------------------------

Write-Host ""
Step "Inference setup complete"
Write-Host @"

Backend:    $chosenBackend
Model slug: $ModelSlug
$(if ($chosenBackend -eq "ollama") { "Ollama URL: $OllamaUrl" } else { "Model path: $($inferenceConfig.POTLUCK_MODEL_PATH)`nGPU layers: $NGpuLayers" })
Context:    $NCtx tokens

To activate: restart the contributor sidecar.
   - Close the Start-PotluckContributor window if open
   - Double-click it again from the Desktop
   - First job: cold-load model (15-60s). Subsequent jobs are fast.

To verify from the dev Mac after restart:
   curl -X POST http://<mac-ip>:8400/jobs -H 'Content-Type: application/json' \
     -d '{"prompt":"Hello","model_slug":"$ModelSlug","sampling":{"max_tokens":20}}'

If something looks wrong, re-run with -NoModel -NoSmokeTest after fixing
to skip the slow steps.

"@

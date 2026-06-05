"""Potluck AI — local-model inference benchmark runner.

Usage (from repo root via make bench-inference):
    apps/desktop/python/.venv/bin/python bench/run_inference.py \
        [--model bench/models/<name>.gguf] \
        [--prompts bench/prompts/inference_v1.json] \
        [--runs 3] \
        [--out bench/results/<timestamp>/] \
        [--n-ctx 8192] \
        [--verbose]

Runs N times per (model x prompt) cell. Reports median across runs.
Writes:
    bench/results/<timestamp>/inference_results.json  — full per-run data
    bench/results/<timestamp>/inference_summary.txt   — human-readable table

The script does NOT auto-update docs/inference-benchmarks.md.
Rob reviews numbers and updates the table manually.

Platform: macOS (Phase 1), Linux/CUDA (Phase 2), Windows/Vulkan (Phase 3).
n_gpu_layers defaults to -1 (full offload on Metal/CUDA). Pass --n-gpu-layers
explicitly on Linux when partial offload is needed (e.g. 70B with 11 GB VRAM).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo root on sys.path (needed for bench.adapters import)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bench.adapters.llama_cpp_runtime import LlamaCppRuntime, InferenceMetrics

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_BENCH_DIR = _REPO_ROOT / "bench"
_DEFAULT_PROMPTS = _BENCH_DIR / "prompts" / "inference_v1.json"
_DEFAULT_OUT_BASE = _BENCH_DIR / "results"
_DEFAULT_RUNS = 3
_DEFAULT_N_CTX = 8192

# Apple Silicon performance cores by chip (sysctl query is authoritative;
# this table is a fallback for chips where sysctl doesn't report correctly).
_APPLE_PERF_CORES: dict[str, int] = {
    "M1": 4, "M1 Pro": 8, "M1 Max": 8, "M1 Ultra": 16,
    "M2": 4, "M2 Pro": 6, "M2 Max": 8, "M2 Ultra": 16,
    "M3": 4, "M3 Pro": 6, "M3 Max": 12, "M3 Ultra": 24,
}


# ---------------------------------------------------------------------------
# Hardware detection helpers
# ---------------------------------------------------------------------------

def _get_mac_chip() -> str:
    """Return the Apple Silicon chip string (e.g. 'Apple M2 Pro')."""
    try:
        r = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip()
    except Exception:
        return platform.processor()


def _get_perf_core_count() -> int:
    """Return the number of performance cores on Apple Silicon."""
    try:
        r = subprocess.run(
            ["sysctl", "-n", "hw.perflevel0.logicalcpu"],
            capture_output=True, text=True, timeout=5,
        )
        v = int(r.stdout.strip())
        if v > 0:
            return v
    except Exception:
        pass
    # Fallback via chip name
    chip = _get_mac_chip()
    for key, count in _APPLE_PERF_CORES.items():
        if key in chip:
            return count
    import psutil
    return max(1, (psutil.cpu_count(logical=False) or 4) // 2)


def _get_nvidia_info() -> dict:
    """Return NVIDIA GPU name + free/total VRAM in GB, or empty dict if no GPU."""
    info: dict = {}
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = [p.strip() for p in r.stdout.strip().split(",")]
            if len(parts) >= 3:
                info["gpu_name"] = parts[0]
                try:
                    info["vram_total_gb"] = round(int(parts[1]) / 1024, 1)
                    info["vram_free_gb"] = round(int(parts[2]) / 1024, 1)
                except ValueError:
                    pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return info


def _collect_system_info() -> dict:
    """Collect hardware/OS metadata for the results header."""
    import psutil

    is_mac = platform.system() == "Darwin"
    is_apple_silicon = platform.machine() == "arm64" and is_mac

    chip = _get_mac_chip() if is_mac else platform.processor() or platform.machine()
    ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    cpu_logical = psutil.cpu_count(logical=True) or 0
    cpu_physical = psutil.cpu_count(logical=False) or 0

    if is_mac:
        os_version = platform.mac_ver()[0] or platform.release()
    else:
        # Linux: try /etc/os-release for a human-readable version
        try:
            lines = Path("/etc/os-release").read_text().splitlines()
            ver_map = {}
            for line in lines:
                if "=" in line:
                    k, _, v = line.partition("=")
                    ver_map[k.strip()] = v.strip().strip('"')
            os_version = ver_map.get("PRETTY_NAME", platform.release())
        except OSError:
            os_version = platform.release()

    nvidia = _get_nvidia_info()

    info = {
        "os": platform.system(),
        "os_version": os_version,
        "arch": platform.machine(),
        "cpu_chip": chip,
        "cpu_logical_cores": cpu_logical,
        "cpu_physical_cores": cpu_physical,
        "ram_gb": ram_gb,
        "is_apple_silicon": is_apple_silicon,
    }
    info.update(nvidia)
    return info


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

def load_prompts(path: Path) -> dict:
    """Load and validate the prompt suite JSON."""
    data = json.loads(path.read_text())
    assert data.get("version"), "Prompt file missing 'version'"
    assert data.get("prompts"), "Prompt file has no prompts"
    return data


# ---------------------------------------------------------------------------
# Per-model benchmark run
# ---------------------------------------------------------------------------

def run_model(
    model_path: Path,
    prompts_data: dict,
    *,
    n_runs: int = 3,
    n_ctx: int = _DEFAULT_N_CTX,
    n_gpu_layers: int = -1,
    n_threads: int | None = None,
    verbose: bool = False,
) -> dict:
    """Run all prompts against one model. Returns structured result dict."""
    prompts = prompts_data["prompts"]
    seed = prompts_data["seed"]
    temperature = prompts_data["temperature"]
    top_p = prompts_data["top_p"]

    model_name = model_path.name

    print(f"\n{'='*72}")
    print(f"Model: {model_name}")
    print(f"  n_ctx={n_ctx}  n_gpu_layers={n_gpu_layers}  n_threads={n_threads or 'auto'}")
    print(f"  n_runs={n_runs} per prompt")
    print(f"{'='*72}")

    # --- Load model ---
    runtime = LlamaCppRuntime(
        model_path=model_path,
        n_ctx=n_ctx,
        n_gpu_layers=n_gpu_layers,
        n_threads=n_threads,
        verbose_load=True,  # shows Metal backend activation
    )

    load_start = time.monotonic()
    try:
        load_meta = runtime.load()
    except Exception as exc:
        print(f"ERROR: Failed to load {model_name}: {exc}")
        return {
            "model": model_name,
            "model_path": str(model_path),
            "status": "load_failed",
            "error": str(exc),
            "load_meta": {},
            "per_prompt": [],
        }
    load_time = time.monotonic() - load_start
    print(f"  Loaded in {load_time:.1f}s (llama-cpp-python {load_meta['llama_cpp_version']})")

    per_prompt: list[dict] = []

    for prompt in prompts:
        pid = prompt["id"]
        category = prompt["category"]
        max_tokens = prompt["max_tokens"]
        system = prompt["system"]
        user = prompt["user"]

        print(f"\n  Prompt: {pid} ({category}, max_tokens={max_tokens})")

        runs: list[dict] = []
        for run_idx in range(n_runs):
            if verbose:
                print(f"    run {run_idx+1}/{n_runs}...", end="", flush=True)
            try:
                m = runtime.run_prompt(
                    system=system,
                    user=user,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    seed=seed,
                )
            except Exception as exc:
                print(f"\n    ERROR run {run_idx+1}: {exc}")
                runs.append({
                    "run": run_idx + 1,
                    "success": False,
                    "error": str(exc),
                })
                continue

            run_data = {
                "run": run_idx + 1,
                "success": m.success,
                "error": m.error,
                "wall_time_s": round(m.wall_time_s, 3),
                "ttft_s": round(m.ttft_s, 3),
                "prompt_eval_time_s": round(m.prompt_eval_time_s, 3),
                "generation_time_s": round(m.generation_time_s, 3),
                "prompt_tokens": m.prompt_tokens,
                "output_tokens": m.output_tokens,
                "prompt_eval_toks_per_s": round(m.prompt_eval_toks_per_s, 1),
                "gen_toks_per_s": round(m.gen_toks_per_s, 1),
                "peak_rss_bytes": m.peak_rss_bytes,
                "output_text": m.output_text if verbose else m.output_text[:200],
            }
            runs.append(run_data)

            if verbose:
                print(
                    f" gen={m.gen_toks_per_s:.1f} tok/s  "
                    f"ttft={m.ttft_s*1000:.0f}ms  "
                    f"out={m.output_tokens}tok  "
                    f"rss={m.peak_rss_bytes//(1<<20)}MB"
                )
            else:
                print(
                    f"    run {run_idx+1}: gen={m.gen_toks_per_s:.1f} tok/s  "
                    f"ttft={m.ttft_s*1000:.0f}ms  "
                    f"out={m.output_tokens}tok"
                )

        # Compute medians across successful runs
        successful = [r for r in runs if r.get("success")]
        summary: dict = {}
        if successful:
            def _med(key: str) -> float:
                vals = [r[key] for r in successful if isinstance(r.get(key), (int, float))]
                return round(statistics.median(vals), 3) if vals else 0.0

            summary = {
                "n_successful": len(successful),
                "median_gen_toks_per_s": _med("gen_toks_per_s"),
                "median_prompt_eval_toks_per_s": _med("prompt_eval_toks_per_s"),
                "median_ttft_s": _med("ttft_s"),
                "median_ttft_ms": round(_med("ttft_s") * 1000, 1),
                "median_wall_time_s": _med("wall_time_s"),
                "median_output_tokens": _med("output_tokens"),
                "median_peak_rss_bytes": int(_med("peak_rss_bytes")),
                "median_peak_rss_mb": round(_med("peak_rss_bytes") / (1 << 20), 1),
            }
            print(
                f"  -> median gen={summary['median_gen_toks_per_s']:.1f} tok/s  "
                f"prompt_eval={summary['median_prompt_eval_toks_per_s']:.1f} tok/s  "
                f"ttft={summary['median_ttft_ms']:.0f}ms  "
                f"rss={summary['median_peak_rss_mb']:.0f}MB"
            )
        else:
            summary = {"n_successful": 0, "error": "all runs failed"}
            print(f"  -> ALL RUNS FAILED for {pid}")

        per_prompt.append({
            "prompt_id": pid,
            "category": category,
            "max_tokens": max_tokens,
            "runs": runs,
            "summary": summary,
        })

    # Aggregate across all prompts
    model_summary = _aggregate_model_summary(per_prompt, model_name)

    print(f"\n  Model summary: {model_summary}")

    runtime.unload()

    return {
        "model": model_name,
        "model_path": str(model_path),
        "status": "ok",
        "load_time_s": round(load_time, 2),
        "load_meta": load_meta,
        "per_prompt": per_prompt,
        "model_summary": model_summary,
    }


def _aggregate_model_summary(per_prompt: list[dict], model_name: str) -> dict:
    """Aggregate per-prompt median stats across all prompts."""
    all_gen: list[float] = []
    all_prompt_eval: list[float] = []
    all_ttft: list[float] = []
    all_rss: list[float] = []

    for pp in per_prompt:
        s = pp.get("summary", {})
        if s.get("n_successful", 0) > 0:
            if s.get("median_gen_toks_per_s", 0) > 0:
                all_gen.append(s["median_gen_toks_per_s"])
            if s.get("median_prompt_eval_toks_per_s", 0) > 0:
                all_prompt_eval.append(s["median_prompt_eval_toks_per_s"])
            if s.get("median_ttft_s", 0) > 0:
                all_ttft.append(s["median_ttft_s"])
            if s.get("median_peak_rss_bytes", 0) > 0:
                all_rss.append(s["median_peak_rss_bytes"])

    def _med(lst: list[float]) -> float:
        return round(statistics.median(lst), 3) if lst else 0.0

    return {
        "model": model_name,
        "overall_median_gen_toks_per_s": _med(all_gen),
        "overall_median_prompt_eval_toks_per_s": _med(all_prompt_eval),
        "overall_median_ttft_ms": round(_med(all_ttft) * 1000, 1),
        "overall_median_peak_rss_mb": round(_med(all_rss) / (1 << 20), 1),
        "n_prompts_run": len(per_prompt),
        "n_prompts_ok": sum(
            1 for pp in per_prompt if pp.get("summary", {}).get("n_successful", 0) > 0
        ),
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _format_summary(results: dict) -> str:
    sys_info = results["system"]
    is_apple = sys_info.get("is_apple_silicon", False)
    gpu_name = sys_info.get("gpu_name", "")
    vram_total = sys_info.get("vram_total_gb", 0)
    n_gpu_layers = results.get("n_gpu_layers", -1)

    if is_apple:
        gpu_line = f"  GPU/Accel:    Metal (Apple Silicon) — n_gpu_layers={n_gpu_layers}"
    elif gpu_name:
        gpu_line = (
            f"  GPU/Accel:    CUDA — {gpu_name}  {vram_total} GB VRAM"
            f" — n_gpu_layers={n_gpu_layers}"
        )
    else:
        gpu_line = f"  GPU/Accel:    CPU-only — n_gpu_layers=0"

    lines = [
        "=" * 72,
        "Potluck AI — Inference Benchmark Results",
        "=" * 72,
        f"  Hardware:     {sys_info['cpu_chip']}  {sys_info['ram_gb']} GB RAM",
        f"  OS:           {sys_info['os_version']}  ({sys_info['arch']})",
        gpu_line,
        f"  llama-cpp-python: {results.get('llama_cpp_version', 'unknown')}",
        f"  Prompt suite: {results['prompt_suite']}",
        f"  Runs/cell:    {results['n_runs']}",
        f"  Date:         {results['timestamp']}",
        "",
        "RESULTS TABLE",
        "-" * 72,
        f"{'Model':<42} {'GenTok/s':>9} {'PETok/s':>9} {'TTFT(ms)':>10} {'PeakRSS(MB)':>12}",
        "-" * 72,
    ]
    for model_result in results["models"]:
        name = model_result["model"]
        # Truncate for display
        disp = name[:40] if len(name) > 40 else name
        if model_result["status"] != "ok":
            lines.append(f"  {disp:<40} {'FAILED / OOM':>42}")
            if model_result.get("error"):
                lines.append(f"    Error: {model_result['error'][:80]}")
            continue
        s = model_result.get("model_summary", {})
        lines.append(
            f"  {disp:<40} "
            f"{s.get('overall_median_gen_toks_per_s', 0):>9.1f} "
            f"{s.get('overall_median_prompt_eval_toks_per_s', 0):>9.1f} "
            f"{s.get('overall_median_ttft_ms', 0):>10.0f} "
            f"{s.get('overall_median_peak_rss_mb', 0):>12.0f}"
        )
    lines.append("-" * 72)
    lines.append("")
    lines.append("Columns:")
    lines.append("  GenTok/s  — output tokens per second (generation speed; the headline number)")
    lines.append("  PETok/s   — prompt eval tokens per second (context ingestion speed)")
    lines.append("  TTFT(ms)  — time to first token in milliseconds")
    lines.append("  PeakRSS   — peak resident set size in MB (approximate; sampled at 100ms)")
    lines.append("")
    lines.append("All values are medians across 3 runs per (model x prompt) cell.")
    lines.append(
        "Per-prompt detail: bench/results/<timestamp>/inference_results.json"
    )
    lines.append("=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Potluck inference benchmark runner")
    p.add_argument(
        "--model",
        action="append",
        dest="models",
        default=None,
        help=(
            "Path to a GGUF model file. Can be specified multiple times. "
            "If not specified, scans bench/models/ for all *.gguf files."
        ),
    )
    p.add_argument(
        "--prompts",
        default=str(_DEFAULT_PROMPTS),
        help=f"Path to prompt suite JSON (default: {_DEFAULT_PROMPTS})",
    )
    p.add_argument(
        "--runs",
        type=int,
        default=_DEFAULT_RUNS,
        help=f"Number of runs per (model x prompt) cell (default: {_DEFAULT_RUNS})",
    )
    p.add_argument(
        "--n-ctx",
        type=int,
        default=_DEFAULT_N_CTX,
        help=f"Context window size (default: {_DEFAULT_N_CTX})",
    )
    p.add_argument(
        "--n-gpu-layers",
        type=int,
        default=-1,
        help="GPU layers to offload (-1=all, 0=CPU only). Default: -1 (full Metal offload)",
    )
    p.add_argument(
        "--n-threads",
        type=int,
        default=None,
        help="CPU inference threads (default: auto-detect performance cores)",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Output directory (default: bench/results/<timestamp>/)",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print output text and per-run timing details",
    )
    return p.parse_args()


def _find_models(models_dir: Path) -> list[Path]:
    """Find all .gguf files in bench/models/, sorted by size ascending."""
    paths = sorted(models_dir.glob("*.gguf"), key=lambda p: p.stat().st_size)
    return paths


def main() -> int:
    args = parse_args()

    _sys = platform.system()
    if _sys not in ("Darwin", "Linux"):
        print(f"ERROR: Unsupported platform '{_sys}'. Supported: Darwin (Phase 1), Linux (Phase 2).")
        return 1

    # Resolve model paths
    if args.models:
        model_paths = [Path(m) for m in args.models]
    else:
        models_dir = _BENCH_DIR / "models"
        model_paths = _find_models(models_dir)
        if not model_paths:
            print(f"No .gguf files found in {models_dir}")
            print("Run: python bench/scripts/get_models.py --model all-small")
            return 1

    # Check models exist
    for mp in model_paths:
        if not mp.exists():
            print(f"ERROR: Model not found: {mp}")
            return 1

    # Load prompt suite
    prompts_path = Path(args.prompts)
    if not prompts_path.exists():
        print(f"ERROR: Prompt suite not found: {prompts_path}")
        return 1
    prompts_data = load_prompts(prompts_path)

    # Output dir
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out) if args.out else (_DEFAULT_OUT_BASE / ts)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Hardware
    sys_info = _collect_system_info()

    # Auto-detect n_threads
    n_threads = args.n_threads
    if n_threads is None:
        if sys_info["is_apple_silicon"]:
            n_threads = _get_perf_core_count()
            print(f"Auto-detected {n_threads} performance cores (Apple Silicon)")
        else:
            # Linux/x86: use physical cores, not hyperthreads
            phys = sys_info.get("cpu_physical_cores", 0) or 0
            logical = sys_info.get("cpu_logical_cores", 0) or 0
            n_threads = phys if phys > 0 else max(1, logical // 2)
            print(f"Auto-detected {n_threads} threads (physical cores: {phys}, logical: {logical})")

    phase = "Phase 1 (Mac/Metal)" if sys_info["is_apple_silicon"] else "Phase 2 (Linux/CUDA)"
    gpu_name = sys_info.get("gpu_name", "CPU-only")
    vram = sys_info.get("vram_total_gb", 0)

    print(f"Potluck Inference Benchmark — {phase}")
    print(f"  Hardware: {sys_info['cpu_chip']}  {sys_info['ram_gb']} GB RAM")
    print(f"  OS: {sys_info['os_version']}")
    print(f"  GPU: {gpu_name}" + (f"  {vram} GB VRAM" if vram else ""))
    print(f"  n_ctx={args.n_ctx}  n_gpu_layers={args.n_gpu_layers}  n_threads={n_threads}")
    print(f"  Models: {len(model_paths)}")
    print(f"  Prompts: {len(prompts_data['prompts'])} (suite: {prompts_data['version']})")
    print(f"  Runs/cell: {args.runs}")
    print(f"  Output: {out_dir}")

    wall_start = time.monotonic()

    model_results: list[dict] = []
    for model_path in model_paths:
        result = run_model(
            model_path=model_path,
            prompts_data=prompts_data,
            n_runs=args.runs,
            n_ctx=args.n_ctx,
            n_gpu_layers=args.n_gpu_layers,
            n_threads=n_threads,
            verbose=args.verbose,
        )
        model_results.append(result)

    wall_total = time.monotonic() - wall_start

    # Collect llama-cpp-python version from first successful load
    lcpp_version = "unknown"
    for r in model_results:
        v = r.get("load_meta", {}).get("llama_cpp_version", "")
        if v:
            lcpp_version = v
            break

    full_results = {
        "benchmark": "potluck-inference-v1",
        "timestamp": ts,
        "prompt_suite": prompts_data["version"],
        "n_runs": args.runs,
        "n_ctx": args.n_ctx,
        "n_gpu_layers": args.n_gpu_layers,
        "n_threads": n_threads,
        "metal_enabled": sys_info["is_apple_silicon"],
        "cuda_enabled": bool(sys_info.get("gpu_name") and not sys_info["is_apple_silicon"]),
        "llama_cpp_version": lcpp_version,
        "system": sys_info,
        "wall_time_total_s": round(wall_total, 1),
        "models": model_results,
    }

    # Write JSON
    json_path = out_dir / "inference_results.json"
    json_path.write_text(json.dumps(full_results, indent=2))
    print(f"\nResults written: {json_path}")

    # Write summary
    summary_text = _format_summary(full_results)
    summary_path = out_dir / "inference_summary.txt"
    summary_path.write_text(summary_text)
    print(f"Summary written: {summary_path}")
    print(f"Wall time: {wall_total:.0f}s")
    print()
    print(summary_text)

    return 0


if __name__ == "__main__":
    sys.exit(main())

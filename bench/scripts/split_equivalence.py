#!/usr/bin/env python3
"""Two-level equivalence test for the managed split engine (llama.cpp layer split over RPC).

Modeled on DeepInfer's examples/yoco/yoco_equiv.py: a metadata-only level that
runs in seconds, and a teacher-forced level that compares model output.

  l1     Plan metadata. Runs the planner tests (no weights, binaries or GPUs) and
         can read a GGUF header to show the layer layout and KV budget a split
         would get. Seconds.
  l2     Teacher-forced output. Runs llama.cpp's own `llama-perplexity` on a fixed
         text with `--kl-divergence-base` for the reference config, then
         `--kl-divergence` for each candidate config, and reports mean and max KL
         divergence, top-1 agreement ("Same top p") and PPL deltas. Any config can
         be the reference or the candidate (whole vs split now; old pin vs new
         pin, BF16 vs a compressed build later) because a config is just
         {bin_dir, model, args, rpc_servers}.
  build  Build the pinned llama.cpp with RPC (+Metal on macOS) and llama-perplexity.

Examples (from the repo root, python with the sidecar requirements):

  python bench/scripts/split_equivalence.py build --source ~/llama.cpp
  python bench/scripts/split_equivalence.py l1
  python bench/scripts/split_equivalence.py l2 --suite local-split \\
      --model ~/.potluck/models/qwen-2.5-0.5b-instruct-q4/model.gguf
  python bench/scripts/split_equivalence.py l2 --reference ref.json --candidate cand.json \\
      --max-mean-kld 0.001 --min-top1 99.5

A config file is JSON: {"name": "...", "bin_dir": "...", "model": "...",
"args": ["-ngl", "99"], "rpc_servers": 0, "env": {}}. Each rpc server is a local
`rpc-server` on 127.0.0.1 in the port range given by --port-base (default 18100).
Children are started and stopped by PID only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY_ROOT = ROOT / "apps" / "desktop" / "python"
sys.path.insert(0, str(PY_ROOT))

from potluck_local.services.split_layout import (  # noqa: E402
    CPU, assignment_problems, parse_assignment_log, predict_layer_assignment, summarize)

PIN = json.loads((PY_ROOT / "potluck_local" / "data" / "split-engine.json").read_text())
DEFAULT_WORK = Path(tempfile.gettempdir()) / "potluck-split-equiv"
L1_TESTS = ["tests/test_split_layout.py", "tests/test_split_memory.py", "tests/test_managed_split.py"]
PORT_LOW, PORT_HIGH = 18100, 18199

# Starting thresholds for "split matches whole" on one machine and one backend,
# set from the Mac measurement in docs/architecture/managed-split-models.md.
# The PPL check is a ratio: an absolute difference grows with PPL and with fewer
# chunks, and failed a whole-model repeat at 8 chunks (0.051 at PPL 53.9).
DEFAULTS = {"max_mean_kld": 1e-4, "max_max_kld": 1e-3, "min_top1": 99.9, "max_abs_ln_ppl_ratio": 0.005}


# ---------------------------------------------------------------- helpers

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 24):
            h.update(chunk)
    return h.hexdigest()


def machine() -> dict:
    info = {"os": platform.system(), "release": platform.release(), "arch": platform.machine(), "python": platform.python_version()}
    if platform.system() == "Darwin":
        for key, name in (("machdep.cpu.brand_string", "cpu"), ("hw.memsize", "memBytes")):
            out = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True).stdout.strip()
            info[name] = int(out) if out.isdigit() else out
    return info


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def slug(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text)


def merge_json(path: Path, key: str, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    data[key] = value
    path.write_text(json.dumps(data, indent=2) + "\n")


class Children:
    """Child processes started by this run. Stopped by PID, never by name."""

    def __init__(self):
        self.procs: list[subprocess.Popen] = []
        self.log: list[dict] = []

    def start(self, argv, role, **kw) -> subprocess.Popen:
        p = subprocess.Popen(argv, **kw)
        self.procs.append(p)
        self.log.append({"pid": p.pid, "role": role})
        return p

    def stop(self, p: subprocess.Popen):
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
        entry = next(e for e in self.log if e["pid"] == p.pid)
        entry["stopped"] = True
        entry["exit"] = p.returncode

    def stop_all(self):
        for p in self.procs:
            self.stop(p)


# ---------------------------------------------------------------- build

def cmd_build(args):
    import shutil
    import tarfile
    out = Path(args.out)
    src, build = out / "src", out / "build"
    if not (build / "bin" / "llama-perplexity").exists():
        if src.exists():
            shutil.rmtree(src)
        src.mkdir(parents=True)
        tar = subprocess.run(["git", "-C", str(args.source), "archive", PIN["commit"]], capture_output=True, check=True).stdout
        archive = out / "source.tar"
        archive.write_bytes(tar)
        with tarfile.open(archive) as t:
            t.extractall(src, filter="data")
        metal = "ON" if platform.system() == "Darwin" else "OFF"
        subprocess.run(["cmake", "-S", str(src), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
                        "-DBUILD_SHARED_LIBS=OFF", "-DGGML_BACKEND_DL=OFF", "-DGGML_RPC=ON", "-DGGML_RPC_RDMA=OFF",
                        "-DGGML_NATIVE=OFF", f"-DGGML_METAL={metal}", "-DGGML_METAL_EMBED_LIBRARY=ON", "-DGGML_BLAS=OFF",
                        "-DLLAMA_CURL=OFF", "-DLLAMA_OPENSSL=OFF", "-DLLAMA_BUILD_TESTS=OFF",
                        "-DLLAMA_BUILD_SERVER=OFF", "-DLLAMA_BUILD_EXAMPLES=ON"], check=True)
        subprocess.run(["cmake", "--build", str(build), "--config", "Release", "--target",
                        "llama-perplexity", "ggml-rpc-server", "-j", str(args.jobs)], check=True)
    print(json.dumps({"binDir": str(build / "bin"), "commit": PIN["commit"]}))


# ---------------------------------------------------------------- l1

def cmd_l1(args):
    started = time.time()
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *L1_TESTS],
                          cwd=PY_ROOT, capture_output=True, text=True)
    summary = next((ln for ln in reversed(proc.stdout.splitlines()) if re.search(r"\d+ (passed|failed)", ln)), "")
    result = {"tests": L1_TESTS, "passed": proc.returncode == 0, "summary": summary.strip("= ").strip(),
              "seconds": round(time.time() - started, 1), "models": []}
    from potluck_local.services.llama_backend import kv_cache_bytes, read_gguf_model_info
    budgets = [int(b) for b in args.budget_mib.split(",")] if args.budget_mib else []
    for model in args.model or []:
        info = read_gguf_model_info(model)
        n = info.get("block_count")
        row = {"model": str(model), "sizeBytes": Path(model).stat().st_size, "header": info,
               "kvMiB": round((kv_cache_bytes(info, args.ctx) or 0) / 2**20, 1), "ctx": args.ctx}
        if n and budgets:
            from potluck_local.services.split_layout import plan_fits
            ok, assignment, load = plan_fits(n, row["sizeBytes"] / 2**20, row["kvMiB"], budgets)
            row["plan"] = {"budgetsMiB": budgets, "fits": ok, "summary": summarize(assignment) if assignment else None,
                           "loadMiB": {str(k): round(v, 1) for k, v in load.items()}}
        result["models"].append(row)
    result["machine"] = machine()
    result["engineCommit"] = PIN["commit"]
    merge_json(Path(args.out), "l1", result)
    print(proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr[-400:])
    return 0 if result["passed"] else 1


# ---------------------------------------------------------------- l2

KLD_PATTERNS = {
    "meanKld": r"Mean\s+KLD:\s+(-?[\d.]+)",
    "maxKld": r"Maximum KLD:\s+(-?[\d.]+)",
    "p999Kld": r"99\.9%\s+KLD:\s+(-?[\d.]+)",
    "medianKld": r"Median\s+KLD:\s+(-?[\d.]+)",
    "meanDeltaP": r"Mean\s+Δp:\s+(-?[\d.]+)",
    "maxDeltaPPct": r"Maximum Δp:\s+(-?[\d.]+)",
    "rmsDeltaPPct": r"RMS Δp\s*:\s+(-?[\d.]+)",
    "top1AgreementPct": r"Same top p:\s+(-?[\d.]+)",
    "pplCandidate": r"Mean PPL\(Q\)\s+:\s+(-?[\d.]+)",
    "pplReference": r"Mean PPL\(base\)\s+:\s+(-?[\d.]+)",
    "meanLnPplRatio": r"Mean ln\(PPL\(Q\)/PPL\(base\)\)\s+:\s+(-?[\d.]+)",
}


def parse_kld(text: str) -> dict:
    out = {}
    for key, pattern in KLD_PATTERNS.items():
        m = re.search(pattern, text)
        if m:
            out[key] = float(m[1])
    if "pplCandidate" in out and "pplReference" in out:
        out["absDeltaPpl"] = round(abs(out["pplCandidate"] - out["pplReference"]), 6)
    return out


def flag_value(args: list[str], *names: str) -> str | None:
    for i, a in enumerate(args):
        if a in names and i + 1 < len(args):
            return args[i + 1]
    return None


def load_config(spec: str, defaults: dict) -> dict:
    cfg = json.loads(Path(spec).read_text())
    return {"bin_dir": defaults["bin_dir"], "model": defaults["model"], "args": [], "rpc_servers": 0, "env": {}, **cfg}


def fit_candidate_args(bin_dir: str, rpc_endpoints: list[str]) -> list[str]:
    """What Potluck's planner passes for these devices: real --list-devices output
    fed to split_memory.fit_arguments, with this machine's available RAM."""
    import psutil
    from potluck_local.services.split_memory import fit_arguments
    out = subprocess.run([f"{bin_dir}/llama-perplexity", "--rpc", ",".join(rpc_endpoints), "--list-devices"],
                         capture_output=True, text=True).stdout
    avail = psutil.virtual_memory().available
    backend = "metal" if platform.system() == "Darwin" else "cpu"
    workers = {ep.split(":")[1]: {"backend": backend, "freeMemoryBytes": avail} for ep in rpc_endpoints}
    return fit_arguments(out, workers, avail)


def suite(name: str, defaults: dict) -> tuple[dict, list[dict]]:
    base = {"bin_dir": defaults["bin_dir"], "model": defaults["model"], "env": {}}
    ref = {**base, "name": "whole", "args": ["-ngl", "99"], "rpc_servers": 0}
    if name != "local-split":
        raise SystemExit(f"unknown suite {name}")
    cands = [
        {**base, "name": "whole-repeat-noise-floor", "args": ["-ngl", "99"], "rpc_servers": 0},
        {**base, "name": "split-2way-even", "rpc_servers": 1, "args": ["--device", "MTL0,RPC0", "--fit", "off", "-ngl", "99", "-ts", "1,1"]},
        {**base, "name": "split-3way-even", "rpc_servers": 2, "args": ["--device", "MTL0,RPC0,RPC1", "--fit", "off", "-ngl", "99", "-ts", "1,1,1"]},
        {**base, "name": "split-2way-uneven-70-30", "rpc_servers": 1, "args": ["--device", "MTL0,RPC0", "--fit", "off", "-ngl", "99", "-ts", "7,3"]},
        {**base, "name": "split-remote-only", "rpc_servers": 1, "args": ["--device", "RPC0", "--fit", "off", "-ngl", "99"]},
        {**base, "name": "split-potluck-fit-args", "rpc_servers": 1, "args": ["@potluck-fit"]},
    ]
    return ref, cands


def run_perplexity(cfg: dict, text: Path, kld_file: Path, mode: str, ctx: int, chunks: int,
                   children: Children, work: Path, port_base: int, timeout: int) -> dict:
    """One llama-perplexity run. mode is 'base' (write logits) or 'compare'."""
    log = work / f"{mode}-{slug(cfg['name'])}-{slug(Path(cfg['model']).parent.name)}.log"
    servers, endpoints = [], []
    result = {"name": cfg["name"], "model": cfg["model"], "binDir": cfg["bin_dir"], "args": cfg["args"],
              "rpcServers": cfg["rpc_servers"], "log": str(log)}
    try:
        for i in range(cfg["rpc_servers"]):
            port = port_base + i
            if not (PORT_LOW <= port <= PORT_HIGH) or not port_free(port):
                raise RuntimeError(f"port {port} unavailable")
            p = children.start([f"{cfg['bin_dir']}/ggml-rpc-server", "--host", "127.0.0.1", "--port", str(port)],
                               f"rpc-server:{port}", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            servers.append(p)
            endpoints.append(f"127.0.0.1:{port}")
        for _ in range(100):  # wait for every listener
            if all(not port_free(int(e.split(":")[1])) for e in endpoints):
                break
            time.sleep(.1)
        args = list(cfg["args"])
        if "@potluck-fit" in args:
            try:
                args = fit_candidate_args(cfg["bin_dir"], endpoints)
            except Exception as exc:  # the planner refusing is a result, not a crash
                result["plannerRefusal"] = f"{type(exc).__name__}: {exc}"
                result["error"] = "planner refused"
                result["returncode"] = None
                result["placement"] = {"observed": None}
                return result
            result["resolvedArgs"] = args
        argv = [f"{cfg['bin_dir']}/llama-perplexity", "-m", cfg["model"], "-f", str(text), "-c", str(ctx),
                "--chunks", str(chunks), "--kl-divergence-base", str(kld_file), "-v"]
        if endpoints:  # --rpc must precede --device: the engine resolves names in argument order
            argv += ["--rpc", ",".join(endpoints)]
        argv += args
        if mode == "compare":
            argv.insert(argv.index("--kl-divergence-base"), "--kl-divergence")
        env = {**os.environ, **cfg.get("env", {})}
        started = time.time()
        with open(log, "wb") as fh:
            p = children.start(argv, f"llama-perplexity:{mode}:{cfg['name']}", stdout=fh, stderr=subprocess.STDOUT, env=env)
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                result["error"] = f"timeout after {timeout}s"
            children.stop(p)
        result["seconds"] = round(time.time() - started, 1)
        result["returncode"] = p.returncode
        text_out = log.read_text(errors="replace")
        result["placement"] = observed_placement(text_out, args, endpoints)
        if mode == "compare":
            result["metrics"] = parse_kld(text_out)
        m = re.search(r"Final estimate: PPL = ([\d.]+)", text_out)
        if m:
            result["ppl"] = float(m[1])
        if p.returncode not in (0,) and "error" not in result:
            result["error"] = f"exit {p.returncode}"
    finally:
        for s in servers:
            children.stop(s)
    return result


def observed_placement(log_text: str, args: list[str], endpoints: list[str]) -> dict:
    """Layer ranges per device as the engine logged them, checked against the
    layout rule when the args fix the tensor split."""
    names, head = parse_assignment_log(log_text)
    if not names:
        return {"observed": None}
    order = list(dict.fromkeys(n for n in names if n != "CPU"))
    ranges = {}
    for il, n in enumerate(names[:-1]):
        ranges.setdefault(n, [il, il])[1] = il
    indexed = [CPU if n == "CPU" else order.index(n) for n in names]
    out = {"observed": {"layers": len(names) - 1, "ranges": ranges, "outputDevice": names[head]},
           "problems": assignment_problems(indexed, len(order))}
    out["modelBufferMiB"] = {m[1]: float(m[2]) for m in re.finditer(r"load_tensors:\s+(\S+) model buffer size =\s+([\d.]+) MiB", log_text)}
    ts, ngl = flag_value(args, "-ts", "--tensor-split"), flag_value(args, "-ngl", "--gpu-layers", "--n-gpu-layers")
    dev_flag = flag_value(args, "--device", "-dev")
    if ts and len(names) > 1:
        split = [float(x) for x in ts.split(",")]
        pred = predict_layer_assignment(len(names) - 1, split, int(ngl) if ngl and ngl.lstrip("-").isdigit() else None)
        listed = dev_flag.split(",") if dev_flag else order
        pred_names = ["CPU" if d == CPU else listed[d] for d in pred]
        pred_ranges = {}
        for il, n in enumerate(pred_names[:-1]):
            pred_ranges.setdefault(n, [il, il])[1] = il
        out["predicted"] = {"ranges": pred_ranges, "outputDevice": pred_names[-1]}
        out["matchesPrediction"] = pred_names == names
    return out


def verdict(metrics: dict, th: dict) -> dict:
    checks = {
        "meanKld": metrics.get("meanKld", 1e9) <= th["max_mean_kld"],
        "maxKld": metrics.get("maxKld", 1e9) <= th["max_max_kld"],
        "top1": metrics.get("top1AgreementPct", 0) >= th["min_top1"],
        "lnPplRatio": abs(metrics.get("meanLnPplRatio", 1e9)) <= th["max_abs_ln_ppl_ratio"],
    }
    return {"pass": all(checks.values()), "checks": checks}


def cmd_l2(args):
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    bin_dir = args.bin_dir or str(DEFAULT_WORK / "build" / "bin")
    defaults = {"bin_dir": bin_dir, "model": args.model}
    th = {"max_mean_kld": args.max_mean_kld, "max_max_kld": args.max_max_kld,
          "min_top1": args.min_top1, "max_abs_ln_ppl_ratio": args.max_abs_ln_ppl_ratio}
    if args.suite:
        ref, cands = suite(args.suite, defaults)
    else:
        if not (args.reference and args.candidate):
            raise SystemExit("give --suite, or --reference and --candidate (JSON config files)")
        ref, cands = load_config(args.reference, defaults), [load_config(c, defaults) for c in args.candidate]
    for cfg in [ref, *cands]:
        for name in ("model",):
            if not cfg.get(name) or not Path(cfg[name]).is_file():
                raise SystemExit(f"{cfg.get('name')}: {name} is missing")
    text = Path(args.text) if args.text else work / "text.txt"
    if not args.text:
        text.write_bytes(default_text())
    kld = work / f"base-{slug(ref['name'])}-{slug(Path(ref['model']).parent.name)}.kld"
    children = Children()
    base_port = args.port_base
    results = []
    try:
        ref_run = run_perplexity(ref, text, kld, "base", args.ctx, args.chunks, children, work, base_port, args.timeout)
        for cfg in cands:
            run = run_perplexity(cfg, text, kld, "compare", args.ctx, args.chunks, children, work, base_port, args.timeout)
            run["verdict"] = verdict(run.get("metrics", {}), th) if "metrics" in run else {"pass": False, "checks": {}}
            results.append(run)
    finally:
        children.stop_all()
    controls = [r for r in results if r["name"].endswith("noise-floor")]
    section = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engineCommit": PIN["commit"], "machine": machine(),
        "text": {"path": str(text), "sha256": hashlib.sha256(text.read_bytes()).hexdigest(), "bytes": text.stat().st_size},
        "ctx": args.ctx, "chunks": args.chunks, "thresholds": th,
        "reference": {k: ref[k] for k in ("name", "model", "args", "rpc_servers")} | {"modelSha256": sha256_file(Path(ref["model"])), "ppl": ref_run.get("ppl"),
                                                                                     "returncode": ref_run.get("returncode"), "placement": ref_run.get("placement")},
        "candidates": results, "noiseFloor": [c["metrics"] for c in controls if "metrics" in c],
        "children": children.log,
        "allPassed": all(r["verdict"]["pass"] for r in results) and ref_run.get("returncode") == 0,
    }
    key = args.key or (Path(ref["model"]).parent.name)
    existing = Path(args.out)
    data = json.loads(existing.read_text()) if existing.exists() else {}
    data.setdefault("l2", {})[key] = section
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(json.dumps(data, indent=2) + "\n")
    for r in results:
        m = r.get("metrics", {})
        pl = r.get("placement", {})
        print(f"{key:36s} {r['name']:28s} meanKLD={m.get('meanKld')} maxKLD={m.get('maxKld')} top1={m.get('top1AgreementPct')}% "
              f"dPPL={m.get('absDeltaPpl')} pass={r['verdict']['pass']} head={((pl.get('observed') or {}).get('outputDevice'))}")
    return 0 if section["allPassed"] else 1


def default_text() -> bytes:
    """Fixed text: the repo's architecture docs, byte-stable at the pinned commit."""
    parts = sorted((ROOT / "docs" / "architecture").glob("*.md"))
    return b"\n".join(p.read_bytes() for p in parts)[:60000]


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build pinned llama.cpp + llama-perplexity + rpc-server")
    b.add_argument("--source", type=Path, required=True, help="a llama.cpp git checkout containing the pinned commit (read via git archive)")
    b.add_argument("--out", default=str(DEFAULT_WORK))
    b.add_argument("--jobs", type=int, default=4)
    b.set_defaults(fn=cmd_build)

    out_default = str(ROOT / "bench" / "results" / "split-equivalence.json")
    l1 = sub.add_parser("l1", help="plan metadata check, seconds, no weights")
    l1.add_argument("--model", action="append", help="GGUF to read the header of (never loaded)")
    l1.add_argument("--budget-mib", help="comma-separated per-device budgets for a layout preview")
    l1.add_argument("--ctx", type=int, default=4096)
    l1.add_argument("--out", default=out_default)
    l1.set_defaults(fn=cmd_l1)

    l2 = sub.add_parser("l2", help="teacher-forced KL divergence, reference vs candidates")
    l2.add_argument("--bin-dir", help="directory with llama-perplexity and ggml-rpc-server")
    l2.add_argument("--model", help="default model for configs that do not set one")
    l2.add_argument("--suite", choices=["local-split"], help="whole model vs split over local rpc-servers")
    l2.add_argument("--reference", help="reference config JSON")
    l2.add_argument("--candidate", action="append", help="candidate config JSON (repeatable)")
    l2.add_argument("--text", help="fixed text file (default: docs/architecture/*.md, first 60000 bytes)")
    l2.add_argument("--ctx", type=int, default=512)
    l2.add_argument("--chunks", type=int, default=16)
    l2.add_argument("--port-base", type=int, default=PORT_LOW)
    l2.add_argument("--timeout", type=int, default=900)
    l2.add_argument("--work", default=str(DEFAULT_WORK / "work"))
    l2.add_argument("--key", help="name for this model in the results file")
    l2.add_argument("--out", default=out_default)
    l2.add_argument("--max-mean-kld", type=float, default=DEFAULTS["max_mean_kld"])
    l2.add_argument("--max-max-kld", type=float, default=DEFAULTS["max_max_kld"])
    l2.add_argument("--min-top1", type=float, default=DEFAULTS["min_top1"], help="percent")
    l2.add_argument("--max-abs-ln-ppl-ratio", type=float, default=DEFAULTS["max_abs_ln_ppl_ratio"],
                    help="limit on |mean ln(PPL(candidate)/PPL(reference))|")
    l2.set_defaults(fn=cmd_l2)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()

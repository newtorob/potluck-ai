#!/usr/bin/env python3
"""Build the pinned household engine locally. No CI, mutable tags or pip wheels.

python scripts/build-split-engine.py --source /path/to/llama.cpp --backend metal
Output defaults to apps/desktop/python/native/split, consumed by build.py.
The source checkout is read through git archive and never modified.
"""
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / "apps/desktop/python/potluck_local/data/split-engine.json").read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--backend", choices=["metal", "cuda", "cpu"], required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "apps/desktop/python/native/split")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--cuda-host-compiler", type=Path)
    parser.add_argument("--cuda-architectures", default="75")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 32:
        parser.error("--jobs must be 1..32")
    if not all(a.isdigit() for a in args.cuda_architectures.split(";")):
        parser.error("CUDA architectures must be semicolon-separated numbers")
    if args.output.exists():
        parser.error("output already exists; choose a new directory")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="potluck-engine-build-") as directory:
        root = Path(directory)
        archive = root / "source.tar"
        with archive.open("wb") as out:
            subprocess.run(["git", "-C", str(args.source), "archive", PIN["commit"]], stdout=out, check=True)
        source = root / "source"
        source.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(source, filter="data")
        header = (source / "ggml/include/ggml-rpc.h").read_text()
        found = ".".join(re.search(rf"RPC_PROTO_{k}_VERSION\s+(\d+)", header)[1] for k in ("MAJOR", "MINOR", "PATCH"))
        if found != PIN["rpcProtocol"]:
            parser.error(f"source speaks ggml RPC {found}, pin says {PIN['rpcProtocol']}; update split-engine.json")
        build = root / "build"
        extra = []
        if args.backend == "cuda":
            extra.append("-DCMAKE_CUDA_ARCHITECTURES=" + args.cuda_architectures)
            if args.cuda_host_compiler:
                extra.append("-DCMAKE_CUDA_HOST_COMPILER=" + str(args.cuda_host_compiler))
        subprocess.run(["cmake", "-S", str(source), "-B", str(build),
            "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=OFF", "-DGGML_BACKEND_DL=OFF",
            "-DGGML_RPC=ON", "-DGGML_RPC_RDMA=OFF", "-DGGML_NATIVE=OFF",
            "-DGGML_METAL=" + ("ON" if args.backend == "metal" else "OFF"),
            "-DGGML_CUDA=" + ("ON" if args.backend == "cuda" else "OFF"),
            "-DGGML_METAL_EMBED_LIBRARY=ON", "-DGGML_BLAS=OFF",
            "-DLLAMA_OPENSSL=OFF", "-DLLAMA_BUILD_TESTS=OFF",
            # Upstream fetches prebuilt web UI assets from Hugging Face at build time
            # unless these are off. The managed engine always runs with --no-webui.
            "-DLLAMA_BUILD_UI=OFF", "-DLLAMA_USE_PREBUILT_UI=OFF",
            "-DLLAMA_BUILD_SERVER=ON", "-DLLAMA_BUILD_EXAMPLES=ON"] + extra, check=True)
        subprocess.run(["cmake", "--build", str(build), "--config", "Release", "--target",
                        "llama-server", "ggml-rpc-server", "-j", str(args.jobs)], check=True)
        staged = root / "bundle"
        staged.mkdir()
        for name in ("llama-server", "rpc-server"):
            shutil.copy2(build / "bin" / ("ggml-rpc-server" if name == "rpc-server" else name), staged / name)
        shutil.copy2(source / "LICENSE", staged / "LICENSE")
        (staged / "manifest.json").write_text(json.dumps({**PIN, "backend": args.backend}, indent=2) + "\n")
        shutil.copytree(staged, args.output)
        print(f"Built pinned {args.backend} split engine: {args.output}")


if __name__ == "__main__":
    main()

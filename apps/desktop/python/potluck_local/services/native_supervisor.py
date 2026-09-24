"""Small parent-death/heartbeat watchdog for native split processes.

The sidecar owns stdin. EOF, 45 seconds without a heartbeat, or termination
ends the native process group. This also works after the sidecar is SIGKILLed.
Only explicitly supplied command arguments are executed; no shell is involved.
"""
from __future__ import annotations

import os
import json
import re
import select
import signal
import subprocess
import sys
import threading
import time


def allocation_counter(line: str) -> dict | None:
    match = re.search(r"load_tensors:\s*(.*?)\s+model buffer size\s*=\s*(\d+(?:\.\d+)?)\s*MiB", line)
    if not match:
        return None
    label = match[1]
    rpc = re.fullmatch(r"RPC\d+\[127\.0\.0\.1:(\d+)\]", label)
    device = "RPC" if rpc else next((d for d in ("CUDA", "Metal", "CPU", "Vulkan") if label.startswith(d)), None)
    if label.startswith("MTL"):
        device = "Metal"
    if device is None:
        return None
    return {"device": device, "modelMiB": float(match[2]),
            **({"rpcPort": int(rpc[1])} if rpc else {})}


def run(argv: list[str]) -> int:
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, start_new_session=True)

    def counters():
        # Forward only allocation counters. Native prompts, tokens, paths and
        # arbitrary diagnostics never enter sidecar logs or the result surface.
        for raw in iter(child.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace")
            counter = allocation_counter(line)
            if counter:
                print(json.dumps(counter), flush=True)
    threading.Thread(target=counters, daemon=True).start()

    def stop(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        deadline = time.monotonic() + 45
        while child.poll() is None:
            ready, _, _ = select.select([sys.stdin.buffer], [], [], 1)
            if time.monotonic() >= deadline:
                break
            if ready:
                if not os.read(sys.stdin.fileno(), 1024):
                    break
                deadline = time.monotonic() + 45
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=3)
    return child.returncode or 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))

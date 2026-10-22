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
    size = float(match[2])
    # Fit probes create zero-byte dummy buffers. They are not placement and
    # must not consume the bounded collector before the actual model loads.
    if size <= 0:
        return None
    return {"device": device, "modelMiB": size,
            **({"rpcPort": int(rpc[1])} if rpc else {})}


def failure_code(line: str) -> dict | None:
    # Recognize the pinned engine's allocation failure, without forwarding any
    # native text (which can contain model paths, prompts or credentials).
    if re.search(r"(?:^|\s)ggml_backend_cuda_buffer_type_alloc_buffer:.*"
                 r"cudaMalloc failed: out of memory(?:\s|$)", line):
        return {"error": "cuda_out_of_memory"}
    return None


def run(argv: list[str]) -> int:
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, start_new_session=True)

    def counters():
        # Forward only allocation counters and allowlisted error codes. Native
        # text never enters sidecar logs or the result surface.
        for raw in iter(child.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace")
            counter = allocation_counter(line) or failure_code(line)
            if counter:
                print(json.dumps(counter), flush=True)
    reader = threading.Thread(target=counters, daemon=True)
    reader.start()

    def stop(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        (watch_threaded if sys.platform == "win32" else watch)(child)
    finally:
        if child.poll() is None:
            if sys.platform == "win32":
                # TerminateProcess; the pinned llama-server and rpc-server start no processes of their own.
                child.kill()
                child.wait(timeout=3)
            else:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=3)
        reader.join(timeout=1)
    return child.returncode or 0


def watch(child):
    deadline = time.monotonic() + 45
    while child.poll() is None:
        ready, _, _ = select.select([sys.stdin.buffer], [], [], 1)
        if time.monotonic() >= deadline:
            break
        if ready:
            if not os.read(sys.stdin.fileno(), 1024):
                break
            deadline = time.monotonic() + 45


def watch_threaded(child):
    # Windows cannot select() on a pipe, so a thread reads the heartbeat lines.
    last, eof = [time.monotonic()], threading.Event()

    def read():
        for _ in iter(sys.stdin.buffer.readline, b""):
            last[0] = time.monotonic()
        eof.set()
    threading.Thread(target=read, daemon=True).start()
    while child.poll() is None and not eof.wait(1) and time.monotonic() < last[0] + 45:
        pass


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))

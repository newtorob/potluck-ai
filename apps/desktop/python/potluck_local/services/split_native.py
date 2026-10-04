"""Pinned, packaged native processes and an OpenAI adapter for the local runtime."""
from __future__ import annotations

import importlib.resources
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time

import httpx

PIN = json.loads(importlib.resources.files("potluck_local.data").joinpath("split-engine.json").read_text())


class SplitError(RuntimeError):
    def __init__(self, detail: str, status: int = 409, *, code: str = "split_unavailable"):
        self.status, self.code = status, code
        super().__init__(detail)


def engine_bundle() -> tuple[Path, dict]:
    # Override is for source validation/builds. Shipped users need no environment
    # variables, toolchain or external llama installation.
    override = os.environ.get("POTLUCK_SPLIT_ENGINE_DIR")
    root = Path(override) if override else (
        Path(sys._MEIPASS) / "split-engine" if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[2] / "native" / "split")
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        if any(manifest.get(k) != PIN[k] for k in ("schemaVersion", "protocol", "commit")):
            raise ValueError()
        if manifest.get("backend") not in ("metal", "cuda", "cpu"):
            raise ValueError()
        for name in ("llama-server", "rpc-server"):
            path = root / name
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError()
        return root, manifest
    except (OSError, ValueError, KeyError):
        raise SplitError("This runtime has no compatible packaged split engine. Install a split-capable Potluck build.", 503, code="split_engine_missing") from None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class NativeProcess:
    def __init__(self, args: list[str], *, env: dict | None = None):
        command = ([sys.executable, "--split-supervisor"] if getattr(sys, "frozen", False)
                   else [sys.executable, "-m", "potluck_local.services.native_supervisor"])
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("LLAMA_", "GGML_RPC_"))}
        clean_env.update(env or {})
        self.placement = []
        self.failure = None
        self.process = subprocess.Popen(command + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=clean_env, start_new_session=True)
        self._lock = threading.Lock()
        def collect():
            for line in iter(self.process.stdout.readline, b""):
                try:
                    row = json.loads(line)
                    if row == {"error": "cuda_out_of_memory"}:
                        self.failure = "cuda_out_of_memory"
                    elif set(row) in ({"device", "modelMiB"}, {"device", "modelMiB", "rpcPort"}) and len(self.placement) < 32:
                        self.placement.append(row)
                except (ValueError, TypeError):
                    pass
        self._reader = threading.Thread(target=collect, daemon=True)
        self._reader.start()
        self.heartbeat()

    def heartbeat(self):
        with self._lock:
            if self.process.poll() is not None:
                self._reader.join(timeout=1)
                if self.failure == "cuda_out_of_memory":
                    raise SplitError("The head computer ran out of GPU memory. Unload other models or free GPU memory, then start the split again.", 503, code="split_gpu_memory")
                raise SplitError("The native split engine stopped. Stop the split, check available memory and start again.", 503)
            try:
                self.process.stdin.write(b".\n")
                self.process.stdin.flush()
            except (OSError, ValueError):
                raise SplitError("The native split engine stopped.", 503) from None

    def close(self):
        with self._lock:
            if self.process.poll() is not None:
                return
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)


class SplitModel:
    """Only a locally supervised llama-server can back this adapter.

    It shares the runtime's inference lock and exact GGUF identity. A dead or
    unresponsive native process raises an error, never a fabricated finish.
    """
    managed_split = True

    def __init__(self, process: NativeProcess, port: int, key: str, on_close):
        self.process, self.on_close = process, on_close
        self.base = f"http://127.0.0.1:{port}"
        self.headers = {"Authorization": f"Bearer {key}"}
        self.closed = False
        self.failure = None

    def close(self):
        if not self.closed:
            self.closed = True
            self.process.close()
            self.on_close()

    def cancel(self):
        # Kill the isolated engine to interrupt even a native RPC deadlock. A
        # cancelled split is explicitly stopped and must be started again.
        self.close()

    def failed(self, error: str):
        # A socket error after explicit cancellation must not relabel a user
        # stop as a failure. An engine error that happens first remains failed.
        if not self.closed:
            self.failure = error
            self.close()
        return SplitError(error, 502)

    def create_chat_completion(self, **kwargs):
        if self.closed or self.process.process.poll() is not None:
            raise self.failed("Split model is stopped. Start it again before inference.")
        body = {**kwargs, "cache_prompt": True}
        if kwargs.get("stream"):
            return self._stream(body)
        try:
            with httpx.Client(timeout=180, trust_env=False) as client:
                response = client.post(self.base + "/v1/chat/completions", headers=self.headers, json=body)
                result = response.json()
                if response.status_code != 200 or not isinstance(result, dict) or result.get("error") \
                        or not result.get("choices") or not all(isinstance(c, dict) and c.get("finish_reason") for c in result["choices"]):
                    raise SplitError("The split engine could not complete this request.", 502)
                return result
        except (httpx.HTTPError, ValueError, SplitError):
            raise self.failed("Split inference failed. Check worker status and start again.") from None

    def _stream(self, body):
        terminal = False
        try:
            with httpx.Client(timeout=httpx.Timeout(90, connect=3), trust_env=False) as client:
                with client.stream("POST", self.base + "/v1/chat/completions", headers=self.headers, json=body) as response:
                    if response.status_code != 200:
                        raise SplitError("The split engine rejected the request.", 502)
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            if not terminal:
                                raise SplitError("Split inference ended without a completion event.", 502)
                            return
                        chunk = json.loads(data)
                        if "error" in chunk:
                            raise SplitError("Split inference failed.", 502)
                        for choice in chunk.get("choices", []):
                            if choice.get("finish_reason") is not None:
                                terminal = True
                        yield chunk
            raise SplitError("Split inference was interrupted before completion.", 502)
        except (httpx.HTTPError, ValueError, SplitError):
            raise self.failed("Split inference was interrupted. Check worker status and start again.") from None

    def tokenize(self, text: bytes, add_bos=True, special=False):
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.post(self.base + "/tokenize", headers=self.headers,
                                   json={"content": text.decode(), "add_special": add_bos, "parse_special": special})
            response.raise_for_status()
            return response.json()["tokens"]

"""Pinned, packaged native processes and an OpenAI adapter for the local runtime."""
from __future__ import annotations

import importlib.resources
from dataclasses import dataclass, field
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
CANCEL_GRACE_SECONDS = 3.0
CANCEL_POLL_SECONDS = 0.05
CANCEL_PROBE_TIMEOUT = 0.25


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


@dataclass
class _SplitRequest:
    cancelled: threading.Event
    finished: threading.Event = field(default_factory=threading.Event)
    connection: socket.socket | None = None
    started: bool = False
    sent: bool = False
    prevented_send: bool = False
    admitted: bool = False


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
        self._state_lock = threading.Lock()
        self._request: _SplitRequest | None = None

    @staticmethod
    def _interrupt(request: _SplitRequest | None):
        # close() alone need not interrupt a blocking recv on another thread.
        # The request thread still owns closing its HTTP client and response.
        if request is not None and request.connection is not None:
            try:
                request.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _close(self, request: _SplitRequest | None = None, *, cancel: bool = True):
        with self._state_lock:
            if self.closed or (request is not None and self._request is not request):
                return
            self.closed = True
            active = self._request
            if cancel and active is not None:
                active.cancelled.set()
        self._interrupt(active)
        try:
            self.process.close()
        finally:
            self.on_close()

    def close(self):
        self._close()

    def _slot_busy(self, timeout: float) -> bool | None:
        # The pinned engine queues /slots on its model loop. /health can remain
        # healthy during a stuck decode and does not establish safe reuse.
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                response = client.get(self.base + "/slots", headers=self.headers)
                slots = response.json()
            if response.status_code == 200 and isinstance(slots, list) and len(slots) == 1 \
                    and isinstance(slots[0], dict) and type(slots[0].get("is_processing")) is bool:
                return slots[0]["is_processing"]
        except (httpx.HTTPError, ValueError):
            pass
        return None

    def cancel(self, cancel_event: threading.Event | None = None):
        if cancel_event is not None:
            cancel_event.set()
        with self._state_lock:
            request = self._request
            if self.closed or request is None or (cancel_event is not None and request.cancelled is not cancel_event):
                return
            request.cancelled.set()
            if not request.started:
                # The request checks the same event before starting HTTP work.
                # This covers an executor/generator that has not started.
                self._interrupt(request)
                return

        try:
            self._cancel_active(request)
        except Exception:  # A transport/status contract failure cannot permit reuse.
            self._close(request)

    def _cancel_active(self, request: _SplitRequest):
        deadline = time.monotonic() + CANCEL_GRACE_SECONDS
        # Headers establish admission at this exact pin. Before headers, only
        # observing its busy slot can do so: idle may precede deferred admission
        # while the native HTTP handler is still tokenizing the request.
        if request.sent and not request.admitted:
            if self._slot_busy(min(CANCEL_PROBE_TIMEOUT, CANCEL_GRACE_SECONDS)) is True:
                request.admitted = True
        self._interrupt(request)
        while time.monotonic() < deadline:
            with self._state_lock:
                if self.closed or self._request is not request:
                    return
            if request.finished.is_set() and request.prevented_send:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if request.connection is None:
                # A connect may still be underway. Its trace must prevent the
                # send; absent verified transport evidence, stop at the deadline.
                request.finished.wait(min(CANCEL_POLL_SECONDS, remaining))
                time.sleep(min(CANCEL_POLL_SECONDS, max(0, deadline - time.monotonic())))
                continue
            busy = self._slot_busy(min(CANCEL_PROBE_TIMEOUT, remaining))
            if busy is True:
                request.admitted = True
            if request.finished.is_set() and request.admitted and busy is False:
                return
            time.sleep(min(CANCEL_POLL_SECONDS, max(0, deadline - time.monotonic())))
        # Unknown admission/idle state or a hung native/RPC call requires the
        # bounded supervised shutdown. It must not affect a later request.
        self._close(request)

    def failed(self, error: str):
        # A socket error after explicit cancellation must not relabel a user
        # stop as a failure. An engine error that happens first remains failed.
        with self._state_lock:
            if not self.closed:
                self.failure = error
        self._close(cancel=False)
        return SplitError(error, 502)

    @staticmethod
    def _cancelled():
        return SplitError("Split request was cancelled.", 499, code="split_cancelled")

    def _begin(self, cancel_event: threading.Event | None) -> _SplitRequest:
        request = _SplitRequest(cancel_event if cancel_event is not None else threading.Event())
        with self._state_lock:
            if request.cancelled.is_set():
                raise self._cancelled()
            previous = self._request
            if previous is not None and not previous.finished.is_set() \
                    and not (previous.cancelled.is_set() and not previous.started):
                raise SplitError("Another split request is still running.", 409)
            self._request = request
        return request

    def _start(self, request: _SplitRequest):
        with self._state_lock:
            if request.cancelled.is_set():
                raise self._cancelled()
            request.started = True

    def _trace(self, request: _SplitRequest):
        # HTTPX's trace exposes the socket before response headers, so a slow
        # prefill can be interrupted too. If this contract changes, cancellation
        # cannot prove reuse and falls back to stopping the supervised engine.
        def trace(name, info):
            with self._state_lock:
                if name == "connection.connect_tcp.complete":
                    stream = info.get("return_value")
                    extra = getattr(stream, "get_extra_info", None)
                    connection = extra("socket") if callable(extra) else None
                    request.connection = connection if isinstance(connection, socket.socket) else None
                    if request.cancelled.is_set():
                        request.prevented_send = True
                        self._interrupt(request)
                        raise self._cancelled()
                elif name == "http11.send_request_headers.started":
                    if request.cancelled.is_set():
                        request.prevented_send = True
                        raise self._cancelled()
                    request.sent = True
                elif name == "http11.receive_response_headers.complete":
                    result = info.get("return_value")
                    if isinstance(result, tuple) and len(result) >= 2 and result[1] == 200:
                        request.admitted = True
        return trace

    def create_chat_completion(self, **kwargs):
        cancel_event = kwargs.pop("cancel_event", None)
        request = self._begin(cancel_event)
        if self.closed or self.process.process.poll() is not None:
            request.finished.set()
            raise self.failed("Split model is stopped. Start it again before inference.")
        # Not covered by prompt_guard. llama-server renders the template itself and
        # tokenizes the result with parse_special=true (server-context.cpp at the
        # pinned commit), so control-token text in these messages still becomes
        # control tokens. Closing it needs /apply-template, /tokenize and a
        # /completion call with a token array, plus the chat endpoint's reply
        # shaping and tool-call parsing rebuilt here.
        body = {**kwargs, "cache_prompt": True}
        if kwargs.get("stream"):
            return self._stream(body, request)
        try:
            self._start(request)
            with httpx.Client(timeout=httpx.Timeout(180, connect=3), trust_env=False) as client:
                response = client.post(self.base + "/v1/chat/completions", headers=self.headers, json=body,
                                       extensions={"trace": self._trace(request)})
                if request.cancelled.is_set():
                    raise self._cancelled()
                result = response.json()
                if response.status_code != 200 or not isinstance(result, dict) or result.get("error") \
                        or not result.get("choices") or not all(isinstance(c, dict) and c.get("finish_reason") for c in result["choices"]):
                    raise SplitError("The split engine could not complete this request.", 502)
                return result
        except Exception:
            if request.cancelled.is_set():
                raise self._cancelled() from None
            raise self.failed("Split inference failed. Check worker status and start again.") from None
        finally:
            request.finished.set()

    def _stream(self, body, request):
        terminal = False
        try:
            self._start(request)
            with httpx.Client(timeout=httpx.Timeout(90, connect=3), trust_env=False) as client:
                with client.stream("POST", self.base + "/v1/chat/completions", headers=self.headers, json=body,
                                   extensions={"trace": self._trace(request)}) as response:
                    if response.status_code != 200:
                        raise SplitError("The split engine rejected the request.", 502)
                    for line in response.iter_lines():
                        if request.cancelled.is_set():
                            raise self._cancelled()
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
        except Exception:
            if request.cancelled.is_set():
                raise self._cancelled() from None
            raise self.failed("Split inference was interrupted. Check worker status and start again.") from None
        finally:
            request.finished.set()

    def tokenize(self, text: bytes, add_bos=True, special=False):
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.post(self.base + "/tokenize", headers=self.headers,
                                   json={"content": text.decode(), "add_special": add_bos, "parse_special": special})
            response.raise_for_status()
            return response.json()["tokens"]

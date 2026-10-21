"""Pinned, packaged native processes and an OpenAI adapter for the local runtime."""
from __future__ import annotations

import importlib.resources
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time

import httpx

from . import prompt_cache, prompt_guard, tool_markers

logger = logging.getLogger(__name__)
PIN = json.loads(importlib.resources.files("potluck_local.data").joinpath("split-engine.json").read_text())
CANCEL_GRACE_SECONDS = 3.0
CANCEL_POLL_SECONDS = 0.05
CANCEL_PROBE_TIMEOUT = 0.25
CHAT_PATH = "/v1/chat/completions"
COMPLETION_PATH = "/v1/completions"
# Request keys that only the chat endpoint reads: they shape the template or the
# reply, and /v1/completions has no use for them. Every other key goes through.
_CHAT_ONLY = frozenset({"messages", "tools", "tool_choice", "parallel_tool_calls", "response_format",
                        "add_generation_prompt", "continue_final_message", "chat_template_kwargs",
                        "reasoning_format", "reasoning_effort", "echo", "logprobs", "top_logprobs"})
_TEMPLATE_KEYS = ("messages", "tools", "tool_choice", "parallel_tool_calls", "add_generation_prompt",
                  "continue_final_message", "chat_template_kwargs", "reasoning_format", "reasoning_effort")


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
    # Waiting on or running a slot restore: a bounded native call cancel() must not mistake for a hang.
    restoring: bool = False


class SplitModel:
    """Only a locally supervised llama-server can back this adapter.

    It shares the runtime's inference lock and exact GGUF identity. A dead or
    unresponsive native process raises an error, never a fabricated finish.
    """
    managed_split = True

    def __init__(self, process: NativeProcess, port: int, key: str, on_close, protected_tokens=None, slot_cache=None,
                 marker_tokens=None):
        # protected_tokens() lists the control-token texts of the loaded vocab, for
        # prompt_guard. None leaves every request on the server's own chat endpoint.
        # slot_cache is a prompt_cache.SplitSlotCache for a server started with
        # --slot-save-path, or None for no disk prompt cache.
        # marker_tokens() is {token id: text} of the tool-call markers the vocab holds as
        # a token (tool_markers.py), or None when the engine cannot say.
        self.process, self.on_close = process, on_close
        self._marker_tokens = marker_tokens
        self._markers_cache: dict[int, str] | None = None
        self._slot_cache = slot_cache
        self._protected_tokens = protected_tokens
        self._guard_cache: prompt_guard.PromptGuard | None | bool = False
        self.base = f"http://127.0.0.1:{port}"
        self.headers = {"Authorization": f"Bearer {key}"}
        self.closed = False
        self.failure = None
        self._state_lock = threading.Lock()
        self._request: _SplitRequest | None = None
        if slot_cache is not None:
            slot_cache.bind(self._slot_post)

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
            if self._slot_cache is not None:
                self._slot_cache.close()
            self.on_close()

    def close(self):
        self._save_live_slot()
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
            if request.restoring:
                # A slot restore ends by itself (its own HTTP timeout); stopping the
                # engine over it would throw away the state being restored.
                deadline = max(deadline, time.monotonic() + CANCEL_GRACE_SECONDS)
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

    def tool_markers(self) -> dict[int, str]:
        if self._markers_cache is None:
            try:
                self._markers_cache = dict(self._marker_tokens()) if self._marker_tokens is not None else {}
            except Exception:  # noqa: BLE001 - a vocab that cannot be read proves nothing
                self._markers_cache = {}
        return self._markers_cache

    def _guard(self):
        if self._guard_cache is False:
            texts = self._protected_tokens() if self._protected_tokens is not None else []
            if self._protected_tokens is not None and not texts:
                logger.warning("split_prompt_guard_unavailable reason=no_control_tokens_read")
            self._guard_cache = prompt_guard.PromptGuard(texts) if texts else None
        return self._guard_cache

    def _post(self, client, request: _SplitRequest, path: str, body: dict):
        if request.cancelled.is_set():
            request.prevented_send = True
            raise self._cancelled()
        response = client.post(self.base + path, headers=self.headers, json=body)
        if response.status_code != 200:
            raise SplitError("The split engine could not read this request.", 502)
        return response.json()

    def _prepare(self, request: _SplitRequest, body: dict):
        """``(path, body, ticket)`` for the request.

        llama-server renders the chat template itself and tokenizes the whole
        prompt with parse_special=true (server-context.cpp and server-common.cpp at
        the pinned commit), and no request field or flag turns that off. So when
        prompt_guard finds control-token text in the request, the prompt is built
        here from the same server's own template and tokenizer: /apply-template
        renders the sanitized messages, /tokenize turns the template text into
        tokens with special parsing on and each protected piece into plain text
        tokens, and /v1/completions gets the token ids. A request with no such
        text is returned untouched for /v1/chat/completions.
        """
        guard = self._guard()
        if guard is None:
            return CHAT_PATH, body, prompt_guard.NO_TICKET
        clean, ticket = guard.sanitize({"messages": body.get("messages"), "tools": body.get("tools")})
        if not ticket.active:
            return CHAT_PATH, body, prompt_guard.NO_TICKET
        try:
            template = {k: body[k] for k in _TEMPLATE_KEYS if k in body}
            template["messages"] = clean["messages"]
            if "tools" in template:
                template["tools"] = clean["tools"]
            # A separate client: the request's own connection is made later, under
            # its trace, so cancellation can reach it.
            with httpx.Client(timeout=httpx.Timeout(30, connect=3), trust_env=False) as client:
                text = self._post(client, request, "/apply-template", template)["prompt"]
                tokens: list[int] = []
                plain: dict[str, list[int]] = {}  # a protected text repeats; tokenize it once
                # The template text has no BOS; the server adds it on the first piece.
                for piece, protected in guard.segments(text) or [(text, False)]:
                    if not piece:
                        continue
                    if protected and tokens and piece in plain:
                        tokens += plain[piece]
                        continue
                    # A tool-call marker is cut in two so the vocab cannot match it whole.
                    halves = [piece[:len(piece) // 2], piece[len(piece) // 2:]] if protected and piece in guard.split else [piece]
                    ids = []
                    for half in halves:
                        ids += self._post(client, request, "/tokenize", {
                            "content": half, "add_special": not tokens and not ids,
                            "parse_special": not protected})["tokens"]
                    if protected and tokens:
                        plain[piece] = ids
                    tokens += ids
            completion = {k: v for k, v in body.items() if k not in _CHAT_ONLY}
            completion["prompt"] = tokens
            if body.get("tools") and self.tool_markers():
                # The server names each sampled token's id in logprobs; tool_markers reads it.
                completion["logprobs"] = 1
            schema = body.get("response_format")
            if isinstance(schema, dict) and schema.get("type") in ("json_object", "json_schema"):
                found = schema.get("schema") if schema["type"] == "json_object" else (schema.get("json_schema") or {}).get("schema")
                completion["json_schema"] = found if isinstance(found, dict) else {}
            return COMPLETION_PATH, completion, ticket
        except BaseException:
            ticket.close()
            raise

    @staticmethod
    def _chat_reply(result: dict) -> dict:
        """A /v1/completions reply as the chat.completion the chat endpoint returns."""
        choices = [{"index": c.get("index", 0), "finish_reason": c.get("finish_reason"),
                    "message": {"role": "assistant", "content": c.get("text", "")}}
                   for c in result.get("choices", []) if isinstance(c, dict)]
        return {**{k: v for k, v in result.items() if k != "choices"}, "object": "chat.completion", "choices": choices}

    # -- disk prompt cache (prompt_cache.SplitSlotCache) ------------------------------

    def _slot_post(self, path: str, body: dict) -> dict:
        with httpx.Client(timeout=httpx.Timeout(60, connect=3), trust_env=False) as client:
            response = client.post(self.base + path, headers=self.headers, json=body)
        if response.status_code != 200:
            raise SplitError("The split engine could not move a prompt state.", 502)
        return response.json()

    def _prompt_ids(self, request: _SplitRequest, path: str, body: dict) -> list[int]:
        """The token ids the server evaluates for this request.

        A request prepared by _prepare already carries them. A clean one is rendered
        and tokenized the way the server's chat endpoint does it, by the same server:
        /apply-template, then /tokenize with add_special and parse_special on (the
        pinned server-context tokenizes a rendered chat prompt with both on). The ids
        only choose which saved prefix to load, and the server matches the prompt
        itself, so a difference could cost a miss and never a wrong answer.
        """
        if path == COMPLETION_PATH:
            return list(body["prompt"])
        template = {k: body[k] for k in _TEMPLATE_KEYS if k in body}
        with httpx.Client(timeout=httpx.Timeout(30, connect=3), trust_env=False) as client:
            text = self._post(client, request, "/apply-template", template)["prompt"]
            return self._post(client, request, "/tokenize", {"content": text, "add_special": True, "parse_special": True})["tokens"]

    def _cache_begin(self, request: _SplitRequest, path: str, body: dict):
        cache = self._slot_cache
        if cache is None:
            return None
        request.restoring = True
        try:
            return cache.begin(prompt_cache.request_is_local.get(), lambda: self._prompt_ids(request, path, body))
        except Exception:  # noqa: BLE001 - never fail a request over the cache
            logger.warning("split_prompt_cache_begin_failed", exc_info=True)
            return None
        finally:
            request.restoring = False

    def _cache_finish(self, ids, completed: bool):
        if self._slot_cache is not None and not self.closed:
            try:
                self._slot_cache.finish(ids, completed)
            except Exception:  # noqa: BLE001
                logger.warning("split_prompt_cache_finish_failed", exc_info=True)

    def _save_live_slot(self):
        """On unload, model swap and shutdown: keep the live slot if local requests made it."""
        cache = self._slot_cache
        if cache is None or self.closed:
            return
        with self._state_lock:
            request = self._request
            if request is not None and not request.finished.is_set():
                return  # a request is running: the slot is mid-decode
        if self.process.process.poll() is not None:
            return
        try:
            cache.final_save()
        except Exception:  # noqa: BLE001
            logger.warning("split_prompt_cache_final_save_failed", exc_info=True)

    def create_chat_completion(self, **kwargs):
        cancel_event = kwargs.pop("cancel_event", None)
        request = self._begin(cancel_event)
        if self.closed or self.process.process.poll() is not None:
            request.finished.set()
            raise self.failed("Split model is stopped. Start it again before inference.")
        body = {**kwargs, "cache_prompt": True}
        if kwargs.get("stream"):
            return self._stream(body, request)
        ticket = prompt_guard.NO_TICKET
        ids, done = None, False
        try:
            self._start(request)
            path, body, ticket = self._prepare(request, body)
            ids = self._cache_begin(request, path, body)
            with httpx.Client(timeout=httpx.Timeout(180, connect=3), trust_env=False) as client:
                response = client.post(self.base + path, headers=self.headers, json=body,
                                       extensions={"trace": self._trace(request)})
                if request.cancelled.is_set():
                    raise self._cancelled()
                result = response.json()
                if response.status_code != 200 or not isinstance(result, dict) or result.get("error") \
                        or not result.get("choices") or not all(isinstance(c, dict) and c.get("finish_reason") for c in result["choices"]):
                    raise SplitError("The split engine could not complete this request.", 502)
                done = True
                if path == COMPLETION_PATH:
                    markers = self.tool_markers() if "logprobs" in body else {}
                    for choice in result["choices"]:
                        if markers:
                            choice["text"] = tool_markers.tag_whole(markers, choice)
                    return self._chat_reply(result)
                for choice in result["choices"]:
                    message = choice.get("message")
                    if isinstance(message, dict) and isinstance(message.get("content"), str):
                        message["content"] = tool_markers.escape(message["content"])
                return result
        except Exception:
            if request.cancelled.is_set():
                raise self._cancelled() from None
            raise self.failed("Split inference failed. Check worker status and start again.") from None
        finally:
            ticket.close()
            self._cache_finish(ids, done)
            request.finished.set()

    def _stream(self, body, request):
        terminal = False
        ticket = prompt_guard.NO_TICKET
        ids, done = None, False
        try:
            self._start(request)
            path, body, ticket = self._prepare(request, body)
            ids = self._cache_begin(request, path, body)
            with httpx.Client(timeout=httpx.Timeout(90, connect=3), trust_env=False) as client:
                with client.stream("POST", self.base + path, headers=self.headers, json=body,
                                   extensions={"trace": self._trace(request)}) as response:
                    if response.status_code != 200:
                        raise SplitError("The split engine rejected the request.", 502)
                    first = path == COMPLETION_PATH
                    for line in response.iter_lines():
                        if request.cancelled.is_set():
                            raise self._cancelled()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            if not terminal:
                                raise SplitError("Split inference ended without a completion event.", 502)
                            done = True
                            return
                        chunk = json.loads(data)
                        if "error" in chunk:
                            raise SplitError("Split inference failed.", 502)
                        for choice in chunk.get("choices", []):
                            if choice.get("finish_reason") is not None:
                                terminal = True
                        if path == COMPLETION_PATH:
                            if "logprobs" in body and self.tool_markers():
                                for choice in chunk.get("choices", []):
                                    choice["text"] = tool_markers.tag_chunk(self.tool_markers(), choice)
                            for shaped in self._chat_chunks(chunk, first):
                                yield shaped
                            first = False
                        else:
                            for choice in chunk.get("choices", []):
                                delta = choice.get("delta") if isinstance(choice, dict) else None
                                if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                                    delta["content"] = tool_markers.escape(delta["content"])
                            yield chunk
            raise SplitError("Split inference was interrupted before completion.", 502)
        except Exception:
            if request.cancelled.is_set():
                raise self._cancelled() from None
            raise self.failed("Split inference was interrupted. Check worker status and start again.") from None
        finally:
            ticket.close()
            self._cache_finish(ids, done)
            request.finished.set()

    @staticmethod
    def _chat_chunks(chunk: dict, first: bool):
        """A /v1/completions stream chunk as chat.completion.chunk objects, the way the chat endpoint streams."""
        choice = (chunk.get("choices") or [{}])[0]
        base = {k: v for k, v in chunk.items() if k not in ("choices", "usage")}
        base["object"] = "chat.completion.chunk"

        def make(delta, finish, extra=None):
            return {"choices": [{"finish_reason": finish, "index": choice.get("index", 0), "delta": delta}],
                    **base, **(extra or {})}
        if first:
            yield make({"role": "assistant", "content": None}, None)
        if choice.get("text"):
            yield make({"content": choice["text"]}, None)
        if choice.get("finish_reason") is not None:
            yield make({}, choice["finish_reason"], {"usage": chunk["usage"]} if "usage" in chunk else None)

    def tokenize(self, text: bytes, add_bos=True, special=False):
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.post(self.base + "/tokenize", headers=self.headers,
                                   json={"content": text.decode(), "add_special": add_bos, "parse_special": special})
            response.raise_for_status()
            return response.json()["tokens"]

"""One HTTP GET to the mesh sidecar over its Windows named pipe, standard library only.

On Windows the Go mesh sidecar serves its local API (Go net/http, HTTP/1.1) on
the named pipe `\\\\.\\pipe\\potluck-mesh`. Python has no HTTP client for pipes,
so this module speaks the one request the engine needs by hand: a GET with
`Connection: close`, answered with either `Content-Length` or chunked framing.

This file imports nothing outside the standard library and nothing from
potluck_local, so it can be copied to a Windows machine and run by path:

    python mesh_pipe.py

That opens the real pipe, prints `/status` and `/peers`, and exits non-zero
with a plain message when something is wrong.

Timeouts: asyncio cannot cancel a blocking read on a pipe handle, and closing
the handle from another thread to wake the reader is not something to rely on.
So this code never blocks on a read. Before each read it asks Windows how many
bytes are waiting (PeekNamedPipe) and sleeps in short steps until some arrive
or the deadline passes. The thread that owns the handle therefore always gets
back to its own `finally` and closes the handle itself.
"""

from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Callable, Optional, Tuple

PIPE_NAME = r"\\.\pipe\potluck-mesh"

MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_LINE_BYTES = 4096
MAX_PATH_CHARS = 2048
DEFAULT_TIMEOUT_S = 2.0

_READ_CHUNK = 64 * 1024
_MAX_TRAILER_LINES = 32

ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_PIPE_BUSY = 231
_PIPE_ENDED = (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED)


class MeshPipeError(Exception):
    """The request could not be completed or the answer was not valid HTTP."""


def _describe(exc: BaseException) -> str:
    code = getattr(exc, "winerror", None)
    return type(exc).__name__ + (" (winerror %s)" % code if code else "")


# ---- request ---------------------------------------------------------------

_PATH_OK = re.compile(r"/[\x21-\x7e]*")


def validate_path(path: Any) -> str:
    """Return `path` if it is safe to put on a request line, else raise.

    It must start with "/" and hold only printable ASCII with no space, so it
    cannot end the request line or start another header or request.
    """
    if not isinstance(path, str) or len(path) > MAX_PATH_CHARS or not _PATH_OK.fullmatch(path):
        raise MeshPipeError("request path must start with / and hold only printable ASCII, no spaces")
    return path


def _request_bytes(path: str) -> bytes:
    return (
        "GET " + path + " HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Accept: application/json\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")


# ---- response parsing ------------------------------------------------------

_STATUS_LINE = re.compile(r"HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?")
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_DIGITS = re.compile(r"[0-9]{1,12}")
_CHUNK_SIZE = re.compile(rb"[0-9a-fA-F]{1,8}")


class _Reader:
    """Buffered reads from a stream that stop at a deadline."""

    def __init__(self, stream: Any, deadline: float) -> None:
        self.stream = stream
        self.deadline = deadline
        self.buf = bytearray()

    def fill(self) -> bool:
        """Read more into the buffer. False means the other side closed."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MeshPipeError("timed out waiting for the mesh sidecar")
        wait = getattr(self.stream, "wait_readable", None)
        if wait is not None and not wait(remaining):
            raise MeshPipeError("timed out waiting for the mesh sidecar")
        data = self.stream.read(_READ_CHUNK)
        if not data:
            return False
        self.buf += data
        return True

    def read_line(self) -> bytes:
        """One CRLF-terminated line, without the CRLF."""
        while True:
            end = self.buf.find(b"\r\n")
            if end >= 0:
                line = bytes(self.buf[:end])
                del self.buf[: end + 2]
                return line
            if len(self.buf) > MAX_LINE_BYTES:
                raise MeshPipeError("response line is too long")
            if not self.fill():
                raise MeshPipeError("connection closed in the middle of the response")

    def read_exact(self, count: int) -> bytes:
        while len(self.buf) < count:
            if not self.fill():
                raise MeshPipeError("connection closed before the whole body arrived")
        data = bytes(self.buf[:count])
        del self.buf[:count]
        return data


def _read_head(reader: _Reader) -> Tuple[int, dict]:
    while True:
        end = reader.buf.find(b"\r\n\r\n")
        if end >= 0:
            break
        if len(reader.buf) > MAX_HEADER_BYTES:
            raise MeshPipeError("response headers are too large")
        if not reader.fill():
            raise MeshPipeError("connection closed before the response headers ended")
    if end > MAX_HEADER_BYTES:
        raise MeshPipeError("response headers are too large")
    head = bytes(reader.buf[:end])
    del reader.buf[: end + 4]
    try:
        lines = head.decode("ascii").split("\r\n")
    except UnicodeDecodeError:
        raise MeshPipeError("response headers are not ASCII") from None

    match = _STATUS_LINE.fullmatch(lines[0])
    if not match:
        raise MeshPipeError("bad HTTP status line")
    status = int(match.group(1))

    headers: dict = {}
    for line in lines[1:]:
        if "\n" in line or "\r" in line or line[:1] in (" ", "\t"):
            raise MeshPipeError("bad HTTP header line")
        name, colon, value = line.partition(":")
        if not colon or not _HEADER_NAME.fullmatch(name):
            raise MeshPipeError("bad HTTP header line")
        headers.setdefault(name.lower(), []).append(value.strip(" \t"))
    return status, headers


def _read_chunked(reader: _Reader) -> bytes:
    body = bytearray()
    while True:
        size_text = reader.read_line().split(b";", 1)[0].strip(b" \t")
        if not _CHUNK_SIZE.fullmatch(size_text):
            raise MeshPipeError("bad chunk size in the response")
        size = int(size_text, 16)
        if size == 0:
            break
        if len(body) + size > MAX_BODY_BYTES:
            raise MeshPipeError("response body is too large")
        body += reader.read_exact(size)
        if reader.read_exact(2) != b"\r\n":
            raise MeshPipeError("bad chunk ending in the response")
    for _ in range(_MAX_TRAILER_LINES):
        if not reader.read_line():
            return bytes(body)
    raise MeshPipeError("too many trailer lines in the response")


def _read_body(reader: _Reader, status: int, headers: dict) -> bytes:
    if status < 200:
        raise MeshPipeError("unexpected interim HTTP response")
    if status in (204, 304):
        return b""
    encodings = headers.get("transfer-encoding")
    lengths = headers.get("content-length")
    if encodings:
        if lengths:
            raise MeshPipeError("response has both Transfer-Encoding and Content-Length")
        if len(encodings) != 1 or encodings[0].lower() != "chunked":
            raise MeshPipeError("unsupported Transfer-Encoding in the response")
        return _read_chunked(reader)
    if lengths:
        if len(set(lengths)) != 1 or not _DIGITS.fullmatch(lengths[0]):
            raise MeshPipeError("bad Content-Length in the response")
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            raise MeshPipeError("response body is too large")
        return reader.read_exact(length)
    # No framing: the body runs until the sidecar closes the connection.
    while reader.fill():
        if len(reader.buf) > MAX_BODY_BYTES:
            raise MeshPipeError("response body is too large")
    return bytes(reader.buf)


def _write_all(stream: Any, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if written is None:
            return
        if written <= 0:
            raise MeshPipeError("could not write the request")
        view = view[written:]


def http_get(
    open_stream: Callable[[float], Any], path: str, *, timeout: float = DEFAULT_TIMEOUT_S
) -> Tuple[int, bytes]:
    """GET `path` over a stream and return (status_code, body_bytes).

    `open_stream(deadline)` returns a duplex binary stream with read(n),
    write(data) and close(); `deadline` is a time.monotonic() value. If the
    stream also has wait_readable(seconds) -> bool, it is asked before every
    read, so a silent peer ends the call at the deadline instead of blocking.
    A stream without it must time out its own reads. The stream is always
    closed here, by the calling thread.

    Any status code is returned as is; the caller decides what a 404 means.
    Everything else that can go wrong raises MeshPipeError.
    """
    validate_path(path)
    deadline = time.monotonic() + timeout
    stream = None
    try:
        stream = open_stream(deadline)
        _write_all(stream, _request_bytes(path))
        reader = _Reader(stream, deadline)
        status, headers = _read_head(reader)
        return status, _read_body(reader, status, headers)
    except MeshPipeError:
        raise
    except TimeoutError:
        raise MeshPipeError("timed out waiting for the mesh sidecar") from None
    except (OSError, ValueError) as exc:
        raise MeshPipeError("mesh pipe I/O failed: " + _describe(exc)) from exc
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


# ---- the Windows pipe ------------------------------------------------------

_PIPE_NAME_OK = re.compile(r"\\\\\.\\pipe\\[^\\/:*?\"<>|\x00-\x1f]+", re.IGNORECASE)


def _check_pipe_name(name: str) -> None:
    # open() would also accept any file path, so only a plain pipe name goes through.
    if not isinstance(name, str) or not _PIPE_NAME_OK.fullmatch(name) or not name.rsplit("\\", 1)[1].strip("."):
        raise MeshPipeError(r"pipe name must look like \\.\pipe\name")


def _windows_peek(raw: Any) -> Callable[[], int]:
    """Return a function that says how many bytes are waiting on `raw`."""
    try:
        import _winapi  # built into CPython on Windows
        import msvcrt

        handle = msvcrt.get_osfhandle(raw.fileno())
    except (ImportError, OSError, ValueError) as exc:
        raise MeshPipeError("cannot poll the mesh pipe on this Python: " + type(exc).__name__) from exc

    def peek() -> int:
        return _winapi.PeekNamedPipe(handle, 0)[0]

    return peek


class PipeStream:
    """The opened pipe: read, write, close, and a read that can wait with a limit."""

    def __init__(self, raw: Any, peek: Optional[Callable[[], int]] = None, poll: bool = True) -> None:
        self._raw = raw
        self._peek = peek
        self._poll = poll  # False only for the --blocking diagnostic: plain reads that can hang

    def wait_readable(self, timeout: float) -> bool:
        """True when a read would return now (data waiting, or the sidecar closed)."""
        if not self._poll:
            return True
        if self._peek is None:
            self._peek = _windows_peek(self._raw)
        end = time.monotonic() + timeout
        delay = 0.0005
        while True:
            try:
                if self._peek() > 0:
                    return True
            except OSError as exc:
                if isinstance(exc, BrokenPipeError) or getattr(exc, "winerror", None) in _PIPE_ENDED:
                    return True  # nothing left to wait for; the next read reports the end
                raise
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.02)

    def read(self, count: int) -> bytes:
        return self._raw.read(count)

    def write(self, data: Any) -> Optional[int]:
        return self._raw.write(data)

    def close(self) -> None:
        self._raw.close()


def open_pipe(
    deadline: float,
    name: str = PIPE_NAME,
    *,
    opener: Callable[..., Any] = open,
    poll: bool = True,
) -> PipeStream:
    """Open the sidecar's pipe, waiting out ERROR_PIPE_BUSY until `deadline`.

    All pipe instances are busy for a moment while the sidecar sets up the
    next one, which Windows reports as winerror 231. A missing pipe means the
    sidecar is not running.
    """
    _check_pipe_name(name)
    delay = 0.005
    while True:
        try:
            raw = opener(name, "r+b", buffering=0)
            return PipeStream(raw, poll=poll)
        except FileNotFoundError:
            raise MeshPipeError("mesh sidecar is not running (pipe " + name + " not found)") from None
        except OSError as exc:
            if getattr(exc, "winerror", None) != ERROR_PIPE_BUSY:
                raise MeshPipeError("cannot open the mesh pipe: " + _describe(exc)) from exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MeshPipeError("mesh pipe stayed busy until the deadline")
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.1)


def pipe_get(
    path: str, *, timeout: float = DEFAULT_TIMEOUT_S, name: str = PIPE_NAME
) -> Tuple[int, bytes]:
    """GET `path` from the sidecar's Windows pipe. Blocking; run it in a worker thread."""
    return http_get(lambda deadline: open_pipe(deadline, name), path, timeout=timeout)


# ---- real-machine check: python mesh_pipe.py -------------------------------

def _summary(status_body: Optional[bytes], peers_body: Optional[bytes]) -> list:
    lines = []
    try:
        info = json.loads(status_body) if status_body is not None else None
        if isinstance(info, dict):
            lines.append("this machine: mesh_status=%s mesh_ip=%s" % (info.get("mesh_status"), info.get("mesh_ip")))
    except ValueError:
        pass
    try:
        peers = json.loads(peers_body) if peers_body is not None else None
        if isinstance(peers, dict) and isinstance(peers.get("peers"), list):
            listed = peers["peers"]
            reachable = sum(1 for p in listed if isinstance(p, dict) and p.get("reachable"))
            lines.append("peers: %d listed, %d reachable" % (len(listed), reachable))
    except ValueError:
        pass
    return lines


def check(
    open_stream: Callable[[float], Any],
    label: str,
    paths: Tuple[str, ...] = ("/status", "/peers"),
    timeout: float = 5.0,
    out: Any = None,
) -> int:
    """Fetch each path, print status line and JSON, and return a process exit code."""
    out = out if out is not None else sys.stdout
    print("Potluck mesh pipe check", file=out)
    print("  endpoint: " + label, file=out)
    print("  python:   %s on %s" % (sys.version.split()[0], sys.platform), file=out)
    failures = 0
    bodies = {}
    for path in paths:
        print("", file=out)
        started = time.monotonic()
        try:
            status, body = http_get(open_stream, path, timeout=timeout)
        except MeshPipeError as exc:
            failures += 1
            print("GET %s -> FAILED: %s" % (path, exc), file=out)
            continue
        took_ms = int((time.monotonic() - started) * 1000)
        note = "" if status == 200 else " (expected 200)"
        print("GET %s -> HTTP %d%s, %d bytes, %d ms" % (path, status, note, len(body), took_ms), file=out)
        try:
            parsed = json.loads(body)
        except ValueError:
            failures += 1
            print("  body is not JSON: " + repr(body[:200]), file=out)
            continue
        print(json.dumps(parsed, indent=2, sort_keys=True), file=out)  # ASCII, safe on any console
        if status == 200:
            bodies[path] = body
        else:
            failures += 1
    print("", file=out)
    if failures:
        print("FAIL: %d of %d requests did not work." % (failures, len(paths)), file=out)
        return 1
    print("OK: read %s over %s." % (" and ".join(paths), label), file=out)
    for line in _summary(bodies.get("/status"), bodies.get("/peers")):
        print("  " + line, file=out)
    return 0


def main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Read the Potluck mesh sidecar's local API over its Windows named pipe.")
    parser.add_argument("paths", nargs="*", default=["/status", "/peers"], help="GET paths (default: /status /peers)")
    parser.add_argument("--pipe", default=PIPE_NAME, help="pipe name (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=5.0, help="seconds per request (default: %(default)s)")
    parser.add_argument(
        "--blocking", action="store_true",
        help="read without polling, to tell a polling problem from a pipe problem (can hang: press Ctrl-C)",
    )
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("This check opens a Windows named pipe, so it only works on Windows (this is %s)." % sys.platform)
        return 2
    try:
        _check_pipe_name(args.pipe)
        for path in args.paths:
            validate_path(path)
    except MeshPipeError as exc:
        print("FAIL: " + str(exc))
        return 2
    return check(
        lambda deadline: open_pipe(deadline, args.pipe, poll=not args.blocking),
        args.pipe, tuple(args.paths), args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())

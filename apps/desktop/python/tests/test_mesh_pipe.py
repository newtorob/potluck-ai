"""The Windows named-pipe transport for the mesh sidecar, and mesh_get's Windows branch.

A real named pipe cannot be opened off Windows, so these tests cover everything
around that one open() call: request building, response framing (Content-Length
and chunked, as Go's net/http sends them), size and time limits, the busy-pipe
retry, and how mesh_get maps every failure to MeshUnavailable. Framing is also
tested against a real HTTP server on a unix stream socket, which is the same
byte stream the pipe carries.
"""
from __future__ import annotations

import ast
import asyncio
import json
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from potluck_local.services import mesh_ipc, mesh_pipe
from potluck_local.services.mesh_pipe import MeshPipeError

MODULE_PATH = Path(mesh_pipe.__file__)
HAS_UNIX_SOCKETS = hasattr(socket, "AF_UNIX") and sys.platform != "win32"
needs_unix = pytest.mark.skipif(not HAS_UNIX_SOCKETS, reason="needs unix stream sockets")

CRLF = b"\r\n"
# Headers as Go's net/http sends them for the sidecar (captured from a real run).
GO_CL_HEAD = (
    b"HTTP/1.1 %s\r\nContent-Type: application/json\r\nX-Potluck-Protocol-Version: 1\r\n"
    b"Date: Thu, 08 Oct 2026 19:37:52 GMT\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
)
GO_CHUNKED_HEAD = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Potluck-Protocol-Version: 1\r\n"
    b"Date: Thu, 08 Oct 2026 19:37:52 GMT\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n"
)
GO_204 = (
    b"HTTP/1.1 204 No Content\r\nX-Potluck-Protocol-Version: 1\r\n"
    b"Date: Thu, 08 Oct 2026 19:37:52 GMT\r\nConnection: close\r\n\r\n"
)
# The /stream handler's real output: several flushed chunks, then the 0 chunk.
GO_STREAM = (
    GO_CHUNKED_HEAD
    + b'b\r\n{"parts": [\r\n8\r\n{"i": 0}\r\n9\r\n,{"i": 1}\r\n9\r\n,{"i": 2}\r\n'
    + b'9\r\n,{"i": 3}\r\n9\r\n,{"i": 4}\r\n3\r\n]}\n\r\n0\r\n\r\n'
)


def cl_response(body: bytes, status: bytes = b"200 OK") -> bytes:
    return GO_CL_HEAD % (status, len(body)) + body


def chunk(data: bytes) -> bytes:
    return b"%x\r\n" % len(data) + data + CRLF


def pieces(data: bytes, size: int):
    return [data[i : i + size] for i in range(0, len(data), size)]


class FakeStream:
    """Feeds canned reads. It can raise on read or write, and records what was written."""

    def __init__(self, chunks=(), *, read_error=None, write_error=None, write_size=None, close_error=None):
        self.chunks = [bytes(c) for c in chunks]
        self.read_error = read_error
        self.write_error = write_error
        self.write_size = write_size
        self.close_error = close_error
        self.written = bytearray()
        self.closed = False

    def read(self, count):
        if self.read_error is not None:
            raise self.read_error
        if not self.chunks:
            return b""
        head = self.chunks[0]
        if len(head) > count:
            self.chunks[0] = head[count:]
            return head[:count]
        return self.chunks.pop(0)

    def write(self, data):
        if self.write_error is not None:
            raise self.write_error
        take = len(data) if self.write_size is None else min(self.write_size, len(data))
        self.written += bytes(data[:take])
        return take

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def get(stream_or_chunks, path="/status", timeout=2.0):
    stream = stream_or_chunks if isinstance(stream_or_chunks, FakeStream) else FakeStream(stream_or_chunks)
    result = mesh_pipe.http_get(lambda deadline: stream, path, timeout=timeout)
    assert stream.closed
    return result


def fails(chunks, fragment, **kwargs):
    stream = chunks if isinstance(chunks, FakeStream) else FakeStream(chunks)
    with pytest.raises(MeshPipeError, match=fragment):
        mesh_pipe.http_get(lambda deadline: stream, "/status", timeout=kwargs.get("timeout", 2.0))
    assert stream.closed


# ---- the module stands alone ------------------------------------------------

def test_module_imports_only_the_standard_library():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            imported.add((node.module or "").split(".")[0])
    assert imported
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


def test_module_runs_by_path_without_the_package():
    # -I: no PYTHONPATH, no user site, no current directory on sys.path.
    done = subprocess.run(
        [sys.executable, "-I", str(MODULE_PATH), "--help"], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    assert "named pipe" in done.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="the off-Windows message")
def test_main_says_plainly_it_needs_windows():
    done = subprocess.run([sys.executable, "-I", str(MODULE_PATH)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 2
    assert "only works on Windows" in done.stdout


# ---- path validation --------------------------------------------------------

@pytest.mark.parametrize("path", ["/status", "/peers", "/", "/a/b?x=1&y=2", "/a%20b", "/~x-._"])
def test_good_paths(path):
    assert mesh_pipe.validate_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "", "status", " /status", "/status ", "/a b", "/a\tb", "/a\nb", "/a\rb", "/a\r\nHost: evil",
        "/a\r\n\r\nGET /pair HTTP/1.1", "/a\x00b", "/a\x1bb", "/a\x7fb", "/caf\u00e9", "/a\u2028b",
        "//\n", "/" + "a" * 3000, None, 5, b"/status",
    ],
)
def test_bad_paths_are_refused_before_anything_is_opened(path):
    opened = []
    with pytest.raises(MeshPipeError):
        mesh_pipe.http_get(lambda deadline: opened.append(deadline), path)
    assert opened == []


# ---- the request ------------------------------------------------------------

def test_request_is_exactly_one_plain_get():
    stream = FakeStream([cl_response(b"{}")])
    get(stream, "/peers?x=1")
    assert bytes(stream.written) == (
        b"GET /peers?x=1 HTTP/1.1\r\nHost: localhost\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
    )


def test_partial_writes_are_finished():
    stream = FakeStream([cl_response(b"{}")], write_size=7)
    get(stream)
    assert bytes(stream.written).endswith(b"Connection: close\r\n\r\n")
    assert bytes(stream.written).startswith(b"GET /status HTTP/1.1\r\n")


def test_a_write_that_makes_no_progress_fails():
    fails(FakeStream([b""], write_size=0), "could not write")


# ---- good responses, as Go sends them --------------------------------------

def test_content_length_body():
    body = b'{"mesh_status": "running"}\n'
    assert get([cl_response(body)]) == (200, body)


def test_body_in_many_small_reads_including_one_byte_reads():
    body = json.dumps({"peers": [{"i": i} for i in range(200)]}, indent=2).encode()
    assert get(pieces(cl_response(body), 1)) == (200, body)
    assert get(pieces(cl_response(body), 37)) == (200, body)


def test_body_arriving_in_the_same_read_as_the_headers():
    body = b"[1, 2, 3]"
    assert get([cl_response(body)]) == (200, body)


def test_empty_content_length_body():
    assert get([GO_CL_HEAD % (b"200 OK", 0)]) == (200, b"")


def test_go_404_json_is_returned_not_raised():
    body = b'{\n  "error": {\n    "code": "not_found",\n    "message": "no such thing"\n  }\n}\n'
    assert get([cl_response(body, b"404 Not Found")]) == (404, body)


def test_go_chunked_stream():
    assert get([GO_STREAM]) == (200, b'{"parts": [{"i": 0},{"i": 1},{"i": 2},{"i": 3},{"i": 4}]}\n')
    assert get(pieces(GO_STREAM, 1))[1] == b'{"parts": [{"i": 0},{"i": 1},{"i": 2},{"i": 3},{"i": 4}]}\n'
    assert get(pieces(GO_STREAM, 5))[1] == b'{"parts": [{"i": 0},{"i": 1},{"i": 2},{"i": 3},{"i": 4}]}\n'


def test_chunked_single_big_chunk_over_64k():
    body = b"x" * 270_000
    wire = GO_CHUNKED_HEAD + chunk(body) + b"0\r\n\r\n"
    assert get(pieces(wire, 4096)) == (200, body)
    assert get([wire]) == (200, body)


def test_chunk_sizes_are_hex_and_extensions_and_trailers_are_ignored():
    wire = GO_CHUNKED_HEAD + b"A;ext=1\r\n0123456789\r\n0a\r\nabcdefghij\r\n0;done\r\nX-Trailer: 1\r\nY: 2\r\n\r\n"
    assert get([wire]) == (200, b"0123456789abcdefghij")


def test_204_has_no_body_and_does_not_wait_for_one():
    assert get([GO_204]) == (204, b"")


def test_no_framing_means_body_until_close():
    wire = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b'{"a": 1}'
    assert get(pieces(wire, 10)) == (200, b'{"a": 1}')


def test_header_names_are_case_insensitive_and_equal_duplicate_lengths_are_fine():
    wire = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nCONTENT-LENGTH: 2\r\n\r\n{}"
    assert get([wire]) == (200, b"{}")
    wire = b"HTTP/1.1 200 OK\r\ntransfer-encoding: Chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
    assert get([wire]) == (200, b"{}")


def test_status_line_without_reason_phrase():
    assert get([b"HTTP/1.1 200\r\nContent-Length: 2\r\n\r\n{}"]) == (200, b"{}")


def test_bytes_after_the_declared_length_are_ignored():
    assert get([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}EXTRA"]) == (200, b"{}")


# ---- malformed responses ----------------------------------------------------

def test_empty_answer():
    fails([], "closed before the response headers")


def test_truncated_headers():
    fails([b"HTTP/1.1 200 OK\r\nContent-Le"], "closed before the response headers")
    fails([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"], "closed before the response headers")


def test_truncated_content_length_body():
    fails([cl_response(b"0123456789")[:-4]], "before the whole body")
    fails([b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n"], "before the whole body")


@pytest.mark.parametrize(
    "tail",
    [
        b"a\r\n12345",                      # inside a chunk
        b"5\r\nhello",                      # chunk data complete, its CRLF missing
        b"5\r\nhello\r\n",                  # no terminating chunk
        b"5\r\nhello\r\n0\r\n",             # terminating chunk, no final CRLF
        b"5\r\nhel",
        b"5",                               # size line cut
    ],
)
def test_truncated_chunked_body(tail):
    fails([GO_CHUNKED_HEAD + tail], "closed")


@pytest.mark.parametrize(
    "status_line",
    [b"garbage", b"HTTP/2 200 OK", b"HTTP/1.1 20 OK", b"HTTP/1.1 2000 OK", b"HTTP/1.1 200OK",
     b"HTTP/1.1  200 OK", b"http/1.1 200 OK", b"HTTP/1.1 abc OK", b"HTTP/1.1 -20 OK", b"", b"200 OK"],
)
def test_bad_status_line(status_line):
    fails([status_line + b"\r\nContent-Length: 2\r\n\r\n{}"], "bad HTTP status line")


def test_interim_status_is_refused():
    fails([b"HTTP/1.1 100 Continue\r\n\r\n"], "interim")


@pytest.mark.parametrize(
    "header",
    [b"no colon here", b"Bad Name: x", b"Content-Length : 2", b": empty name", b" folded: x",
     b"\tfolded: x", b"Ctl\x01: x", b"X: a\nInjected: b", b"X: a\rb"],
)
def test_bad_header_lines(header):
    fails([b"HTTP/1.1 200 OK\r\n" + header + b"\r\nContent-Length: 2\r\n\r\n{}"], "bad HTTP header")


def test_non_ascii_headers():
    fails([b"HTTP/1.1 200 OK\r\nX-Name: caf\xc3\xa9\r\nContent-Length: 2\r\n\r\n{}"], "not ASCII")


@pytest.mark.parametrize("length", [b"+2", b"-2", b"1e1", b"0x2", b"2 2", b"", b"abc", b"1_0", b"\xd9\xa2", b"99999999999999"])
def test_bad_content_length(length):
    fails([b"HTTP/1.1 200 OK\r\nContent-Length: " + length + b"\r\n\r\n{}"], "Content-Length|not ASCII|too large")


def test_conflicting_content_lengths():
    fails([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n{}x"], "Content-Length")


def test_transfer_encoding_and_content_length_together():
    fails([b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n2\r\n{}\r\n0\r\n\r\n"], "both")


@pytest.mark.parametrize("coding", [b"gzip", b"gzip, chunked", b"identity", b"chunked, chunked"])
def test_unsupported_transfer_encoding(coding):
    fails([b"HTTP/1.1 200 OK\r\nTransfer-Encoding: " + coding + b"\r\n\r\n2\r\n{}\r\n0\r\n\r\n"], "Transfer-Encoding")


@pytest.mark.parametrize(
    "size_line",
    [b"zz", b"-1", b"0x10", b"+5", b"1_0", b"", b" ", b"G", b"5 5", b"\xd9\xa2", b"fffffffff", b"1" * 40],
)
def test_garbage_chunk_sizes(size_line):
    fails([GO_CHUNKED_HEAD + size_line + b"\r\nhello\r\n0\r\n\r\n"], "bad chunk size")


def test_chunk_size_beyond_the_cap_is_refused_before_reading_it():
    stream = FakeStream([GO_CHUNKED_HEAD + b"fffffff\r\nxx"])
    fails(stream, "too large")


def test_bad_chunk_ending():
    fails([GO_CHUNKED_HEAD + b"5\r\nhelloXX0\r\n\r\n"], "bad chunk ending")


def test_endless_trailers():
    fails([GO_CHUNKED_HEAD + b"0\r\n" + b"X: y\r\n" * 100 + b"\r\n"], "trailer")


# ---- size caps ---------------------------------------------------------------

def test_content_length_over_the_cap_is_refused_without_reading_it():
    stream = FakeStream([GO_CL_HEAD % (b"200 OK", mesh_pipe.MAX_BODY_BYTES + 1) + b"x" * 1000])
    fails(stream, "too large")
    # Nothing past the first read was asked for.
    assert stream.chunks == []


def test_body_exactly_at_the_cap_is_accepted():
    body = b"y" * mesh_pipe.MAX_BODY_BYTES
    status, got = get(pieces(cl_response(body), 1 << 20))
    assert (status, len(got)) == (200, mesh_pipe.MAX_BODY_BYTES)


def test_chunked_body_over_the_cap():
    half = mesh_pipe.MAX_BODY_BYTES // 2 + 10
    wire = GO_CHUNKED_HEAD + chunk(b"a" * half) + chunk(b"b" * half) + b"0\r\n\r\n"
    fails(pieces(wire, 1 << 18), "too large")


def test_unframed_body_over_the_cap():
    wire = b"HTTP/1.1 200 OK\r\n\r\n" + b"z" * (mesh_pipe.MAX_BODY_BYTES + 100)
    fails(pieces(wire, 1 << 16), "too large")


def test_header_block_over_the_cap():
    fails(pieces(b"HTTP/1.1 200 OK\r\n" + b"X-A: " + b"a" * 40_000, 4096), "headers are too large")
    # The end of headers arrives, but only after more than the cap.
    big = b"HTTP/1.1 200 OK\r\nX-A: " + b"a" * (mesh_pipe.MAX_HEADER_BYTES + 100) + b"\r\n\r\n{}"
    fails([big], "headers are too large")


def test_endless_chunk_size_line():
    fails(pieces(GO_CHUNKED_HEAD + b"5" * 20_000, 4096), "too long")


# ---- errors from the stream itself -------------------------------------------

def test_read_error_becomes_the_module_error():
    fails(FakeStream(read_error=ConnectionResetError("reset")), "ConnectionResetError")
    fails(FakeStream(read_error=ValueError("I/O operation on closed file")), "ValueError")


def test_write_error_becomes_the_module_error():
    fails(FakeStream(write_error=BrokenPipeError()), "BrokenPipeError")


def test_open_errors_become_the_module_error():
    def opener(deadline):
        raise PermissionError("denied")

    with pytest.raises(MeshPipeError, match="PermissionError"):
        mesh_pipe.http_get(opener, "/status")


def test_a_failing_close_does_not_hide_the_answer():
    assert get(FakeStream([cl_response(b"{}")], close_error=OSError("close failed"))) == (200, b"{}")


def test_timeout_error_from_a_stream_reads_as_a_timeout():
    fails(FakeStream(read_error=TimeoutError("timed out")), "timed out")


# ---- time limits -------------------------------------------------------------

class SilentStream(FakeStream):
    """Never has anything to read: wait_readable sleeps out the time it is given."""

    def __init__(self, *chunks):
        super().__init__(chunks)
        self.waits = 0

    def wait_readable(self, timeout):
        self.waits += 1
        if self.chunks:
            return True
        time.sleep(timeout)
        return False

    def read(self, count):
        assert self.chunks, "read() must not be called when nothing is waiting"
        return super().read(count)


def test_a_silent_peer_ends_at_the_deadline():
    stream = SilentStream()
    started = time.monotonic()
    fails(stream, "timed out", timeout=0.3)
    assert 0.25 <= time.monotonic() - started < 1.5
    assert stream.waits == 1


def test_a_peer_that_stalls_after_partial_headers_ends_at_the_deadline():
    stream = SilentStream(b"HTTP/1.1 200 OK\r\nContent-Le")
    started = time.monotonic()
    fails(stream, "timed out", timeout=0.3)
    assert time.monotonic() - started < 1.5


def test_a_peer_that_stalls_mid_body_ends_at_the_deadline():
    stream = SilentStream(cl_response(b"0123456789")[:-3])
    started = time.monotonic()
    fails(stream, "timed out", timeout=0.3)
    assert time.monotonic() - started < 1.5


def test_a_slow_trickle_cannot_stretch_the_deadline():
    class Trickle(FakeStream):
        def read(self, count):
            time.sleep(0.05)
            return b"a"

    started = time.monotonic()
    fails(Trickle(), "timed out|headers are too large", timeout=0.4)
    assert time.monotonic() - started < 1.5


def test_deadline_already_passed():
    stream = FakeStream([cl_response(b"{}")])
    fails(stream, "timed out", timeout=-1)


# ---- PipeStream: wait for data without a blocking read -----------------------

class FakePipe:
    """A raw pipe end. read() refuses to run when nothing is waiting, as a blocking read would hang."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = bytearray()
        self.peer_closed = False
        self.written = bytearray()
        self.closed = False
        self.peeks = 0

    def feed(self, data):
        with self.lock:
            self.data += data

    def peek(self):
        self.peeks += 1
        with self.lock:
            if self.data:
                return len(self.data)
            if self.peer_closed:
                err = BrokenPipeError(32, "broken pipe")
                err.winerror = mesh_pipe.ERROR_BROKEN_PIPE
                raise err
            return 0

    def read(self, count):
        with self.lock:
            if not self.data:
                if self.peer_closed:
                    return b""
                raise AssertionError("blocking read on an empty pipe")
            out = bytes(self.data[:count])
            del self.data[:count]
            return out

    def write(self, data):
        self.written += bytes(data)
        return len(data)

    def close(self):
        self.closed = True


def test_pipe_stream_waits_by_peeking_and_never_blocks_on_read():
    pipe = FakePipe()
    body = b'{"peers": []}\n'
    wire = cl_response(body)

    def server():
        for part in pieces(wire, 20):
            time.sleep(0.01)
            pipe.feed(part)
        time.sleep(0.01)
        pipe.peer_closed = True

    threading.Thread(target=server, daemon=True).start()
    status, got = mesh_pipe.http_get(lambda deadline: mesh_pipe.PipeStream(pipe, pipe.peek), "/peers", timeout=5)
    assert (status, got) == (200, body)
    assert pipe.closed
    assert pipe.peeks > 3
    assert bytes(pipe.written).startswith(b"GET /peers HTTP/1.1")


def test_pipe_stream_times_out_on_a_silent_pipe_without_reading():
    pipe = FakePipe()
    started = time.monotonic()
    with pytest.raises(MeshPipeError, match="timed out"):
        mesh_pipe.http_get(lambda deadline: mesh_pipe.PipeStream(pipe, pipe.peek), "/status", timeout=0.3)
    assert 0.25 <= time.monotonic() - started < 1.5
    assert pipe.closed


def test_pipe_stream_sees_a_closed_sidecar_as_the_end_of_the_stream():
    pipe = FakePipe()
    pipe.peer_closed = True
    with pytest.raises(MeshPipeError, match="closed before the response headers"):
        mesh_pipe.http_get(lambda deadline: mesh_pipe.PipeStream(pipe, pipe.peek), "/status", timeout=1)


@pytest.mark.parametrize("winerror", [109, 232, 233])
def test_pipe_ended_error_codes_count_as_readable(winerror):
    def peek():
        err = OSError(winerror, "pipe ended")
        err.winerror = winerror
        raise err

    assert mesh_pipe.PipeStream(FakePipe(), peek).wait_readable(0.5) is True


def test_other_peek_errors_are_not_swallowed():
    def peek():
        err = OSError(6, "invalid handle")
        err.winerror = 6
        raise err

    stream = mesh_pipe.PipeStream(FakePipe(), peek)
    with pytest.raises(OSError):
        stream.wait_readable(0.5)
    with pytest.raises(MeshPipeError, match="OSError"):
        mesh_pipe.http_get(lambda deadline: stream, "/status", timeout=1)


def test_blocking_mode_skips_the_peek_entirely():
    def peek():
        raise AssertionError("peek must not run in blocking mode")

    assert mesh_pipe.PipeStream(FakePipe(), peek, poll=False).wait_readable(0.1) is True
    stream = mesh_pipe.open_pipe(time.monotonic() + 1, opener=lambda *a, **k: FakePipe(), poll=False)
    assert stream.wait_readable(0.1) is True


@pytest.mark.skipif(sys.platform == "win32", reason="checks the off-Windows failure")
def test_polling_needs_windows_and_says_so():
    with pytest.raises(MeshPipeError, match="cannot poll"):
        mesh_pipe.PipeStream(FakePipe()).wait_readable(0.1)


# ---- opening the pipe --------------------------------------------------------

def busy():
    err = OSError(22, "All pipe instances are busy")
    err.winerror = mesh_pipe.ERROR_PIPE_BUSY
    return err


def test_open_pipe_uses_the_documented_open_call():
    calls = []

    def opener(*args, **kwargs):
        calls.append((args, kwargs))
        return FakePipe()

    stream = mesh_pipe.open_pipe(time.monotonic() + 1, opener=opener)
    assert isinstance(stream, mesh_pipe.PipeStream)
    assert calls == [((r"\\.\pipe\potluck-mesh", "r+b"), {"buffering": 0})]
    assert mesh_pipe.PIPE_NAME == r"\\.\pipe\potluck-mesh"


def test_open_pipe_missing_pipe_means_sidecar_not_running():
    def opener(*args, **kwargs):
        raise FileNotFoundError(2, "no such file")

    with pytest.raises(MeshPipeError, match="not running"):
        mesh_pipe.open_pipe(time.monotonic() + 1, opener=opener)


def test_open_pipe_retries_busy_then_succeeds():
    calls = []

    def opener(*args, **kwargs):
        calls.append(1)
        if len(calls) <= 2:
            raise busy()
        return FakePipe()

    assert isinstance(mesh_pipe.open_pipe(time.monotonic() + 2, opener=opener), mesh_pipe.PipeStream)
    assert len(calls) == 3


def test_open_pipe_busy_until_the_deadline():
    calls = []

    def opener(*args, **kwargs):
        calls.append(1)
        raise busy()

    started = time.monotonic()
    with pytest.raises(MeshPipeError, match="busy"):
        mesh_pipe.open_pipe(started + 0.3, opener=opener)
    assert 0.25 <= time.monotonic() - started < 1.5
    assert len(calls) >= 3


def test_open_pipe_busy_with_the_deadline_already_gone_does_not_spin():
    calls = []

    def opener(*args, **kwargs):
        calls.append(1)
        raise busy()

    with pytest.raises(MeshPipeError, match="busy"):
        mesh_pipe.open_pipe(time.monotonic() - 1, opener=opener)
    assert len(calls) == 1


def test_open_pipe_other_errors_are_not_retried():
    calls = []

    def opener(*args, **kwargs):
        calls.append(1)
        err = PermissionError(13, "access denied")
        err.winerror = 5
        raise err

    with pytest.raises(MeshPipeError, match=r"PermissionError \(winerror 5\)"):
        mesh_pipe.open_pipe(time.monotonic() + 1, opener=opener)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "name",
    [
        "/etc/passwd", "C:\\Users\\me\\file.txt", r"\\.\pipe\\", r"\\.\pipe", r"\\.\pipe\..", r"\\.\pipe\.",
        r"\\.\pipe\a\b", r"\\.\pipe\a/b", r"\\.\pipe\..\..\x", r"\\server\pipe\x", r"\\?\pipe\x",
        r"\\.\pipe\x:y", "\\\\.\\pipe\\x\x00y", "", None, r"\\.\mailslot\x", r"\\.\c:\x",
    ],
)
def test_pipe_name_must_be_a_plain_pipe_name(name):
    calls = []
    with pytest.raises(MeshPipeError):
        mesh_pipe.open_pipe(time.monotonic() + 1, name, opener=lambda *a, **k: calls.append(1))
    assert calls == []


def test_pipe_name_check_accepts_the_real_one_and_other_plain_names():
    for name in (mesh_pipe.PIPE_NAME, r"\\.\PIPE\Potluck-Mesh", r"\\.\pipe\test_1"):
        stream = mesh_pipe.open_pipe(time.monotonic() + 1, name, opener=lambda *a, **k: FakePipe())
        assert isinstance(stream, mesh_pipe.PipeStream)


def test_pipe_get_goes_through_open_pipe_with_the_default_name(monkeypatch):
    seen = []

    def fake_open_pipe(deadline, name=None, **kwargs):
        seen.append(name)
        return FakeStream([cl_response(b"{}")])

    monkeypatch.setattr(mesh_pipe, "open_pipe", fake_open_pipe)
    assert mesh_pipe.pipe_get("/status", timeout=1) == (200, b"{}")
    assert seen == [mesh_pipe.PIPE_NAME]


# ---- a real HTTP server on a unix stream socket -------------------------------

def big_peers(n=1500):
    return {
        "peers": [
            {
                "mesh_ip": "100.64.%d.%d" % (i // 250, i % 250 + 1),
                "node_name": "machine-%05d-\u00e9-\u2603" % i,
                "public_key": "A" * 44,
                "reachable": i % 2 == 0,
            }
            for i in range(n)
        ]
    }


class _Handler(socketserver.BaseRequestHandler):
    """Answers like the sidecar's Go server, byte for byte, in several writes."""

    def handle(self):
        sock = self.request
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                part = sock.recv(4096)
                if not part:
                    return
                request += part
            self.server.requests.append(request)
            target = request.split(b" ", 2)[1].decode()
            for piece, pause in self.script(target, request):
                sock.sendall(piece)
                if pause:
                    time.sleep(pause)
        except OSError:
            pass  # the client hung up early (the size-cap case)

    def script(self, target, request):
        if target == "/small":
            body = json.dumps({"mesh_ip": "100.64.0.9", "mesh_status": "running", "name": "caf\u00e9"}, indent=2, ensure_ascii=False).encode() + b"\n"
            return [(cl_response(body), 0)]
        if target == "/big":  # one big chunk, as Go does for a large writeJSON
            body = json.dumps(big_peers(), indent=2, ensure_ascii=False).encode() + b"\n"
            wire = GO_CHUNKED_HEAD + chunk(body) + b"0\r\n\r\n"
            return [(p, 0) for p in pieces(wire, 8192)]
        if target == "/big-cl":
            body = json.dumps(big_peers(), indent=2, ensure_ascii=False).encode()
            wire = cl_response(body)
            half = len(wire) // 2
            return [(wire[:half], 0.03), (wire[half:], 0)]
        if target == "/missing":
            body = b'{\n  "error": {\n    "code": "not_found",\n    "message": "no such thing"\n  }\n}\n'
            return [(cl_response(body, b"404 Not Found"), 0)]
        if target == "/boom":
            body = b'{\n  "error": {\n    "code": "boom",\n    "message": "something broke"\n  }\n}\n'
            return [(cl_response(body, b"500 Internal Server Error"), 0)]
        if target == "/stream":
            return [(p, 0.015) for p in pieces(GO_STREAM, 40)]
        if target == "/text":
            return [(cl_response(b"hello, not json\n"), 0)]
        if target == "/empty":
            return [(GO_204, 0)]
        if target == "/huge":
            wire = GO_CHUNKED_HEAD + b"".join(chunk(b"x" * 65536) for _ in range(96)) + b"0\r\n\r\n"
            return [(p, 0) for p in pieces(wire, 1 << 16)]
        if target == "/hang":
            self.server.release.wait(15)
            return []
        if target == "/echo":
            lines = request.split(b"\r\n")
            headers = {l.split(b":", 1)[0].lower(): l.split(b":", 1)[1].strip() for l in lines[1:] if l}
            body = json.dumps(
                {
                    "accept": headers[b"accept"].decode(),
                    "connection_close": headers[b"connection"] == b"close",
                    "headers": len(headers) - 1,  # Go does not count Host
                    "host": headers[b"host"].decode(),
                    "method": lines[0].split(b" ")[0].decode(),
                    "uri": target,
                },
                indent=2,
            ).encode() + b"\n"
            return [(cl_response(body), 0)]
        return [(cl_response(b"{}", b"404 Not Found"), 0)]


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 32


def unix_opener(path):
    """Open a unix socket as a stream; the socket timeout is what bounds its reads."""

    class SockStream:
        def __init__(self, deadline):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(max(deadline - time.monotonic(), 0.001))
            self.sock.connect(path)
            self.file = self.sock.makefile("rwb", buffering=0)

        def read(self, count):
            return self.file.read(count)

        def write(self, data):
            return self.file.write(data)

        def close(self):
            self.file.close()
            self.sock.close()

    return SockStream


@pytest.fixture
def unix_server():
    # macOS's pytest temp root can exceed the AF_UNIX path length limit.
    with tempfile.TemporaryDirectory(prefix="pl-pipe-", dir="/tmp") as directory:
        path = str(Path(directory) / "mesh.sock")
        server = _UnixServer(path, _Handler)
        server.requests = []
        server.release = threading.Event()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield server, path
        finally:
            server.release.set()
            server.shutdown()
            server.server_close()


def peers_ok(body):
    peers = json.loads(body)["peers"]
    assert len(peers) == 1500
    assert peers[0]["node_name"] == "machine-00000-\u00e9-\u2603"
    assert peers[-1]["mesh_ip"] == "100.64.5.250"


# Shared with the throwaway Go server check: each case is (path, status, check(body)).
REAL_SERVER_CASES = [
    ("/small", 200, lambda b: json.loads(b) == {"mesh_ip": "100.64.0.9", "mesh_status": "running", "name": "caf\u00e9"}),
    ("/big", 200, lambda b: (len(b) > 64 * 1024, peers_ok(b))[0]),
    ("/big-cl", 200, lambda b: (len(b) > 64 * 1024, peers_ok(b))[0]),
    ("/missing", 404, lambda b: json.loads(b)["error"]["code"] == "not_found"),
    ("/boom", 500, lambda b: json.loads(b)["error"]["code"] == "boom"),
    ("/stream", 200, lambda b: [p["i"] for p in json.loads(b)["parts"]] == [0, 1, 2, 3, 4]),
    ("/text", 200, lambda b: b == b"hello, not json\n"),
    ("/empty", 204, lambda b: b == b""),
    (
        "/echo", 200,
        lambda b: json.loads(b) == {
            "accept": "application/json", "connection_close": True, "headers": 2,
            "host": "localhost", "method": "GET", "uri": "/echo",
        },
    ),
]


def run_real_server_cases(open_stream, timeout=5.0):
    """Used here against a Python server and, outside the suite, against Go's net/http."""
    seen = []
    for path, want_status, check in REAL_SERVER_CASES:
        status, body = mesh_pipe.http_get(open_stream, path, timeout=timeout)
        assert status == want_status, (path, status)
        assert check(body), (path, body[:200])
        seen.append(path)
    with pytest.raises(MeshPipeError, match="too large"):
        mesh_pipe.http_get(open_stream, "/huge", timeout=timeout)
    started = time.monotonic()
    with pytest.raises(MeshPipeError, match="timed out"):
        mesh_pipe.http_get(open_stream, "/hang", timeout=0.4)
    assert time.monotonic() - started < 2
    return seen


@needs_unix
def test_every_case_against_a_real_unix_socket_server(unix_server):
    server, path = unix_server
    seen = run_real_server_cases(unix_opener(path))
    assert len(seen) == len(REAL_SERVER_CASES)
    assert all(r.startswith(b"GET /") and r.endswith(b"\r\n\r\n") for r in server.requests)


@needs_unix
def test_unix_socket_connection_failure_is_the_module_error(unix_server):
    server, path = unix_server
    with pytest.raises(MeshPipeError, match="FileNotFoundError|ConnectionRefusedError"):
        mesh_pipe.http_get(unix_opener(path + ".missing"), "/small", timeout=1)


# ---- the printed check --------------------------------------------------------

def run_check(streams):
    import io

    out = io.StringIO()
    remaining = list(streams)
    code = mesh_pipe.check(lambda deadline: remaining.pop(0), r"\\.\pipe\potluck-mesh", out=out)
    return code, out.getvalue()


def test_check_prints_status_line_and_json_for_both_paths():
    status = json.dumps({"mesh_status": "running", "mesh_ip": "100.64.0.4", "name": "caf\u00e9"}).encode()
    peers = json.dumps({"peers": [{"reachable": True}, {"reachable": False}, {"reachable": True}]}).encode()
    code, text = run_check([FakeStream([cl_response(status)]), FakeStream([cl_response(peers)])])
    assert code == 0
    assert "GET /status -> HTTP 200" in text and "GET /peers -> HTTP 200" in text
    assert '"mesh_ip": "100.64.0.4"' in text
    assert "caf\\u00e9" in text  # ASCII only, safe on a Windows console
    assert text.isascii()
    assert "OK: read /status and /peers" in text
    assert "mesh_status=running mesh_ip=100.64.0.4" in text
    assert "peers: 3 listed, 2 reachable" in text


def test_check_fails_with_a_plain_message_when_the_pipe_is_missing():
    def no_pipe(deadline):
        raise MeshPipeError("mesh sidecar is not running (pipe \\\\.\\pipe\\potluck-mesh not found)")

    import io

    out = io.StringIO()
    assert mesh_pipe.check(no_pipe, "pipe", out=out) == 1
    assert "GET /status -> FAILED: mesh sidecar is not running" in out.getvalue()
    assert "FAIL: 2 of 2 requests did not work." in out.getvalue()


def test_check_counts_a_non_200_and_non_json_as_failures():
    code, text = run_check([
        FakeStream([cl_response(b'{"error": {}}', b"500 Internal Server Error")]),
        FakeStream([cl_response(b"<html>")]),
    ])
    assert code == 1
    assert "HTTP 500 (expected 200)" in text
    assert "body is not JSON" in text
    assert "FAIL: 2 of 2" in text


# ---- mesh_get on Windows -------------------------------------------------------

@pytest.fixture
def windows(monkeypatch):
    """Pretend to be Windows, and fail the test if the unix branch is touched."""

    def unix_branch_taken(*args, **kwargs):
        raise AssertionError("the unix branch was taken")

    monkeypatch.setattr(mesh_ipc, "_is_windows", lambda: True)
    monkeypatch.setattr(mesh_ipc, "mesh_socket_path", unix_branch_taken)
    monkeypatch.setattr(mesh_ipc.httpx, "AsyncClient", unix_branch_taken)
    monkeypatch.setattr(mesh_ipc.httpx, "AsyncHTTPTransport", unix_branch_taken)
    monkeypatch.setenv("POTLUCK_LOCAL_API_SOCKET", "/tmp/not-used.sock")
    yield monkeypatch
    wait_for_idle()


def wait_for_idle(limit=5.0):
    end = time.monotonic() + limit
    while mesh_ipc._pipe_in_flight and time.monotonic() < end:
        time.sleep(0.01)
    assert mesh_ipc._pipe_in_flight == 0, "a pipe worker thread is still running"


def serve(monkeypatch, make_stream):
    """Make the pipe open call hand out streams from make_stream(); returns the call log."""
    calls = []

    def fake_open_pipe(deadline, name=None, **kwargs):
        calls.append(name)
        result = make_stream(len(calls))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(mesh_pipe, "open_pipe", fake_open_pipe)
    return calls


def mesh_get(path="/peers", timeout=2.0):
    return asyncio.run(mesh_ipc.mesh_get(path, timeout=timeout))


def unavailable(fragment, path="/peers", timeout=2.0):
    with pytest.raises(mesh_ipc.MeshUnavailable, match=fragment) as caught:
        mesh_get(path, timeout)
    return caught.value


def test_windows_success_returns_decoded_json(windows):
    payload = {"peers": [{"node_name": "caf\u00e9", "mesh_ip": "100.64.0.2", "reachable": True}]}
    body = json.dumps(payload, indent=2, ensure_ascii=False).encode()
    calls = serve(windows, lambda n: FakeStream([cl_response(body)]))
    assert mesh_get("/peers") == payload
    assert calls == [mesh_pipe.PIPE_NAME]  # the fixed name, even with POTLUCK_LOCAL_API_SOCKET set


def test_windows_success_with_chunked_list_body(windows):
    body = b"[1, 2, 3]\n"
    serve(windows, lambda n: FakeStream([GO_CHUNKED_HEAD + chunk(body) + b"0\r\n\r\n"]))
    assert mesh_get("/anything") == [1, 2, 3]


def test_windows_pipe_missing(windows):
    serve(windows, lambda n: MeshPipeError("mesh sidecar is not running (pipe x not found)"))
    unavailable("mesh sidecar is not running")


def test_windows_pipe_busy_through_the_real_open_pipe(windows):
    # Leave open_pipe real; make the open() call inside it report ERROR_PIPE_BUSY.
    attempts = []

    def fake_open(name, mode, buffering=-1):
        attempts.append(name)
        raise busy()

    windows.setattr(mesh_pipe, "open_pipe", _with_opener(fake_open))
    started = time.monotonic()
    # The worker and the caller's wait share one deadline, so either may report it first.
    unavailable("busy|timed out", timeout=0.4)
    assert time.monotonic() - started < 1.5
    assert len(attempts) >= 3
    wait_for_idle()


def _with_opener(opener):
    real = mesh_pipe.open_pipe

    def open_pipe(deadline, name=mesh_pipe.PIPE_NAME, **kwargs):
        return real(deadline, name, opener=opener)

    return open_pipe


def test_windows_pipe_busy_twice_then_free_through_the_real_open_pipe(windows):
    attempts = []
    body = b'{"ok": true}'

    class RawPipe(FakePipe):
        def __init__(self):
            super().__init__()
            self.feed(cl_response(body))

    def fake_open(name, mode, buffering=-1):
        attempts.append(name)
        if len(attempts) <= 2:
            raise busy()
        pipe = RawPipe()
        windows.setattr(mesh_pipe, "_windows_peek", lambda raw: raw.peek)  # no Windows API here
        return pipe

    windows.setattr(mesh_pipe, "open_pipe", _with_opener(fake_open))
    assert mesh_get("/status") == {"ok": True}
    assert attempts == [mesh_pipe.PIPE_NAME] * 3


def test_windows_pipe_missing_through_the_real_open_pipe(windows):
    def fake_open(name, mode, buffering=-1):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    windows.setattr(mesh_pipe, "open_pipe", _with_opener(fake_open))
    unavailable("not running")


@pytest.mark.parametrize(
    "chunks, fragment",
    [
        ([], "closed before the response headers"),
        ([b"HTTP/1.1 200 OK\r\nContent-Le"], "closed before the response headers"),
        ([cl_response(b"0123456789")[:-4]], "before the whole body"),
        ([b"HTTP/9 200 OK\r\nContent-Length: 2\r\n\r\n{}"], "bad HTTP status line"),
        ([GO_CHUNKED_HEAD + b"zz\r\nhello\r\n0\r\n\r\n"], "bad chunk size"),
        ([GO_CL_HEAD % (b"200 OK", mesh_pipe.MAX_BODY_BYTES + 1)], "too large"),
    ],
)
def test_windows_malformed_answers_are_unavailable(windows, chunks, fragment):
    serve(windows, lambda n: FakeStream(chunks))
    unavailable(fragment)


def test_windows_non_200_is_unavailable(windows):
    body = b'{"error": {"code": "not_found"}}'
    serve(windows, lambda n: FakeStream([cl_response(body, b"404 Not Found")]))
    assert "HTTP 404" in str(unavailable("answered HTTP 404"))
    serve(windows, lambda n: FakeStream([GO_204]))
    unavailable("answered HTTP 204")


@pytest.mark.parametrize("body", [b"hello, not json\n", b"", b"{", b"\xff\xfe\xfa", b"<html></html>"])
def test_windows_non_json_is_unavailable(windows, body):
    serve(windows, lambda n: FakeStream([cl_response(body)]))
    unavailable("request failed")


def test_windows_stream_errors_are_unavailable(windows):
    serve(windows, lambda n: FakeStream(read_error=ConnectionResetError("reset")))
    unavailable("ConnectionResetError")
    serve(windows, lambda n: FakeStream(write_error=BrokenPipeError()))
    unavailable("BrokenPipeError")


def test_windows_bad_path_is_unavailable_and_nothing_is_opened(windows):
    calls = serve(windows, lambda n: FakeStream([cl_response(b"{}")]))
    unavailable("path", path="/peers\r\nHost: x")
    unavailable("path", path="peers")
    assert calls == []


def test_windows_unexpected_error_in_the_worker_is_unavailable(windows):
    def boom(path, timeout):
        raise RuntimeError("something odd")

    windows.setattr(mesh_ipc, "_pipe_request", boom)
    unavailable("RuntimeError")


# -- timeouts and threads

class BlockingStream(FakeStream):
    """A read that does not come back until released, like a blocking pipe read."""

    def __init__(self, release):
        super().__init__()
        self.release = release
        self.reading = threading.Event()

    def read(self, count):
        self.reading.set()
        self.release.wait(10)
        return b""


def test_windows_silent_pipe_returns_on_time_and_the_worker_ends_by_itself(windows):
    pipe = FakePipe()
    windows.setattr(mesh_pipe, "_windows_peek", lambda raw: raw.peek)
    streams = []

    def make(n):
        stream = mesh_pipe.PipeStream(pipe)
        streams.append(stream)
        return stream

    serve(windows, make)
    started = time.monotonic()
    unavailable("timed out", timeout=0.4)
    assert 0.3 <= time.monotonic() - started < 1.5
    wait_for_idle()  # the worker ended on its own
    assert pipe.closed


def test_windows_a_stuck_read_cannot_hold_the_caller_past_the_timeout(windows):
    release = threading.Event()
    stream = BlockingStream(release)
    serve(windows, lambda n: stream)

    async def exercise():
        started = time.monotonic()
        with pytest.raises(mesh_ipc.MeshUnavailable, match="timed out") as caught:
            await mesh_ipc.mesh_get("/peers", timeout=0.4)
        elapsed = time.monotonic() - started
        assert stream.reading.is_set()
        assert stream.closed is False  # the worker still owns the handle; nobody closes it from outside
        assert mesh_ipc._pipe_in_flight == 1
        release.set()  # the blocked call finally returns (asyncio.run waits for its thread on exit)
        return elapsed, str(caught.value)

    elapsed, message = asyncio.run(exercise())
    assert 0.3 <= elapsed < 1.5, elapsed
    wait_for_idle()
    assert stream.closed  # the worker closed its own handle on the way out
    assert "timed out" in message


def test_windows_stuck_workers_are_capped_and_the_next_call_does_not_start_a_thread(windows):
    windows.setattr(mesh_ipc, "_PIPE_MAX_IN_FLIGHT", 2)
    release = threading.Event()
    streams = []

    def make(n):
        stream = BlockingStream(release)
        streams.append(stream)
        return stream

    calls = serve(windows, make)

    async def exercise():
        durations = []
        try:
            for _ in range(2):  # two calls that hang in the pipe
                with pytest.raises(mesh_ipc.MeshUnavailable, match="timed out"):
                    await mesh_ipc.mesh_get("/peers", timeout=0.2)
            assert mesh_ipc._pipe_in_flight == 2
            # The engine keeps asking every second: none of these may open a pipe or add a hung thread.
            for _ in range(5):
                started = time.monotonic()
                with pytest.raises(mesh_ipc.MeshUnavailable, match="too many"):
                    await mesh_ipc.mesh_get("/peers", timeout=2.0)
                durations.append(time.monotonic() - started)
        finally:
            release.set()
        return durations

    durations = asyncio.run(exercise())
    assert max(durations) < 0.5
    assert len(calls) == 2 and len(streams) == 2
    wait_for_idle()
    assert all(s.closed for s in streams)
    # Once the hung calls have returned, service resumes.
    serve(windows, lambda n: FakeStream([cl_response(b'{"ok": 1}')]))
    assert mesh_get("/status") == {"ok": 1}


def test_windows_cancelling_the_caller_does_not_leak_the_slot(windows):
    release = threading.Event()
    stream = BlockingStream(release)
    serve(windows, lambda n: stream)

    async def exercise():
        task = asyncio.ensure_future(mesh_ipc.mesh_get("/peers", timeout=5.0))
        try:
            while not stream.reading.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(exercise())
    wait_for_idle()
    assert stream.closed


def test_windows_concurrent_callers_do_not_interfere(windows):
    bodies = {f"/p{i}": json.dumps({"i": i}).encode() for i in range(6)}

    # Each path is answered separately; check the answers reach the right callers.
    def fake_pipe_get(path, *, timeout, name=mesh_pipe.PIPE_NAME):
        time.sleep(0.05)
        return 200, bodies[path]

    windows.setattr(mesh_pipe, "pipe_get", fake_pipe_get)

    async def exercise():
        return await asyncio.gather(*(mesh_ipc.mesh_get(p, timeout=2) for p in bodies))

    assert asyncio.run(exercise()) == [{"i": i} for i in range(6)]


@needs_unix
def test_windows_branch_end_to_end_over_a_real_http_server(windows, unix_server):
    # The same code path as the pipe, with a unix socket standing in for it.
    server, path = unix_server
    opener = unix_opener(path)

    def open_pipe(deadline, name=mesh_pipe.PIPE_NAME, **kwargs):
        return opener(deadline)

    windows.setattr(mesh_pipe, "open_pipe", open_pipe)
    assert mesh_get("/small")["mesh_status"] == "running"
    peers_ok(json.dumps(mesh_get("/big")))
    peers_ok(json.dumps(mesh_get("/big-cl")))
    assert mesh_get("/stream") == {"parts": [{"i": i} for i in range(5)]}
    unavailable("HTTP 404", path="/missing")
    unavailable("HTTP 500", path="/boom")
    unavailable("request failed", path="/text")
    unavailable("too large", path="/huge", timeout=5)
    unavailable("timed out", path="/hang", timeout=0.4)


# ---- the platform check, and the other platforms are left alone ---------------

def test_is_windows_follows_the_same_rule_as_mesh_socket_path(monkeypatch):
    monkeypatch.setattr(mesh_ipc.sys, "platform", "win32")
    monkeypatch.setattr(mesh_ipc.platform, "system", lambda: "Windows")
    assert mesh_ipc._is_windows() is True
    monkeypatch.setattr(mesh_ipc.sys, "platform", "linux")
    assert mesh_ipc._is_windows() is True  # platform.system() alone is enough, as in gateway_routing
    monkeypatch.setattr(mesh_ipc.platform, "system", lambda: "Linux")
    assert mesh_ipc._is_windows() is False
    monkeypatch.setattr(mesh_ipc.sys, "platform", "darwin")
    monkeypatch.setattr(mesh_ipc.platform, "system", lambda: "Darwin")
    assert mesh_ipc._is_windows() is False


def test_off_windows_never_touches_the_pipe(monkeypatch):
    def pipe_touched(*args, **kwargs):
        raise AssertionError("the pipe was used off Windows")

    monkeypatch.setattr(mesh_ipc, "_is_windows", lambda: False)
    monkeypatch.setattr(mesh_pipe, "pipe_get", pipe_touched)
    monkeypatch.setattr(mesh_pipe, "open_pipe", pipe_touched)
    monkeypatch.setattr(mesh_ipc, "mesh_socket_path", lambda: None)
    with pytest.raises(mesh_ipc.MeshUnavailable, match="no local mesh socket on this platform"):
        mesh_get()
    monkeypatch.setattr(mesh_ipc, "mesh_socket_path", lambda: "/tmp/definitely-missing-potluck-mesh.sock")
    with pytest.raises(mesh_ipc.MeshUnavailable, match="not running"):
        mesh_get()


@needs_unix
def test_off_windows_still_reads_over_the_unix_socket(monkeypatch, unix_server):
    server, path = unix_server
    monkeypatch.setattr(mesh_ipc, "_is_windows", lambda: False)
    monkeypatch.setattr(mesh_ipc, "mesh_socket_path", lambda: path)
    assert mesh_get("/small")["mesh_ip"] == "100.64.0.9"
    peers_ok(json.dumps(mesh_get("/big")))
    with pytest.raises(mesh_ipc.MeshUnavailable, match="HTTP 404"):
        mesh_get("/missing")

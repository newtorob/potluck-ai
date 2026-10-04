"""Private discovery and single-instance ownership for POSIX runtime launchers.

Hold the lock for the server lifetime; bind before publishing credentials.
Windows retains the Electron lifecycle until a private ACL adapter is available.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import stat
import tempfile
import uuid
from pathlib import Path

from potluck_local import __version__
from potluck_local.utils.paths import data_dir


@contextlib.contextmanager
def cleanup_on_sigterm():
    """Let context managers unwind when Uvicorn replays a handled SIGTERM.

    Uvicorn restores the original handler after graceful ASGI shutdown, then
    re-raises the signal. The OS default exits without running __exit__, leaving
    the private discovery record behind. Preserve custom handlers, and convert
    only the default termination into an ordinary Python exit while we own it.
    """
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(_signum, _frame):
        raise SystemExit(128 + signal.SIGTERM)

    if previous == signal.SIG_DFL:
        signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        if previous == signal.SIG_DFL:
            signal.signal(signal.SIGTERM, previous)


class RuntimeControl:
    def __init__(self, *, port: int, host: str, token: str, owner: str) -> None:
        self.directory = data_dir() / "run"
        self.path = self.directory / "runtime.json"
        self.port, self.host, self.token = port, host, token
        if owner not in {"cli", "desktop", "standalone"}:
            raise ValueError("Invalid runtime owner")
        self.owner = owner
        self.instance_id = str(uuid.uuid4())
        self._lock: int | None = None
        self.socket: socket.socket | None = None

    def info(self) -> dict[str, object]:
        address = "[::1]" if ":" in self.host else "127.0.0.1"
        return {"schemaVersion": 1, "instanceId": self.instance_id, "pid": os.getpid(),
                "url": f"http://{address}:{self.port}", "owner": self.owner,
                "version": __version__}

    def __enter__(self) -> RuntimeControl:
        if os.name == "nt":
            raise RuntimeError("Shared runtime control requires the Windows ACL adapter")
        if not re.fullmatch(r"[a-f0-9]{64}", self.token):
            raise ValueError("Shared runtime requires a strong control token")
        if self.host not in {"0.0.0.0", "127.0.0.1", "::", "::1"}:
            raise ValueError("Shared runtime must listen on loopback or all interfaces")
        import fcntl

        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError("Unsafe runtime directory")
        self.directory.chmod(0o700)
        try:
            self._lock = os.open(self.directory / "runtime.lock",
                                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            lock_info = os.fstat(self._lock)
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                    or lock_info.st_mode & 0o077):
                raise PermissionError("Unsafe runtime lock")
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("A runtime already owns this data directory") from exc
            self.socket = socket.socket(socket.AF_INET6 if ":" in self.host else socket.AF_INET)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.port = self.socket.getsockname()[1]
            self.socket.listen(128)
            self.socket.setblocking(False)
            fd, temporary = tempfile.mkstemp(prefix="runtime-", dir=self.directory)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump({**self.info(), "token": self.token}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args: object) -> None:
        with contextlib.suppress(OSError, ValueError, AttributeError):
            if json.loads(self.path.read_text()).get("instanceId") == self.instance_id:
                self.path.unlink()
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None

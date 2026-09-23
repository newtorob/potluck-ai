"""Resumable model downloader with SHA256 verification.

Downloads GGUF files using HTTP Range requests so interrupted downloads
resume without re-fetching bytes already on disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1024 * 1024  # 1 MiB


@dataclass
class DownloadProgress:
    model_id: str
    bytes_downloaded: int
    bytes_total: int
    speed_bps: float
    eta_seconds: float
    status: str  # "downloading" | "verifying" | "complete" | "error" | "cancelled"
    error: str | None = None


@dataclass
class _EMA:
    """Exponential moving average for speed smoothing."""
    alpha: float = 0.2
    value: float = 0.0

    def update(self, sample: float) -> float:
        if self.value == 0.0:
            self.value = sample
        else:
            self.value = self.alpha * sample + (1 - self.alpha) * self.value
        return self.value


class ModelDownloader:
    """Handles resumable GGUF model downloads with integrity verification."""

    def __init__(self, models_dir: Path) -> None:
        self.models_dir = models_dir
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._progress: dict[str, DownloadProgress] = {}
        self._cancel_flags: dict[str, bool] = {}
        self._in_flight: set[str] = set()
        self._flight_lock = asyncio.Lock()

    async def download_model(
        self,
        model_id: str,
        url: str,
        expected_sha256: str,
        expected_size: int | None = None,
    ) -> Path:
        """Download a model with resume support. Returns path to the .gguf file."""
        async with self._flight_lock:
            if model_id in self._in_flight:
                raise RuntimeError(f"Download already in progress for {model_id}")
            self._in_flight.add(model_id)

        try:
            return await self._do_download(model_id, url, expected_sha256, expected_size)
        finally:
            async with self._flight_lock:
                self._in_flight.discard(model_id)

    async def _do_download(
        self,
        model_id: str,
        url: str,
        expected_sha256: str,
        expected_size: int | None,
    ) -> Path:
        model_dir = self.models_dir / model_id
        model_dir.mkdir(parents=True, exist_ok=True)
        file_path = model_dir / "model.gguf"
        partial_path = model_dir / "model.gguf.partial"
        meta_path = model_dir / "model.gguf.partial.meta"
        hash_sidecar = model_dir / "model.gguf.sha256"

        self._cancel_flags[model_id] = False

        # Only resume a partial that belongs to THIS (url, expected_sha256). A
        # leftover partial from an earlier attempt with a different source (e.g.
        # the catalog url or quant changed for this slug) would otherwise be
        # appended to and only fail the sha256 check at the very end, with no
        # hint why. Discard it and start clean instead.
        if partial_path.exists() and not _partial_meta_matches(meta_path, url, expected_sha256):
            logger.info(
                "download_partial_discarded model_id=%s reason=url_or_hash_changed", model_id
            )
            partial_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)

        existing_bytes = partial_path.stat().st_size if partial_path.exists() else 0
        if existing_bytes == 0:
            _write_partial_meta(meta_path, url, expected_sha256)

        # Seed progress immediately so poll-during-connection-setup returns real state.
        self._progress[model_id] = DownloadProgress(
            model_id=model_id,
            bytes_downloaded=existing_bytes,
            bytes_total=expected_size or 0,
            speed_bps=0.0,
            eta_seconds=0.0,
            status="downloading",
        )
        logger.info("download_start model_id=%s url=%s", model_id, url)

        # Range offsets must refer to the bytes we store, not a compressed
        # representation that HTTPX would transparently decode.
        headers: dict[str, str] = {"Accept-Encoding": "identity"}
        if existing_bytes > 0:
            headers["Range"] = f"bytes={existing_bytes}-"
            logger.info("download_resume model_id=%s offset=%d", model_id, existing_bytes)

        ema = _EMA()
        last_window_time = time.monotonic()
        last_window_bytes = existing_bytes
        downloaded = existing_bytes
        total = expected_size or 0
        unknown_range_total = False

        try:
            async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
                # A rejected resume gets one full-download retry. Do not touch
                # the partial until that replacement response is validated.
                for attempt in range(2):
                    async with client.stream("GET", url, headers=headers) as response:
                        offset = existing_bytes if "Range" in headers else 0
                        if response.status_code == 416 and offset > 0:
                            match = re.fullmatch(
                                r"bytes \*/([0-9]+)",
                                response.headers.get("content-range", "").strip(), re.I,
                            )
                            if match and int(match[1]) == offset and expected_sha256:
                                # A previous attempt may have received the last
                                # byte before it stopped. Never trust size alone:
                                # fall through to the normal SHA256 verification.
                                total = downloaded = offset
                                break
                            if attempt == 0:
                                headers.pop("Range")
                                continue
                        response.raise_for_status()
                        response_size, response_total = _response_lengths(response, offset)
                        unknown_range_total = (
                            response.status_code == 206 and response_total is None
                        )
                        if unknown_range_total and not expected_sha256:
                            raise ValueError(
                                "Cannot verify a partial response with unknown total size and no SHA256"
                            )
                        if self._cancel_flags.get(model_id):
                            self._progress[model_id] = DownloadProgress(
                                model_id, downloaded, total, 0.0, 0.0, "cancelled",
                            )
                            return partial_path

                        # A 200 response ignored Range and contains the whole
                        # file. Truncate and reset counters before writing it.
                        if response.status_code == 200:
                            offset = 0
                        downloaded = offset
                        last_window_bytes = offset
                        last_window_time = time.monotonic()
                        total = (
                            response_total if response_total is not None else (expected_size or 0)
                        )
                        if (
                            expected_size is not None
                            and response_total is not None
                            and total != expected_size
                        ):
                            logger.warning(
                                "download_size_mismatch model_id=%s expected=%d reported=%d",
                                model_id, expected_size, total,
                            )
                        self._progress[model_id] = DownloadProgress(
                            model_id, downloaded, total, 0.0, 0.0, "downloading",
                        )
                        write_mode = "ab" if offset > 0 else "wb"
                        with open(partial_path, write_mode) as fh:
                            async for chunk in response.aiter_bytes(chunk_size=_CHUNK_SIZE):
                                if self._cancel_flags.get(model_id):
                                    self._progress[model_id] = DownloadProgress(
                                        model_id, downloaded, total, 0.0, 0.0, "cancelled",
                                    )
                                    logger.info("download_cancelled model_id=%s", model_id)
                                    return partial_path
                                if (
                                    response_size is not None
                                    and downloaded + len(chunk) > offset + response_size
                                ):
                                    raise ValueError(
                                        "Download body exceeds the declared response length"
                                    )
                                fh.write(chunk)
                                downloaded += len(chunk)

                                now = time.monotonic()
                                window_elapsed = now - last_window_time
                                if window_elapsed >= 0.5:
                                    window_bytes = downloaded - last_window_bytes
                                    instant_speed = window_bytes / window_elapsed
                                    speed = ema.update(instant_speed)
                                    eta = (
                                        (total - downloaded) / speed
                                        if speed > 0 and total > 0 else 0.0
                                    )
                                    self._progress[model_id] = DownloadProgress(
                                        model_id, downloaded, total, round(speed, 1),
                                        round(eta, 1), "downloading",
                                    )
                                    last_window_time = now
                                    last_window_bytes = downloaded

                        # A short body or a server-chosen subrange is resumable,
                        # not a corrupt complete file. Keep it out of the hash
                        # failure path, which discards genuinely bad files.
                        if response_size is not None and downloaded != offset + response_size:
                            raise ValueError(
                                "Download ended before the declared response length; retry to resume"
                            )
                        if response_total is not None and downloaded != response_total:
                            raise ValueError("Download is incomplete; retry to resume")
                        break

        except httpx.HTTPStatusError as exc:
            err = f"HTTP {exc.response.status_code}"
            self._set_error(model_id, downloaded, total, err)
            raise
        except Exception as exc:
            self._set_error(model_id, downloaded, total, str(exc))
            raise

        # Verify or skip
        if not expected_sha256:
            logger.warning("sha256_skip model_id=%s reason=empty_hash", model_id)
            partial_path.rename(file_path)
            meta_path.unlink(missing_ok=True)  # partial done; no hash to record
            self._progress[model_id] = DownloadProgress(
                model_id=model_id,
                bytes_downloaded=downloaded,
                bytes_total=downloaded,
                speed_bps=0.0,
                eta_seconds=0.0,
                status="complete",
            )
            logger.info("download_complete model_id=%s bytes=%d", model_id, downloaded)
            return file_path

        # SHA256 verification
        self._progress[model_id] = DownloadProgress(
            model_id=model_id,
            bytes_downloaded=downloaded,
            bytes_total=downloaded,
            speed_bps=0.0,
            eta_seconds=0.0,
            status="verifying",
        )
        logger.info("sha256_verify_start model_id=%s", model_id)

        loop = asyncio.get_running_loop()
        actual_hash = await loop.run_in_executor(None, lambda: _sha256_file(partial_path))

        if actual_hash != expected_sha256.lower():
            if not unknown_range_total:
                partial_path.unlink(missing_ok=True)
            err = f"sha256 mismatch: got {actual_hash[:16]}... expected {expected_sha256[:16]}..."
            self._set_error(model_id, downloaded, downloaded, err)
            raise ValueError(f"SHA256 mismatch for {model_id}")

        partial_path.rename(file_path)
        # Record the verified hash next to the model so it can be re-checked at
        # load time (opt-in), and drop the now-stale partial meta.
        try:
            hash_sidecar.write_text(actual_hash)
        except OSError:
            logger.warning("sha256_sidecar_write_failed model_id=%s", model_id)
        meta_path.unlink(missing_ok=True)
        self._progress[model_id] = DownloadProgress(
            model_id=model_id,
            bytes_downloaded=downloaded,
            bytes_total=downloaded,
            speed_bps=0.0,
            eta_seconds=0.0,
            status="complete",
        )
        logger.info("download_complete model_id=%s bytes=%d verified=true", model_id, downloaded)
        return file_path

    def _set_error(self, model_id: str, downloaded: int, total: int, message: str) -> None:
        self._progress[model_id] = DownloadProgress(
            model_id=model_id,
            bytes_downloaded=downloaded,
            bytes_total=total,
            speed_bps=0.0,
            eta_seconds=0.0,
            status="error",
            error=message,
        )
        logger.error("download_error model_id=%s error=%s", model_id, message)

    def cancel_download(self, model_id: str) -> None:
        self._cancel_flags[model_id] = True

    def get_progress(self, model_id: str) -> DownloadProgress | None:
        return self._progress.get(model_id)


def _response_lengths(response: httpx.Response, offset: int) -> tuple[int | None, int | None]:
    """Validate headers before touching the partial; return body and full sizes.

    A catalog size is a progress estimate, not a substitute for HTTP framing.
    See RFC 9110 section 14.4 for Content-Range validity and byte offsets.
    """
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise ValueError("Compressed model responses cannot be resumed safely")
    raw_length = response.headers.get("content-length")
    if raw_length is not None and not re.fullmatch(r"[0-9]+", raw_length.strip()):
        raise ValueError("Invalid Content-Length for model download")
    length = int(raw_length) if raw_length is not None else None
    if response.status_code == 200:
        return length, length
    if response.status_code != 206:
        raise ValueError(f"Unexpected HTTP {response.status_code} for model download")
    match = re.fullmatch(
        r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)",
        response.headers.get("content-range", "").strip(), re.I,
    )
    if match is None:
        raise ValueError("Missing or invalid Content-Range for resumed download")
    start, end = int(match[1]), int(match[2])
    total = None if match[3] == "*" else int(match[3])
    if start != offset or end < start or (total is not None and end >= total):
        raise ValueError("Content-Range does not match the requested download offset")
    body_size = end - start + 1
    if length is not None and length != body_size:
        raise ValueError("Content-Length disagrees with Content-Range")
    return body_size, total


def _partial_meta_matches(meta_path: Path, url: str, expected_sha256: str) -> bool:
    """True when the partial's recorded (url, sha256) matches this request, so it
    is safe to resume. Missing/unreadable/mismatched meta returns False."""
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return False
    return meta.get("url") == url and meta.get("sha256") == expected_sha256


def _write_partial_meta(meta_path: Path, url: str, expected_sha256: str) -> None:
    """Record what a fresh partial is being downloaded for, so a later resume can
    confirm it still matches. Best-effort: a write failure just means the next
    run can't resume and will start fresh."""
    try:
        meta_path.write_text(json.dumps({"url": url, "sha256": expected_sha256}))
    except OSError:
        logger.warning("partial_meta_write_failed path=%s", meta_path)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


_downloader: ModelDownloader | None = None


def get_downloader(models_dir: Path | None = None) -> ModelDownloader:
    global _downloader
    if _downloader is None:
        from potluck_local.utils.paths import models_dir as default_models_dir
        _downloader = ModelDownloader(models_dir or default_models_dir())
    return _downloader

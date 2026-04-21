"""Resumable model downloader with SHA256 verification.

Downloads GGUF files using HTTP Range requests so interrupted downloads
resume without re-fetching bytes already on disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
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

        self._cancel_flags[model_id] = False
        existing_bytes = partial_path.stat().st_size if partial_path.exists() else 0

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

        headers: dict[str, str] = {}
        if existing_bytes > 0:
            headers["Range"] = f"bytes={existing_bytes}-"
            logger.info("download_resume model_id=%s offset=%d", model_id, existing_bytes)

        ema = _EMA()
        last_window_time = time.monotonic()
        last_window_bytes = existing_bytes
        downloaded = existing_bytes

        try:
            async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()

                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        reported_total = existing_bytes + int(content_length)
                        if expected_size is not None and reported_total != expected_size:
                            logger.warning(
                                "download_size_mismatch model_id=%s expected=%d reported=%d",
                                model_id,
                                expected_size,
                                reported_total,
                            )
                        total = reported_total
                    else:
                        total = expected_size or 0

                    write_mode = "ab" if existing_bytes > 0 else "wb"
                    with open(partial_path, write_mode) as fh:
                        async for chunk in response.aiter_bytes(chunk_size=_CHUNK_SIZE):
                            if self._cancel_flags.get(model_id):
                                self._progress[model_id] = DownloadProgress(
                                    model_id=model_id,
                                    bytes_downloaded=downloaded,
                                    bytes_total=total,
                                    speed_bps=0.0,
                                    eta_seconds=0.0,
                                    status="cancelled",
                                )
                                logger.info("download_cancelled model_id=%s", model_id)
                                return partial_path

                            fh.write(chunk)
                            downloaded += len(chunk)

                            now = time.monotonic()
                            window_elapsed = now - last_window_time
                            if window_elapsed >= 0.5:
                                window_bytes = downloaded - last_window_bytes
                                instant_speed = window_bytes / window_elapsed
                                speed = ema.update(instant_speed)
                                eta = (total - downloaded) / speed if speed > 0 and total > 0 else 0.0
                                self._progress[model_id] = DownloadProgress(
                                    model_id=model_id,
                                    bytes_downloaded=downloaded,
                                    bytes_total=total,
                                    speed_bps=round(speed, 1),
                                    eta_seconds=round(eta, 1),
                                    status="downloading",
                                )
                                last_window_time = now
                                last_window_bytes = downloaded

        except httpx.HTTPStatusError as exc:
            err = f"HTTP {exc.response.status_code}"
            self._set_error(model_id, downloaded, expected_size or 0, err)
            raise
        except Exception as exc:
            self._set_error(model_id, downloaded, expected_size or 0, str(exc))
            raise

        # Verify or skip
        if not expected_sha256:
            logger.warning("sha256_skip model_id=%s reason=empty_hash", model_id)
            partial_path.rename(file_path)
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
            partial_path.unlink(missing_ok=True)
            err = f"sha256 mismatch: got {actual_hash[:16]}... expected {expected_sha256[:16]}..."
            self._set_error(model_id, downloaded, downloaded, err)
            raise ValueError(f"SHA256 mismatch for {model_id}")

        partial_path.rename(file_path)
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

"""Resumable model downloader with SHA256 verification.

TODO: Implement per guide §4.2:
  - ModelDownloader(models_dir) constructor
  - download_model(model_id, url, expected_sha256, expected_size) -> Path
    * Range-request resume support
    * Progress tracking via DownloadProgress dataclass
    * Cancel via _cancel_flags dict
    * SHA256 verification before rename from .partial to .gguf
  - get_progress(model_id) -> DownloadProgress | None
  - cancel_download(model_id)
"""

from __future__ import annotations

from pathlib import Path


class ModelDownloader:
    """Handles resumable GGUF model downloads with integrity verification."""

    def __init__(self, models_dir: Path) -> None:
        # TODO: store models_dir, initialise _active_downloads, _progress,
        #       _cancel_flags dicts
        pass

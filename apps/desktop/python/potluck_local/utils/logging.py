"""Logging configuration for the local sidecar.

In dev mode (POTLUCK_DEV=1): human-readable format at DEBUG level.
In production: JSON-structured format at INFO level for log aggregation.
"""

from __future__ import annotations

import logging
import sys


_JSON_FMT = (
    '{"time":"%(asctime)s","level":"%(levelname)s",'
    '"logger":"%(name)s","message":"%(message)s"}'
)
_DEV_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%dT%H:%M:%S"


def setup_logging(dev: bool = False) -> None:
    """Configure the root logger.

    Call once at startup from app.main().  Subsequent calls are idempotent
    because basicConfig() is a no-op when handlers are already installed.

    Args:
        dev: When True, emit coloured human-readable output at DEBUG level.
             When False, emit minified JSON at INFO level.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt=_DEV_FMT if dev else _JSON_FMT,
            datefmt=_DATE_FMT,
        )
    )

    logging.basicConfig(
        level=logging.DEBUG if dev else logging.INFO,
        handlers=[handler],
        force=False,  # respect existing configuration (e.g., uvicorn's own setup)
    )

    # Suppress noisy third-party loggers in production
    if not dev:
        for name in ("uvicorn.access", "multipart"):
            logging.getLogger(name).setLevel(logging.WARNING)

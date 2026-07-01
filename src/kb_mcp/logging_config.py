"""Rotating-file logger configuration for exomem."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_dir: Path, level: int = logging.INFO) -> None:
    # Honor FASTMCP_LOG_LEVEL so fastmcp's auth/JWT DEBUG lines (e.g. the exact
    # reason behind an `invalid_token` 401) are surfaceable without a code change.
    env_level = os.environ.get("FASTMCP_LOG_LEVEL", "").upper()
    if env_level:
        level = getattr(logging, env_level, level)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "exomem.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers on reconfiguration.
    for existing in list(root.handlers):
        if isinstance(existing, RotatingFileHandler):
            root.removeHandler(existing)
    root.addHandler(handler)

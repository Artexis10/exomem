"""Rotating-file logger configuration for exomem."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def resolve_log_dir(default: Path | None = None) -> Path:
    """The log directory: $EXOMEM_LOG_DIR when set, else `default`, else the
    checkout-derived `<repo>/logs`.

    EXOMEM_LOG_DIR exists for installs where the package directory isn't
    writable — containers (the image sets it to /data/logs) and non-root
    wheel installs. It must be a PROCESS env var (container ENV, service
    environment, shell): logging configures before the server loads `.env`,
    so a value only in `.env` arrives too late.
    """
    env = os.environ.get("EXOMEM_LOG_DIR", "").strip()
    if env:
        return Path(env)
    if default is not None:
        return default
    return Path(__file__).resolve().parents[2] / "logs"


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

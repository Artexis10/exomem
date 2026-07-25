"""Rotating-file JSONL logger configuration for exomem."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .log_events import JsonLinesFormatter

# Windows cannot share a single RotatingFileHandler across processes (the
# rename-on-rotate step fails when another process holds the file open), and
# one checkout runs more than one process kind: the long-lived server,
# one-shot CLI invocations, and the media worker child. Each gets its own
# file so nothing contends for another's handle.
_LOG_FILENAMES: dict[str, str] = {
    "server": "exomem.log",
    "cli": "exomem-cli.log",
    "media": "exomem-media.log",
}


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


def _resolve_level(level: int) -> int:
    # EXOMEM_LOG_LEVEL is canonical; FASTMCP_LOG_LEVEL is honored as a
    # fallback so fastmcp's auth/JWT DEBUG lines (e.g. the exact reason
    # behind an `invalid_token` 401) stay surfaceable without a code change.
    for name in ("EXOMEM_LOG_LEVEL", "FASTMCP_LOG_LEVEL"):
        raw = os.environ.get(name, "").strip().upper()
        if raw:
            return getattr(logging, raw, level)
    return level


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def configure_logging(
    log_dir: Path, level: int = logging.INFO, *, process: str = "server"
) -> None:
    """Configure root JSONL file logging for one process role.

    `process` selects the per-process-role log file
    (`server`/`cli`/`media`, default `server`) under `log_dir`.
    """
    level = _resolve_level(level)
    filename = _LOG_FILENAMES.get(process, _LOG_FILENAMES["server"])
    max_bytes = int(_positive_float_env("EXOMEM_LOG_MAX_MB", 5.0) * 1024 * 1024)
    backup_count = _positive_int_env("EXOMEM_LOG_BACKUPS", 5)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / filename,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(JsonLinesFormatter())
    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers on reconfiguration.
    for existing in list(root.handlers):
        if isinstance(existing, RotatingFileHandler):
            root.removeHandler(existing)
    root.addHandler(handler)
    if process == "server":
        _silence_uvicorn_access()


def _silence_uvicorn_access() -> None:
    """Silence uvicorn's own unstructured access log the SAME moment the
    structured `AccessLogMiddleware` record becomes the access log, so the
    same request is never described by both an unparseable text line and a
    JSONL record.

    `uvicorn.Config.__init__` calls its own `configure_logging()` later
    (inside `mcp.run()`, after this function returns), which re-applies its
    default `dictConfig` and would re-add a handler for this logger name —
    but `disable_existing_loggers` is `False` in that config and `disabled`
    is not a key `dictConfig` ever sets, so `.disabled = True` set here
    survives that later call intact. Setting `propagate = False` and
    clearing handlers too matches uvicorn's own `access_log=False` behavior
    for defense in depth.
    """
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_logger.propagate = False
    access_logger.disabled = True

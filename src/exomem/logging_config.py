"""Rotating-file JSONL logger configuration for exomem."""

from __future__ import annotations

import logging
import os
import sys
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


def _is_source_checkout(candidate_root: Path) -> bool:
    """Whether `candidate_root` (the `parents[2]` hop from this module's
    `__file__`) is genuinely this project's source checkout, not the
    `<venv>/Lib` (or `<venv>/lib/pythonX.Y`) hop a wheel install produces at
    that same index.

    Two markers, both required, chosen so a wheel install cannot satisfy them
    by accident:

    - This module's immediate parent directory is literally named `src`. A
      wheel (or any non-editable) install copies `logging_config.py` straight
      into `.../site-packages/exomem/`, never into
      `.../site-packages/src/exomem/` — only a genuine src-layout checkout
      (including an editable install, whose `__file__` still points at the
      real source tree) has `src` as this module's parent.
    - `candidate_root / "pyproject.toml"` exists, confirming `candidate_root`
      is an actual project root and not just some directory that happens to
      sit two hops up.

    `site-packages`/`dist-packages` anywhere in the module's own path is
    rejected outright as an independent second guard, even if the two
    markers above were somehow both satisfied.
    """
    package_dir = Path(__file__).resolve().parent
    if package_dir.parent.name != "src":
        return False
    lowered_parts = {part.lower() for part in package_dir.parts}
    if "site-packages" in lowered_parts or "dist-packages" in lowered_parts:
        return False
    return (candidate_root / "pyproject.toml").is_file()


def _user_log_dir() -> Path:
    """Per-user, per-platform log location for when this isn't a source
    checkout (e.g. a wheel install with EXOMEM_LOG_DIR unset) — the venv's
    own `Lib`/`site-packages` directory is not writable by convention and not
    where a log directory belongs.

    Mirrors `install_info.managed_manifest_path()`'s per-platform root
    (`%LOCALAPPDATA%\\Exomem` on Windows, `~/Library/...Exomem` on macOS, XDG
    on Linux) so installs already familiar with that split find logs in the
    sibling convention for their platform. Linux uses `XDG_STATE_HOME` (falling
    back to `~/.local/state`) rather than XDG's config dir, since logs are
    state, not configuration, per the XDG Base Directory spec.
    """
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(root) if root else Path.home() / "AppData" / "Local"
        return base / "Exomem" / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "Exomem"
    root = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(root) if root else Path.home() / ".local" / "state"
    return base / "exomem" / "logs"


def resolve_log_dir(default: Path | None = None) -> Path:
    """The log directory: $EXOMEM_LOG_DIR when set, else `default`, else the
    checkout-derived `<repo>/logs` when this genuinely IS a source checkout,
    else a per-user platform log location.

    EXOMEM_LOG_DIR exists for installs where the package directory isn't
    writable — containers (the image sets it to /data/logs) and non-root
    wheel installs. It must be a PROCESS env var (container ENV, service
    environment, shell): logging configures before the server loads `.env`,
    so a value only in `.env` arrives too late.

    The final fallback used to assume a src-layout checkout unconditionally
    (`Path(__file__).resolve().parents[2] / "logs"`), which is correct for
    `<repo>/src/exomem/logging_config.py` but silently wrong for a wheel
    install, where the same two-hop climb from
    `<venv>/Lib/site-packages/exomem/logging_config.py` lands inside the venv
    itself (`<venv>/Lib`) — logs then land at `<venv>/Lib/logs`, invisible
    next to the actual venv/service layout. `_is_source_checkout()` detects
    the checkout case positively instead of assuming it.
    """
    env = os.environ.get("EXOMEM_LOG_DIR", "").strip()
    if env:
        return Path(env)
    if default is not None:
        return default
    checkout_root = Path(__file__).resolve().parents[2]
    if _is_source_checkout(checkout_root):
        return checkout_root / "logs"
    return _user_log_dir()


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

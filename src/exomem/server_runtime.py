"""Server startup wiring for Exomem.

This module owns process-local runtime setup: environment loading, vault
resolution, warmup/model policy, media extraction, and file watching. It is kept
separate from transport route registration so ``server.build_server`` stays a
small composition root.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import env_compat, extract, project_keys, schema
from .vault import resolve_vault

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServerRuntime:
    vault_root: Path
    source_schema: Any
    project_keys_hint: str
    base_url: str
    media_worker: Any | None = None
    file_watcher: Any | None = None


def initialize_runtime(*, load_dotenv_func: Callable[..., object]) -> ServerRuntime:
    """Initialize process-local server runtime state.

    ``load_dotenv_func`` is injected from ``server.py`` so tests that monkeypatch
    ``exomem.server.load_dotenv`` still neutralize dotenv loading exactly as they
    did before this extraction.
    """
    load_dotenv_func(override=True)
    env_compat.promote_legacy()

    vault_root = resolve_vault()
    source_schema = schema.load_source_schema(vault_root)
    log.info("vault=%s source_types=%s", vault_root, source_schema.source_types)

    project_keys_hint = project_keys.keys_hint(vault_root)
    _start_compute_runtime(vault_root)
    media_worker = _start_media_worker(vault_root)
    file_watcher = _start_file_watcher(vault_root)

    base_url = os.environ.get("EXOMEM_BASE_URL", "").strip().rstrip("/")
    return ServerRuntime(
        vault_root=vault_root,
        source_schema=source_schema,
        project_keys_hint=project_keys_hint,
        base_url=base_url,
        media_worker=media_worker,
        file_watcher=file_watcher,
    )


def _start_compute_runtime(vault_root: Path) -> None:
    """Start warmup, model unloading, and live compute-mode watching."""
    from . import mode, warmup

    log.info("compute policy: %s", mode.resolved())
    if warmup.warmup_enabled():
        if os.environ.get("EXOMEM_EAGER_BOOT"):
            warmup.warm_all(vault_root)
        else:
            warmup.start_background(vault_root)

    if mode.release_when_idle():
        from . import model_reaper

        model_reaper.start()

    mode.start_config_watch()

    from . import auto_quiet

    auto_quiet.start_if_enabled()


def _start_media_worker(vault_root: Path) -> Any | None:
    """Start the optional off-request media extraction worker."""
    if not extract.extraction_enabled():
        return None

    from . import media_worker as media_worker_module

    worker = media_worker_module.MediaWorker(vault_root)
    worker.start()
    try:
        worker.scan_pending()
    except Exception as exc:  # noqa: BLE001 - startup scan is best-effort
        log.warning("media worker startup scan failed: %s", exc)
    return worker


def _start_file_watcher(vault_root: Path) -> Any | None:
    """Start the optional live file watcher."""
    if os.environ.get("EXOMEM_DISABLE_FILE_WATCHER"):
        return None

    from . import file_watcher as file_watcher_module

    watcher = file_watcher_module.FileWatcher(vault_root)
    try:
        watcher.start()
    except Exception as exc:  # noqa: BLE001 - watcher must not break startup
        log.warning("file watcher start failed: %s", exc)
    return watcher

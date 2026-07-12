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

from . import env_compat, hosted_runtime, privacy_log, project_keys, schema
from .hosted_runtime import HostedCellConfig, HostedCellLifecycle, hosted_mode_enabled
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
    hosted_config: HostedCellConfig | None = None
    hosted_lifecycle: HostedCellLifecycle | None = None


def initialize_runtime(*, load_dotenv_func: Callable[..., object]) -> ServerRuntime:
    """Initialize process-local server runtime state.

    ``load_dotenv_func`` is injected from ``server.py`` so tests that monkeypatch
    ``exomem.server.load_dotenv`` still neutralize dotenv loading exactly as they
    did before this extraction.
    """
    if hosted_mode_enabled():
        return _initialize_hosted_runtime()

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


def _initialize_hosted_runtime() -> ServerRuntime:
    """Initialize one explicit hosted cell without reading any dotenv file."""
    privacy_log.install_hosted_log_redaction()
    config = HostedCellConfig.from_env(require_provisioned=True)
    config.apply_process_environment()
    lifecycle = HostedCellLifecycle(config)
    vault_root = config.vault_root

    source_schema = schema.load_source_schema(vault_root)
    project_keys_hint = project_keys.keys_hint(vault_root)
    log.info(
        "hosted_cell=%s source_types=%s",
        config.cell_id,
        source_schema.source_types,
    )

    mutation_ready, mutation_reason = probe_hosted_mutation_authority(vault_root)

    startup = lifecycle.complete_startup(
        vault_ready=True,
        mutation_authority_ready=mutation_ready,
        service_auth_ready=True,
    )
    if not mutation_ready:
        lifecycle.set_mutation_authority(False, reason_code=mutation_reason)

    media_worker = None
    file_watcher = None
    if not mutation_ready:
        for feature in ("embeddings", "file-watcher", "media"):
            if config.has_feature(feature):
                lifecycle.set_worker_status(
                    feature,
                    ready=False,
                    reason_code="HOSTED_MUTATION_AUTHORITY_UNAVAILABLE",
                )
    elif config.resource_limits.worker_count == 0:
        for feature in ("embeddings", "file-watcher", "media"):
            if config.has_feature(feature):
                lifecycle.set_worker_status(
                    feature,
                    ready=False,
                    reason_code="HOSTED_WORKER_LIMIT_ZERO",
                )
    elif startup.phase != "active":
        for feature in ("embeddings", "file-watcher", "media"):
            if config.has_feature(feature):
                lifecycle.set_worker_status(
                    feature,
                    ready=False,
                    reason_code="HOSTED_CELL_NOT_ACTIVE",
                )
    else:
        if config.has_feature("embeddings"):
            _start_compute_runtime(vault_root)
        if config.has_feature("media"):
            media_worker = _start_media_worker(vault_root)
            lifecycle.set_worker_status(
                "media",
                ready=media_worker is not None,
                reason_code="HOSTED_WORKER_UNAVAILABLE",
            )
            if media_worker is not None:
                lifecycle.register_background_worker(
                    stopper=media_worker.stop, starter=media_worker.start
                )
        if config.has_feature("file-watcher"):
            file_watcher = _start_file_watcher(vault_root)
            lifecycle.set_worker_status(
                "file-watcher",
                ready=file_watcher is not None,
                reason_code="HOSTED_WORKER_UNAVAILABLE",
            )
            if file_watcher is not None:
                lifecycle.register_background_worker(
                    stopper=file_watcher.stop, starter=file_watcher.start
                )

    return ServerRuntime(
        vault_root=vault_root,
        source_schema=source_schema,
        project_keys_hint=project_keys_hint,
        base_url="",
        media_worker=media_worker,
        file_watcher=file_watcher,
        hosted_config=config,
        hosted_lifecycle=lifecycle,
    )


def probe_hosted_mutation_authority(vault_root: Path) -> tuple[bool, str]:
    """Prove the shared mutation guard can be acquired and safely released."""

    try:
        with hosted_runtime.hosted_mutation_guard(vault_root):
            pass
    except Exception as exc:  # noqa: BLE001 - any uncertainty keeps hosted writes closed
        log.warning(
            "hosted mutation authority unavailable error=%s",
            type(exc).__name__,
        )
        return False, "HOSTED_MUTATION_AUTHORITY_UNAVAILABLE"
    return True, "HOSTED_READY"


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
    if os.environ.get("EXOMEM_DISABLE_MEDIA_EXTRACTION"):
        return None

    from . import media_worker as media_worker_module

    worker = None
    try:
        worker = media_worker_module.MediaWorker(vault_root)
        worker.start()
    except Exception as exc:  # noqa: BLE001 - media must never deny the core service
        if worker is not None:
            try:
                worker.stop()
            except Exception:  # noqa: BLE001 - startup degradation must remain soft
                pass
        log.warning("media runtime unavailable; core service continuing: %s", exc)
        return None
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

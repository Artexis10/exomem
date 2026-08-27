"""Server startup wiring for Exomem.

This module owns process-local runtime setup: environment loading, vault
resolution, warmup/model policy, media extraction, and file watching. It is kept
separate from transport route registration so ``server.build_server`` stays a
small composition root.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    env_compat,
    hosted_runtime,
    media_processing,
    metrics,
    privacy_log,
    project_keys,
    schema,
)
from .governance import authorization_session_lifecycle, projection_runtime
from .governance.authorization_serving_membership import unavailable_readiness
from .hosted_runtime import (
    HostedBindingV2,
    HostedCellConfig,
    HostedCellLifecycle,
    hosted_mode_enabled,
)
from .vault import resolve_vault

log = logging.getLogger(__name__)

# The production Windows seed has measured about 49 seconds under pressure.
# Give it substantial headroom, but never let one blocked watcher prevent the
# background catalogue warm/repair path from starting indefinitely.
RECALL_SEED_WAIT_SECONDS = 120.0


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
    hosted_security_authority: Any | None = None
    hosted_lifetime_lock: AbstractContextManager[None] | None = None


def initialize_runtime(*, load_dotenv_func: Callable[..., object]) -> ServerRuntime:
    """Initialize process-local server runtime state.

    ``load_dotenv_func`` is injected from ``server.py`` so tests that monkeypatch
    ``exomem.server.load_dotenv`` still neutralize dotenv loading exactly as they
    did before this extraction.
    """
    if hosted_mode_enabled():
        return _initialize_hosted_runtime()

    # An installed package lives under site-packages, so python-dotenv's implicit
    # caller-relative search misses the service working directory. The documented
    # repo-root .env is explicitly cwd-relative for both checkout and wheel installs.
    load_dotenv_func(dotenv_path=Path.cwd() / ".env", override=True)
    env_compat.promote_legacy()

    vault_root = resolve_vault()
    source_schema = schema.load_source_schema(vault_root)
    log.info("vault=%s source_types=%s", vault_root, source_schema.source_types)

    project_keys_hint = project_keys.keys_hint(vault_root)
    projection_runtime.preactivate_projection_runtime(vault_root)
    _start_metrics_persistence()
    base_url = os.environ.get("EXOMEM_BASE_URL", "").strip().rstrip("/")
    return ServerRuntime(
        vault_root=vault_root,
        source_schema=source_schema,
        project_keys_hint=project_keys_hint,
        base_url=base_url,
    )


class LocalRuntimeActivation:
    """Start local background workers after transport liveness is observable."""

    def __init__(self, vault_root: Path, *, fallback_seconds: float = 5.0) -> None:
        from . import readiness, warmup

        if warmup.warmup_enabled():
            readiness.manage_runtime()
        self.vault_root = vault_root
        self.fallback_seconds = fallback_seconds
        self._lock = threading.Lock()
        self._started = False
        self._timer: threading.Timer | None = None
        self._thread: threading.Thread | None = None
        self.media_worker: Any | None = None
        self.file_watcher: Any | None = None

    def start(self) -> None:
        """Launch local workers once; safe from health and timer races."""
        with self._lock:
            if self._started:
                return
            self._started = True
            timer = self._timer
        if timer is not None:
            timer.cancel()
        thread = threading.Thread(
            target=self._activate,
            name="exomem-local-activation",
            daemon=True,
        )
        with self._lock:
            self._thread = thread
        thread.start()

    def _activate(self) -> None:
        self._start_component("file watcher", self._start_file_watcher)
        if self.file_watcher is None:
            # A maintained projection is authoritative only while something is
            # actually maintaining it.  The explicit watcher-off mode and a
            # watcher startup failure retain the supported walk-backed path.
            self._downgrade_recall_runtime()
        self._wait_for_recall_seed()
        self._start_component("retrieval", _start_compute_runtime)
        self._wait_for_required_admission()
        self._start_component(
            "file watcher recovery",
            self._finish_file_watcher_startup,
        )
        starters = (
            ("graph drain", _start_graph_drain),
            ("media", self._start_media_worker),
        )
        for label, starter in starters:
            self._start_component(label, starter)

    def _wait_for_recall_seed(self) -> None:
        """Order maintained-catalog verification behind watcher authority."""
        from . import freshness

        watcher = self.file_watcher
        if watcher is None or not freshness.event_indexes_enabled():
            return
        try:
            seeded = watcher.wait_until_seeded(timeout=RECALL_SEED_WAIT_SECONDS)
        except Exception:  # noqa: BLE001 - retrieval fallback stays background-only
            log.warning("file watcher seed wait failed", exc_info=True)
            self._downgrade_recall_runtime()
            return
        if not seeded:
            log.warning(
                "file watcher did not establish both recall projections; "
                "switching this process to exact walk-backed recall"
            )
            self._downgrade_recall_runtime()

    def _downgrade_recall_runtime(self) -> None:
        """Revoke an unavailable watcher generation and retain exact fallback."""
        from . import freshness, readiness

        watcher = self.file_watcher
        # Invalidation cancels any replacement walk that is still in flight;
        # if it eventually returns it cannot re-publish an unmaintained map.
        freshness.invalidate(self.vault_root)
        readiness.unmanage_runtime()
        self.file_watcher = None
        stop = getattr(watcher, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:  # noqa: BLE001 - fallback is already authoritative
                log.warning(
                    "file watcher shutdown after recall downgrade failed",
                    exc_info=True,
                )

    def _wait_for_required_admission(self) -> None:
        """Keep reconcilers out until retrieval and mutation state are admitted."""
        from . import readiness, warmup

        if not warmup.warmup_enabled():
            return
        while True:
            catalog_ready = readiness.is_ready("retrieval_catalog")
            semantic_ready = readiness.is_ready("semantic_corpus")
            if catalog_ready and (semantic_ready or not readiness.is_warming()):
                # Semantic corpus has no independent post-warm repair signal.
                # Let it serialize behind retrieval while the one-shot warm is
                # active, then preserve the historical soft-failure behavior
                # instead of stranding graph/media recovery forever.
                return
            if not readiness.warm_started():
                # A custom/hosted starter elected not to open a managed warm.
                return
            missing = "retrieval_catalog" if not catalog_ready else "semantic_corpus"
            readiness.wait(missing, timeout=0.1)

    def _start_component(self, label: str, starter: Callable[[Path], Any]) -> None:
        try:
            starter(self.vault_root)
        except Exception:  # noqa: BLE001 - workers cannot deny transport liveness
            log.warning(
                "%s runtime startup failed; transport continuing",
                label,
                exc_info=True,
            )

    def _start_media_worker(self, vault_root: Path) -> None:
        self.media_worker = _start_media_worker(vault_root)

    def _start_file_watcher(self, vault_root: Path) -> None:
        self.file_watcher = _start_file_watcher(vault_root)

    def _finish_file_watcher_startup(self, _vault_root: Path) -> None:
        watcher = self.file_watcher
        finish = getattr(watcher, "finish_startup_recovery", None)
        if callable(finish):
            finish()

    def lifespan(self):
        """Arm a fallback for clients that never call the liveness endpoint."""
        from fastmcp.server.lifespan import lifespan

        @lifespan
        async def _lifespan(_server):
            timer = threading.Timer(self.fallback_seconds, self.start)
            timer.daemon = True
            with self._lock:
                if not self._started:
                    self._timer = timer
                    timer.start()
            try:
                yield {}
            finally:
                timer.cancel()

        return _lifespan


def _initialize_hosted_runtime() -> ServerRuntime:
    """Initialize one explicit hosted cell without reading any dotenv file."""
    privacy_log.install_hosted_log_redaction()
    config = HostedCellConfig.from_env(require_provisioned=True)
    binding = None
    if config.requires_dynamic_security:
        assert config.vault_id is not None
        binding = HostedBindingV2(
            cell_id=config.cell_id,
            vault_id=config.vault_id,
            vault_root=config.vault_root,
            state_root=config.state_root,
            log_root=config.log_root,
            runtime_uid=config.runtime_uid,
            runtime_gid=config.runtime_gid,
        )
    from .hosted_restore import acquire_hosted_lifetime_lock

    lifetime_lock = acquire_hosted_lifetime_lock(config.state_root, binding=binding)
    lifetime_lock.__enter__()
    try:
        _cleanup_hosted_transfer_temp(config)
        return _initialize_locked_hosted_runtime(config, lifetime_lock)
    except BaseException:
        lifetime_lock.__exit__(*sys.exc_info())
        raise


def _hosted_authorization_session_readiness_provider(
    config: HostedCellConfig,
) -> Callable[[], Any]:
    """Bind Hosted membership checks to deployment-owned identity, never liveness."""

    if config.vault_id is None or config.authorization_session_replica_id is None:
        return unavailable_readiness

    def readiness() -> Any:
        return authorization_session_lifecycle.hosted_serving_membership_readiness(
            config.vault_root,
            expected_cell_id=config.cell_id,
            expected_logical_vault_id=config.vault_id,
            expected_replica_id=config.authorization_session_replica_id,
        )

    return readiness


def _initialize_locked_hosted_runtime(
    config: HostedCellConfig,
    lifetime_lock: AbstractContextManager[None],
) -> ServerRuntime:
    """Finish hosted startup while retaining exclusive target-root ownership."""

    config.apply_process_environment()
    _start_metrics_persistence()
    lifecycle = HostedCellLifecycle(
        config,
        authorization_session_readiness_provider=(
            _hosted_authorization_session_readiness_provider(config)
        ),
    )
    security_authority = _initialize_hosted_security(config)
    vault_root = config.vault_root

    source_schema = schema.load_source_schema(vault_root)
    project_keys_hint = project_keys.keys_hint(vault_root)
    projection_runtime.preactivate_projection_runtime(vault_root)
    log.info(
        "hosted_cell=%s source_types=%s",
        config.cell_id,
        source_schema.source_types,
    )

    mutation_ready, mutation_reason = probe_hosted_mutation_authority(vault_root)

    startup = lifecycle.complete_startup(
        vault_ready=True,
        mutation_authority_ready=mutation_ready,
        service_auth_ready=(
            security_authority is not None or config.service_credential is not None
        ),
    )
    if config.has_feature("diarization"):
        lifecycle.set_worker_status(
            "diarization",
            ready=False,
            reason_code="HOSTED_RUNTIME_TEMP_AUTHORITY_REQUIRED",
        )
    if not mutation_ready:
        lifecycle.set_mutation_authority(False, reason_code=mutation_reason)

    media_worker = None
    file_watcher = None
    if mutation_ready and startup.phase == "active":
        # Retrieval catalog warm-up is core service work, not an optional
        # hosted worker.  A zero optional-worker budget must still converge
        # truthful retrieval admission for lean cells.
        if config.has_feature("embeddings") and config.resource_limits.worker_count > 0:
            _start_compute_runtime(vault_root)
        else:
            _start_retrieval_runtime(vault_root)
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
    elif startup.phase == "quiesced":
        for feature in ("embeddings", "file-watcher", "media"):
            if config.has_feature(feature):
                lifecycle.set_worker_status(
                    feature,
                    ready=False,
                    reason_code="HOSTED_CELL_NOT_ACTIVE",
                )
        if config.has_feature("media"):
            media_worker = _create_media_worker(vault_root)
            if media_worker is not None:
                lifecycle.register_background_worker(
                    stopper=media_worker.stop,
                    starter=_worker_starter(lifecycle, "media", media_worker),
                )
        if config.has_feature("file-watcher"):
            file_watcher = _create_file_watcher(vault_root)
            if file_watcher is not None:
                lifecycle.register_background_worker(
                    stopper=file_watcher.stop,
                    starter=_worker_starter(lifecycle, "file-watcher", file_watcher),
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
        hosted_security_authority=security_authority,
        hosted_lifetime_lock=lifetime_lock,
    )


def _cleanup_hosted_transfer_temp(config: HostedCellConfig) -> None:
    from .hosted_runtime_temp import prepare_hosted_runtime_temp
    from .hosted_transfer_routes import cleanup_hosted_transfer_temp

    prepare_hosted_runtime_temp(
        config.state_root,
        expected_uid=config.runtime_uid,
        expected_gid=config.runtime_gid,
    )
    cleanup_hosted_transfer_temp(config.state_root)


def _initialize_hosted_security(config: HostedCellConfig) -> Any | None:
    """Open and validate the v2 authority before the server becomes reachable."""

    if not config.requires_dynamic_security:
        return None
    assert config.vault_id is not None
    from .hosted_security import HostedSecurityAuthority

    authority = HostedSecurityAuthority(
        config.state_root,
        cell_id=config.cell_id,
        vault_id=config.vault_id,
        expected_uid=config.runtime_uid,
        expected_gid=config.runtime_gid,
    )
    authority.validate_ready()
    return authority


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


def _start_metrics_persistence() -> None:
    """Restore the metrics registry from its prior snapshot and start the
    background snapshotter against the writer-lease state directory.

    Best-effort: metrics persistence is not part of server readiness, so any
    failure here (an unreadable snapshot, a lease-config error) is logged and
    swallowed rather than blocking startup.
    """
    try:
        from .writer_lease import get_manager

        state_dir = get_manager().config.state_dir
        metrics.load_snapshot_once(state_dir)
        metrics.start_snapshotter(state_dir, metrics.snapshot_interval_seconds_from_env())
    except Exception as exc:  # noqa: BLE001 - metrics startup must never break the server
        log.warning("metrics persistence unavailable at startup: %s", exc)


def _start_retrieval_runtime(vault_root: Path) -> None:
    """Start core catalog/cache warm-up independently of optional models."""
    from . import warmup

    if warmup.warmup_enabled():
        if os.environ.get("EXOMEM_EAGER_BOOT"):
            from . import readiness

            readiness.begin_warm()
            try:
                warmup.warm_all(vault_root)
            finally:
                readiness.finish_warm()
        else:
            warmup.start_background(vault_root)


def _start_compute_runtime(vault_root: Path) -> None:
    """Start retrieval warm-up, model unloading, and live compute-mode watching."""
    from . import mode

    log.info("compute policy: %s", mode.resolved())
    _start_retrieval_runtime(vault_root)

    if mode.release_when_idle():
        from . import model_reaper

        model_reaper.start()

    mode.start_config_watch()

    from . import auto_quiet

    auto_quiet.start_if_enabled()


def _start_media_worker(vault_root: Path) -> Any | None:
    """Start the optional off-request media extraction worker."""
    worker = _create_media_worker(vault_root)
    unavailable_reason: str | None = None
    unavailable_action: str | None = None
    if worker is None:
        if os.environ.get("EXOMEM_DISABLE_MEDIA_EXTRACTION"):
            unavailable_reason = (
                "MediaExtractionDisabled: EXOMEM_DISABLE_MEDIA_EXTRACTION is set"
            )
            unavailable_action = (
                "enable media extraction by clearing EXOMEM_DISABLE_MEDIA_EXTRACTION, "
                "restart the service, then retry"
            )
        else:
            unavailable_reason = "MediaRuntimeUnavailable: media worker construction failed"
            unavailable_action = (
                "fix the media runtime configuration, restart the service, then retry"
            )
    if worker is not None:
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001 - media must never deny the core service
            try:
                worker.stop()
            except Exception:  # noqa: BLE001 - startup degradation must remain soft
                pass
            log.warning("media runtime unavailable; core service continuing: %s", exc)
            worker = None
            unavailable_reason = f"MediaRuntimeUnavailable: {type(exc).__name__}: {exc}"
            unavailable_action = (
                "fix the media runtime configuration, restart the service, then retry"
            )
    if unavailable_reason is not None and unavailable_action is not None:
        media_processing.set_media_runtime_unavailable(
            vault_root,
            reason=unavailable_reason,
            next_action=unavailable_action,
        )
    else:
        media_processing.set_media_runtime_available(vault_root)
    try:
        from .writer_lease import get_manager

        manager = get_manager()

        def reconcile_commit_guard():
            return manager.mutation_guard(
                vault_root,
                operation="startup_media_reconcile_commit",
                holder_kind="background",
            )

        media_processing.reconcile_all_media(
            vault_root,
            limit=media_processing.DEFAULT_RECONCILE_LIMIT,
            reconcile_one=lambda binary: media_processing.reconcile_media(
                vault_root,
                binary,
                explicit=False,
                commit_guard=reconcile_commit_guard,
            ),
        )
        if unavailable_reason is not None and unavailable_action is not None:

            def unavailable_commit_guard():
                return manager.mutation_guard(
                    vault_root,
                    operation="startup_media_unavailable_commit",
                    holder_kind="background",
                )

            media_processing.mark_processing_unavailable(
                vault_root,
                reason=unavailable_reason,
                next_action=unavailable_action,
                commit_guard=unavailable_commit_guard,
            )
    except Exception as exc:  # noqa: BLE001 - startup discovery is best-effort
        log.warning("media worker startup discovery failed: %s", exc)
    if worker is None:
        return None
    try:
        worker.scan_pending()
    except Exception as exc:  # noqa: BLE001 - startup scan is best-effort
        log.warning("media worker startup scan failed: %s", exc)
    return worker


def _create_media_worker(vault_root: Path) -> Any | None:
    """Construct a media worker without starting background execution."""
    if os.environ.get("EXOMEM_DISABLE_MEDIA_EXTRACTION"):
        return None

    from . import media_worker as media_worker_module

    try:
        return media_worker_module.MediaWorker(vault_root)
    except Exception as exc:  # noqa: BLE001 - media must never deny the core service
        log.warning("media runtime unavailable; core service continuing: %s", exc)
        return None


def _start_graph_drain(vault_root: Path) -> Any | None:
    """Start the drain that settles queued epistemic-graph repair.

    Deliberately not folded into the file watcher, even though the watcher is
    where the only existing drain call sites live. The watcher is optional -- it
    no-ops without `watchdog` and is skipped entirely under
    `EXOMEM_DISABLE_FILE_WATCHER` -- and graph convergence must not be optional
    with it. Where the watcher was absent, nothing drained the queue at all and
    the graph stayed `recovery_required` indefinitely with the repair already
    queued and admissible.
    """
    from . import graph_drain

    try:
        return graph_drain.start(vault_root)
    except Exception as exc:  # noqa: BLE001 - convergence must not break startup
        log.warning("graph drain start failed: %s", exc)
        return None


def _start_file_watcher(vault_root: Path) -> Any | None:
    """Start the optional live file watcher."""
    watcher = _create_file_watcher(vault_root)
    if watcher is None:
        return None
    try:
        if not watcher.start():
            return None
    except Exception as exc:  # noqa: BLE001 - watcher must not break startup
        log.warning("file watcher start failed: %s", exc)
        return None
    return watcher


def _create_file_watcher(vault_root: Path) -> Any | None:
    """Construct a file watcher without starting background execution."""
    if os.environ.get("EXOMEM_DISABLE_FILE_WATCHER"):
        return None

    from . import file_watcher as file_watcher_module

    try:
        return file_watcher_module.FileWatcher(vault_root)
    except Exception as exc:  # noqa: BLE001 - watcher must not break startup
        log.warning("file watcher unavailable; core service continuing: %s", exc)
        return None


def _worker_starter(
    lifecycle: HostedCellLifecycle,
    feature: str,
    worker: Any,
) -> Callable[[], None]:
    """Start a dormant worker and make its resumed health explicit."""

    def start() -> None:
        worker.start()
        lifecycle.set_worker_status(feature, ready=True)

    return start

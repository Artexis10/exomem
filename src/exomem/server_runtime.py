"""Server startup wiring for Exomem.

This module owns process-local runtime setup: environment loading, vault
resolution, warmup/model policy, media extraction, and file watching. It is kept
separate from transport route registration so ``server.build_server`` stays a
small composition root.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from . import (
    env_compat,
    media_processing,
    metrics,
    privacy_log,
    project_keys,
    schema,
    vocabulary_recovery,
)
from .dotenv_guard import working_directory_dotenv
from .governance import projection_runtime
from .vault import resolve_vault

log = logging.getLogger(__name__)

# The production Windows seed has measured about 49 seconds under pressure.
# Give it substantial headroom, but never let one blocked watcher prevent the
# background catalogue warm/repair path from starting indefinitely.
RECALL_SEED_WAIT_SECONDS = 120.0

# Failed catalogue publication can require another whole-vault build. Back off
# to limit repeated I/O under persistent failure, with a cap for quiet recovery.
CATALOGUE_RECOVERY_FIRST_BACKOFF_SECONDS = 1.0
CATALOGUE_RECOVERY_MAX_BACKOFF_SECONDS = 60.0


@dataclass(frozen=True)
class ServerRuntime:
    vault_root: Path
    source_schema: Any
    project_keys_hint: str
    base_url: str
    media_worker: Any | None = None
    file_watcher: Any | None = None
    derived_drain: Any | None = None


def initialize_runtime(*, load_dotenv_func: Callable[..., object]) -> ServerRuntime:
    """Initialize process-local server runtime state.

    ``load_dotenv_func`` is injected from ``server.py`` so tests that monkeypatch
    ``exomem.server.load_dotenv`` still neutralize dotenv loading exactly as they
    did before this extraction.
    """
    from . import cloud_cell

    cloud_cell.resource_policy()

    if cloud_cell.cloud_mode_enabled():
        # A cloud cell reads no `.env` file: configuration comes only from the
        # pod environment (design D1.6). Everything else below this branch is
        # the standalone path, unmodified.
        privacy_log.install_hosted_log_redaction()
        # query_log itself fails closed on content-private logging (D1.2), but
        # usage boost and the relevance check both read those journals -- with
        # them off, both features would otherwise loop forever finding nothing.
        os.environ["EXOMEM_DISABLE_USAGE_BOOST"] = "1"
        os.environ["EXOMEM_DISABLE_RELEVANCE_CHECK"] = "1"
    else:
        # An installed package lives under site-packages, so python-dotenv's
        # implicit caller-relative search misses the service working
        # directory. The documented repo-root .env is explicitly cwd-relative
        # for both checkout and wheel installs. It is never read from inside a
        # vault, whose files remote writers can plant.
        dotenv_path = working_directory_dotenv()
        if dotenv_path is not None:
            load_dotenv_func(dotenv_path=dotenv_path, override=True)
    env_compat.promote_legacy()

    vault_root = resolve_vault()
    from . import state_migration

    state_migration.require_vault_state_ready(vault_root)
    source_schema = schema.load_source_schema(vault_root)
    log.info("vault=%s source_types=%s", vault_root, source_schema.source_types)

    project_keys_hint = project_keys.keys_hint(vault_root)
    try:
        projection_runtime.preactivate_projection_runtime(vault_root)
    except projection_runtime.ProjectionRuntimeUnavailable:
        # The projected content boundary stays closed while owner repair serves.
        log.warning("governed projected retrieval is unavailable")
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

    def __init__(
        self,
        vault_root: Path,
        *,
        fallback_seconds: float = 5.0,
        deferred: bool = False,
    ) -> None:
        from . import readiness, warmup

        # A standby owns nothing until promotion, so it neither claims request
        # admission nor arms the liveness fallback that would start schedulers
        # beside the worker still serving (`seamless-managed-worker-handoff` D7).
        self._deferred = deferred
        if warmup.warmup_enabled() and not deferred:
            readiness.manage_runtime()
        self.vault_root = vault_root
        self.fallback_seconds = fallback_seconds
        self._lock = threading.Lock()
        self._started = False
        self._shutdown = threading.Event()
        self._timer: threading.Timer | None = None
        self._thread: threading.Thread | None = None
        self.media_worker: Any | None = None
        self.file_watcher: Any | None = None
        self.derived_drain: Any | None = None
        self.semantic_drain: Any | None = None
        self.vocabulary_recovery: Any | None = None
        self.catalogue_recovery: threading.Thread | None = None
        self._catalogue_stop = threading.Event()
        # The recovery watcher runs until told to stop, so it has its own stop
        # event: every path that stops the other workers stops it too.
        self._vocabulary_stop = threading.Event()
        self.recall_reembed: Any | None = None
        self.retrieval_warmup: threading.Thread | None = None
        self.matrix_warmup: threading.Thread | None = None
        self.dreamer: Any | None = None
        self.graph_drain: threading.Thread | None = None
        self.graph_cleanup_attempted = False
        # Set when this process builds an HTTP app, the only thing that serves
        # `/upload/sessions`; a stdio server never resumes their commits.
        self.serves_upload_sessions = False
        if not deferred:
            self._bind_graph_rebuild_lifetime()
            self._commit_recall_reembed()

    def _bind_graph_rebuild_lifetime(self) -> None:
        from . import graph_sync, working_set_runtime
        from .writer_lease import active_manager

        state = active_manager()._mutation_coordinator_for(self.vault_root).state_root
        graph_sync.rebuild_coordinator(self.vault_root, state_root=state).bind_shutdown(self._shutdown)
        working_set_runtime.bind_shutdown(self.vault_root, self._shutdown)

    def release(self) -> None:
        """Hand this process the ownership a standby refused, then activate."""
        from . import readiness, warmup

        with self._lock:
            was_deferred = self._deferred
            if was_deferred:
                self._bind_graph_rebuild_lifetime()
                self._commit_recall_reembed()
            self._deferred = False
        if was_deferred and warmup.warmup_enabled():
            readiness.manage_runtime()
        self.start()

    def start(self) -> None:
        """Launch local workers once; safe from health and timer races."""
        with self._lock:
            if self._started or self._shutdown.is_set() or self._deferred:
                return
            self._started = True
            timer = self._timer
            thread = threading.Thread(
                target=self._activate,
                name="exomem-local-activation",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        if timer is not None:
            timer.cancel()

    def _activate(self) -> None:
        if self._shutdown.is_set():
            return
        self._start_component("derived drain", self._start_derived_drain)
        if self._shutdown.is_set():
            self._stop_background_workers()
            return
        self._start_component("file watcher", self._start_file_watcher)
        if self._shutdown.is_set():
            self._stop_background_workers()
            return
        if self.file_watcher is None:
            # A maintained projection is authoritative only while something is
            # actually maintaining it.  The explicit watcher-off mode and a
            # watcher startup failure retain the supported walk-backed path.
            self._downgrade_recall_runtime()
        self._wait_for_recall_seed()
        if self._shutdown.is_set():
            self._stop_background_workers()
            return
        self._start_component("retrieval", self._start_compute_runtime)
        if self._shutdown.is_set():
            self._stop_background_workers()
            return
        self._start_component("projector refresh", refresh_obsolete_projector)
        if self._shutdown.is_set():
            self._stop_background_workers()
            return
        self._start_component("catalogue recovery", self._start_catalogue_recovery)
        if self._shutdown.is_set():
            self._stop_background_workers()
            return
        self._wait_for_required_admission()
        if self._shutdown.is_set():
            self._stop_background_workers()
            return
        self._start_component("semantic drain", self._start_semantic_drain)
        if self._shutdown.is_set():
            self._stop_background_workers()
            return
        # The request path, warmed by one internal turn before the startup
        # drain: a quiet-mode standby skips its cache warm, so without this the
        # first real turn after a promotion pays the cold resolver, page and
        # index handles itself.
        self._start_component("request-path warm", _warm_request_path)
        if self._shutdown.is_set():
            self._stop_background_workers()
            return
        self._start_component(
            "file watcher recovery",
            self._finish_file_watcher_startup,
        )
        if self._shutdown.is_set():
            self._stop_background_workers()
            return
        # Lazily, once the drain is done and off this thread: nothing below
        # waits on a matrix load, and the reaper still frees it when idle.
        self.matrix_warmup = threading.Thread(
            target=_warm_embedding_matrix,
            args=(self.vault_root,),
            name="exomem-matrix-warm",
            daemon=True,
        )
        try:
            self.matrix_warmup.start()
        except Exception:
            self.matrix_warmup = None
            raise
        starters = (
            ("graph drain", self._start_graph_drain),
            ("media", self._start_media_worker),
            ("upload sessions", self._resume_upload_sessions),
            ("vocabulary recovery", self._start_vocabulary_recovery),
            ("recall re-embed", self._start_recall_reembed),
            # Last: upkeep is the least important background work and must
            # never contend with admission, the watcher or graph convergence.
            ("dreamer", self._start_dreamer),
        )
        for label, starter in starters:
            if self._shutdown.is_set():
                break
            self._start_component(label, starter)
        if not self._shutdown.is_set() and self.file_watcher is not None:
            self.file_watcher.arm_optional_recovery()
        if self._shutdown.is_set():
            self._stop_background_workers()

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
        while not self._shutdown.is_set():
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

    def _stop_background_workers(self) -> None:
        """Stop workers already owned by this activation during shutdown."""
        from . import working_set_runtime

        working_set_runtime.stop_builds(self.vault_root)
        with self._lock:
            # The retry lock prevents any later repair scheduling after stop.
            self._catalogue_stop.set()
        if self.graph_drain is not None:
            from . import graph_drain

            graph_drain.stop(worker=self.graph_drain)
        semantic_failure = None
        for label, worker in (
            ("semantic drain", self.semantic_drain),
            ("derived drain", self.derived_drain),
            ("media", self.media_worker),
            ("file watcher", self.file_watcher),
        ):
            stop = getattr(worker, "stop", None)
            if not callable(stop):
                continue
            try:
                stop()
            except Exception as error:  # noqa: BLE001 - shutdown still has to join activation
                if label == "semantic drain":
                    semantic_failure = error
                log.warning("%s runtime shutdown failed", label, exc_info=True)
        from . import dreamer

        # Hosting covers a worker the setting started after this activation.
        if self.dreamer is not None or dreamer.hosting():
            try:
                _stop_dreamer()
            except Exception:  # noqa: BLE001 - shutdown still has to join activation
                log.warning("dreamer runtime shutdown failed", exc_info=True)
        self._vocabulary_stop.set()
        self._join_vocabulary_recovery()
        if self.graph_drain is not None and self.graph_drain.is_alive():
            raise RuntimeError("graph drain is still running during shutdown")
        if semantic_failure is not None:
            raise RuntimeError("semantic drain is still running during shutdown") from semantic_failure

    def _join_vocabulary_recovery(self) -> None:
        """Join the recovery watcher, bounded; it exits within one poll."""
        thread = self.vocabulary_recovery
        if not isinstance(thread, threading.Thread) or thread is threading.current_thread():
            return
        thread.join(timeout=VOCABULARY_WATCHER_JOIN_SECONDS)
        if thread.is_alive():
            log.warning("vocabulary recovery watcher did not stop before its deadline")

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

    def _start_compute_runtime(self, vault_root: Path) -> None:
        _start_compute_runtime(vault_root, owner=self)

    def _join_native_workers(self) -> None:
        """Finish owned native work after activation can no longer assign workers."""
        from . import working_set_runtime

        for thread in (self.retrieval_warmup, self.matrix_warmup, self.recall_reembed):
            if isinstance(thread, threading.Thread) and thread is not threading.current_thread():
                thread.join()
        working_set_runtime.join_builds(self.vault_root)
        join = getattr(self.semantic_drain, "join", None)
        if callable(join):
            join()

    def _start_graph_drain(self, vault_root: Path) -> None:
        self.graph_drain = _start_graph_drain(vault_root)

    def _resume_upload_sessions(self, vault_root: Path) -> None:
        """Sweep upload sessions and resume interrupted commits, in the serving HTTP runtime only."""
        if not self.serves_upload_sessions:
            return
        from .server_transfer import resume_upload_sessions

        resume_upload_sessions(vault_root)

    def _start_derived_drain(self, vault_root: Path) -> None:
        self.derived_drain = _start_derived_drain(vault_root)

    def _start_semantic_drain(self, vault_root: Path) -> None:
        from . import semantic_drain

        self.semantic_drain = semantic_drain.start(vault_root)

    def _start_catalogue_recovery(self, vault_root: Path) -> None:
        """Own pending catalogue retries before admission and throughout serving."""
        with self._lock:
            if (
                self.catalogue_recovery is not None
                or self._shutdown.is_set()
                or self._catalogue_stop.is_set()
            ):
                return
            thread = threading.Thread(
                target=self._recover_catalogue,
                args=(vault_root.resolve(),),
                name="exomem-catalogue-recovery",
                daemon=True,
            )
            thread.start()
            self.catalogue_recovery = thread

    def _recover_catalogue(self, vault_key: Path) -> None:
        from . import lexstore

        backoff = CATALOGUE_RECOVERY_FIRST_BACKOFF_SECONDS
        while not self._catalogue_stop.wait(backoff):
            with self._lock:
                if self._shutdown.is_set() or self._catalogue_stop.is_set():
                    return
                try:
                    pending = lexstore.retry_pending_repair(vault_key)
                except Exception:  # noqa: BLE001 - retry ownership must survive transient failure
                    log.warning("catalogue recovery retry failed", exc_info=True)
                    pending = True
            backoff = (
                min(backoff * 2, CATALOGUE_RECOVERY_MAX_BACKOFF_SECONDS)
                if pending
                else CATALOGUE_RECOVERY_FIRST_BACKOFF_SECONDS
            )

    def _start_vocabulary_recovery(self, vault_root: Path) -> None:
        """Drain queued recovery in the background, not on a client review call."""
        thread = threading.Thread(
            target=watch_vocabulary_recovery,
            args=(vault_root, self._vocabulary_stop),
            name="exomem-vocabulary-recovery",
            daemon=True,
        )
        thread.start()
        self.vocabulary_recovery = thread

    def _commit_recall_reembed(self) -> None:
        """The re-embed job starts last of the workers; until it plans, recall
        on a vault no build has covered is marked warming."""
        from . import recall_migration

        recall_migration.will_start(self.vault_root)

    def _start_recall_reembed(self, vault_root: Path) -> None:
        """Bring the recall sidecar into the recall encoder's space, off-request.

        Stops between batches when the process shuts down; a restart resumes.
        """
        from . import recall_migration

        self.recall_reembed = recall_migration.start(vault_root, self._shutdown)

    def _start_file_watcher(self, vault_root: Path) -> None:
        self.file_watcher = _start_file_watcher(vault_root)

    def _start_dreamer(self, vault_root: Path) -> None:
        self.dreamer = _start_dreamer(vault_root)

    def _finish_file_watcher_startup(self, _vault_root: Path) -> None:
        watcher = self.file_watcher
        finish = getattr(watcher, "finish_startup_recovery", None)
        if callable(finish):
            finish()

    async def _finish_shutdown(self) -> None:
        """Finish the tracked shutdown before process teardown."""
        with self._lock:
            self._shutdown.set()
            timer = self._timer
            self._timer = None
            thread = self._thread
        if timer is not None:
            timer.cancel()
        shutdown_error = None
        try:
            await anyio.to_thread.run_sync(self._stop_background_workers)
        except RuntimeError as error:
            shutdown_error = error
        if thread is not None and thread is not threading.current_thread():
            await anyio.to_thread.run_sync(thread.join)
        # A starter may have assigned its worker after the first stop.
        try:
            await anyio.to_thread.run_sync(self._stop_background_workers)
        except RuntimeError as error:
            shutdown_error = error
        # The supervisor owns the outer deadline; Python cannot tear down
        # while a daemon still owns a native forward or its model handle.
        await anyio.to_thread.run_sync(self._join_native_workers)
        # Recheck the stopped owners after the final join: a timed stop
        # that subsequently finished is no longer a shutdown failure.
        try:
            await anyio.to_thread.run_sync(self._stop_background_workers)
        except RuntimeError as error:
            shutdown_error = error
        else:
            shutdown_error = None
        # Activation may have started the watcher after the stop above;
        # it saw the stop event already set, so this join is short.
        self._vocabulary_stop.set()
        await anyio.to_thread.run_sync(self._join_vocabulary_recovery)
        from . import graph_sync

        if not self._deferred:
            self.graph_cleanup_attempted = True
            drained = await anyio.to_thread.run_sync(
                lambda: graph_sync.drain_active_rebuilds(timeout=5.0)
            )
            if not drained:
                shutdown_error = RuntimeError("graph rebuilds are still running during shutdown")
        from . import service_standby, writer_lease

        if not self._deferred:
            # After every worker that writes has stopped, so the final flush cannot move the head.
            try:
                await anyio.to_thread.run_sync(writer_lease.stop_server_lifecycle)
            except Exception:  # noqa: BLE001 - an unreleased lease expires on its TTL; the rest of shutdown goes on
                log.warning("writer lease handback at stop failed", exc_info=True)
        # A discarded standby must not leave the catalogue it built
        # behind; a no-op for any worker that is not an unpromoted one.
        # Off the loop: it may wait, bounded, for a build to stop, and
        # shielded so a cancelled shutdown still finishes the removal.
        await anyio.to_thread.run_sync(service_standby.discard)
        if shutdown_error is not None:
            raise shutdown_error

    def lifespan(self):
        """Arm a fallback for clients that never call the liveness endpoint."""
        from fastmcp.server.lifespan import lifespan

        @lifespan
        async def _lifespan(_server):
            timer = threading.Timer(self.fallback_seconds, self.start)
            timer.daemon = True
            with self._lock:
                if not self._started and not self._deferred:
                    self._timer = timer
                    timer.start()
            try:
                yield {}
            finally:
                with anyio.CancelScope(shield=True):
                    # Retain cleanup through repeated parent cancellation until owned workers finish.
                    cleanup = asyncio.create_task(self._finish_shutdown())
                    cancellation = None
                    while True:
                        try:
                            await asyncio.shield(cleanup)
                            break
                        except asyncio.CancelledError as error:
                            if cancellation is None:
                                cancellation = error
                            if cleanup.done():
                                cleanup.result()
                                break
                    if cancellation is not None:
                        raise cancellation

        return _lifespan


#: How long the activation sequence waits for the graph projection to become
#: current before giving up on this pass, and how many bounded recovery passes
#: it will run (`seamless-managed-worker-handoff` D12).
VOCABULARY_DRAIN_WAIT_SECONDS = 120.0
VOCABULARY_DRAIN_PASSES = 256
#: How long a publication-triggered drain waits for the projection to report
#: current once the published snapshot is readable.
VOCABULARY_REDRAIN_READY_SECONDS = 10.0
#: The signal fires after the commit or swap, but a snapshot can still be slow
#: to prove readable. Each readiness probe can walk the vault, so one
#: publication probes at most `VOCABULARY_REDRAIN_MAX_POLLS` times, backing off
#: from one second, doubling to a one-minute cap: about two minutes, eight
#: probes. After that only the next publication wakes the watcher.
VOCABULARY_REDRAIN_MAX_POLLS = 8
VOCABULARY_REDRAIN_FIRST_BACKOFF_SECONDS = 1.0
VOCABULARY_REDRAIN_MAX_BACKOFF_SECONDS = 60.0
#: How long shutdown waits for the watcher. It polls its stop event every
#: quarter second, so this bounds only a recovery pass already in flight.
VOCABULARY_WATCHER_JOIN_SECONDS = 5.0


def drain_vocabulary_recovery(
    vault_root: Path,
    shutdown: threading.Event,
    *,
    wait_seconds: float = VOCABULARY_DRAIN_WAIT_SECONDS,
    max_passes: int = VOCABULARY_DRAIN_PASSES,
) -> int:
    """Drain queued vocabulary recovery once the graph projection is current.

    Rows queue while the projection warms, and until now only an explicit client
    review call emptied them — on the personal service thirty accumulated across
    one upgrade. Each pass is the existing bounded four-job window; the loop
    stops as soon as a pass processes nothing, when the projection never becomes
    current, or when the runtime shuts down. Returns the number of jobs drained.
    """
    from . import graph_sync, vocabulary_delivery
    from .governance.principal import library_scope

    deadline = time.monotonic() + wait_seconds
    while not shutdown.is_set():
        try:
            current = graph_sync.status(vault_root)["state"] == "current"
        except Exception:  # noqa: BLE001 - a background drain never breaks activation
            current = False
        if current:
            break
        if time.monotonic() >= deadline:
            log.info("vocabulary recovery drain skipped; graph projection is not current")
            return 0
        shutdown.wait(0.25)
    drained = 0
    for _ in range(max_passes):
        if shutdown.is_set():
            break
        try:
            with library_scope():
                result = vocabulary_delivery.recover(vault_root)
        except Exception:  # noqa: BLE001 - recovery retries on the next activation
            log.warning("vocabulary recovery pass failed", exc_info=True)
            break
        processed = result.get("processed")
        processed = processed if isinstance(processed, int) and processed > 0 else 0
        drained += processed
        if not processed:
            break
    if drained:
        log.info("drained %d queued vocabulary recovery job(s)", drained)
    return drained


#: The internal warm turn. Plain words the lexical, semantic and recent
#: stages all see; it names nothing, so no anchor resolves and no heat is kept.
_REQUEST_PATH_WARM_TURN = "what changed recently and what is still open"


def _warm_request_path(vault_root: Path) -> None:
    """Run one activation for its caches only: no log, no session, no heat.

    Calls the activation body directly, so the activation log, the upkeep
    carrier and the continuity carry-through never see it; with no session,
    anchor or caller it records no heat and no episode nudge. Never raises.
    """
    from . import warmup

    if not warmup.warmup_enabled():
        return
    started = time.perf_counter()
    try:
        from . import commands, working_set

        commands._op_activate_context_body(  # noqa: SLF001 - deliberately log-free
            vault_root,
            _REQUEST_PATH_WARM_TURN,
            working_set.DEFAULT_BUDGET_CHARS,
            purpose=None,
            continuity=None,
            anchor=None,
            include_timings=False,
            client=None,
            session=None,
            workspace=None,
        )
    except Exception:  # noqa: BLE001 - a warm is never load-bearing
        log.info("request-path warm skipped", exc_info=True)
        return
    log.info("request-path warm done in %.0f ms", (time.perf_counter() - started) * 1000.0)


def _warm_embedding_matrix(vault_root: Path) -> None:
    """Warm a retained corpus matrix without scanning the disk-only profile."""
    from . import warmup

    if not warmup.warmup_enabled() or os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return
    started = time.perf_counter()
    try:
        from . import cloud_cell

        # service-v1 retains no matrix; warming repeats a complete disk scan
        # while foreground recall is already serving.
        if cloud_cell.resource_policy() == "service-v1":
            return
        from . import embeddings

        # The load a cold recall starts: it reads off the index lock, so a
        # recall meanwhile reports the dense lane warming and never waits.
        embeddings.get_embedding_index(vault_root).start_corpus_matrix_load().join()
    except Exception:  # noqa: BLE001 - a warm is never load-bearing
        log.info("embedding matrix warm skipped", exc_info=True)
        return
    # The load logs its own outcome; a failed one is not "done".
    log.info("embedding matrix warm finished in %.0f ms", (time.perf_counter() - started) * 1000.0)


def redrain_backoffs(
    *,
    first_backoff: float = VOCABULARY_REDRAIN_FIRST_BACKOFF_SECONDS,
    max_backoff: float = VOCABULARY_REDRAIN_MAX_BACKOFF_SECONDS,
    max_polls: int = VOCABULARY_REDRAIN_MAX_POLLS,
) -> tuple[float, ...]:
    """The waits between one publication's readiness probes."""
    delays: list[float] = []
    delay = first_backoff
    for _ in range(max(0, max_polls - 1)):
        delays.append(min(delay, max_backoff))
        delay *= 2
    return tuple(delays)


def redrain_after_publish(
    vault_root: Path,
    shutdown: threading.Event,
    *,
    ready_seconds: float = VOCABULARY_REDRAIN_READY_SECONDS,
    wake: threading.Event | None = None,
    first_backoff: float = VOCABULARY_REDRAIN_FIRST_BACKOFF_SECONDS,
    max_backoff: float = VOCABULARY_REDRAIN_MAX_BACKOFF_SECONDS,
    max_polls: int = VOCABULARY_REDRAIN_MAX_POLLS,
) -> int | None:
    """Drain once after a graph publication, when there is work and it can land.

    Returns None when nothing is queued. Otherwise probes, with backoff and a
    bounded count, for the read snapshot the publication made, then runs the
    same bounded drain activation runs. A later publication (`wake`) ends the
    current backoff and restarts the schedule, since it is a new snapshot to
    wait for. Claims are compare-and-set, so this never repeats a job the
    activation drain or an explicit review already completed.
    """
    from . import epistemic_graph

    if not vocabulary_recovery.page(vault_root, limit=1):
        return None
    graph = epistemic_graph.EpistemicGraphIndex(vault_root)
    schedule = redrain_backoffs(
        first_backoff=first_backoff, max_backoff=max_backoff, max_polls=max_polls
    )
    step = 0
    while not shutdown.is_set():
        try:
            readable = graph.available()
        except Exception:  # noqa: BLE001 - a background drain never breaks the runtime
            readable = False
        if readable:
            return drain_vocabulary_recovery(vault_root, shutdown, wait_seconds=ready_seconds)
        if step >= len(schedule):
            return 0
        if _wait_for_backoff(schedule[step], shutdown, wake):
            step = 0
        else:
            step += 1
    return 0


def _wait_for_backoff(
    seconds: float, shutdown: threading.Event, wake: threading.Event | None
) -> bool:
    """Wait out one backoff; True when a new publication ended it early."""
    deadline = time.monotonic() + seconds
    while not shutdown.is_set():
        if wake is not None and wake.is_set():
            wake.clear()
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        shutdown.wait(min(0.25, remaining))
    return False


def watch_vocabulary_recovery(vault_root: Path, shutdown: threading.Event) -> None:
    """Drain at activation, then again after every graph publication.

    Graph churn withdraws the read snapshot a write's guidance needs, which
    strands its recovery job until something drains the queue. Publications
    only set an event, so many coalesce into one drain and none waits for it.
    """
    published = vocabulary_recovery.publication_signal(vault_root)
    try:
        drain_vocabulary_recovery(vault_root, shutdown)
        while not shutdown.is_set():
            if not published.wait(timeout=0.5):
                continue
            published.clear()
            try:
                redrain_after_publish(vault_root, shutdown, wake=published)
            except Exception:  # noqa: BLE001 - the next publication retries
                log.warning("vocabulary recovery redrain failed", exc_info=True)
    finally:
        vocabulary_recovery.release_publication_signal(vault_root, published)


def refresh_obsolete_projector(vault_root: Path) -> str:
    """Refresh an obsolete projector namespace as the owner, under the writer lease."""
    from .governance import projector_refresh
    from .writer_lease import get_manager

    with get_manager().mutation_guard(
        vault_root, operation="projector_refresh", holder_kind="background"
    ):
        return projector_refresh.converge(vault_root)


def _start_metrics_persistence() -> None:
    """Restore the metrics registry from its prior snapshot and start the
    background snapshotter against the writer-lease state directory.

    Best-effort: metrics persistence is not part of server readiness, so any
    failure here (an unreadable snapshot, a lease-config error) is logged and
    swallowed rather than blocking startup.
    """
    from . import service_standby

    if service_standby.in_standby():
        # A standby publishes nothing, not even its own metrics snapshots, while
        # the worker that owns the state directory is still serving.
        return
    try:
        from .writer_lease import get_manager

        state_dir = get_manager().config.state_dir
        metrics.load_snapshot_once(state_dir)
        metrics.start_snapshotter(state_dir, metrics.snapshot_interval_seconds_from_env())
    except Exception as exc:  # noqa: BLE001 - metrics startup must never break the server
        log.warning("metrics persistence unavailable at startup: %s", exc)


def _start_retrieval_runtime(
    vault_root: Path, *, stop: threading.Event | None = None,
) -> threading.Thread | None:
    """Start core catalog/cache warm-up independently of optional models."""
    from . import warmup

    if warmup.warmup_enabled():
        if os.environ.get("EXOMEM_EAGER_BOOT"):
            from . import foreground_priority, readiness, runtime_resources

            readiness.begin_warm()
            try:
                with (
                    foreground_priority.cancellable(stop),
                    runtime_resources.model_work("bulk", cancel_event=stop),
                ):
                    warmup.warm_all(vault_root)
            except foreground_priority.BulkCancelled:
                pass
            finally:
                readiness.finish_warm()
        else:
            return warmup.start_background(vault_root, stop)
    return None


def _start_compute_runtime(
    vault_root: Path, *, owner: LocalRuntimeActivation | None = None,
) -> None:
    """Start retrieval warm-up, model unloading, and live compute-mode watching."""
    from . import mode

    log.info("compute policy: %s", mode.resolved())
    warm_thread = _start_retrieval_runtime(
        vault_root, stop=owner._shutdown if owner is not None else None,
    )
    # Ownership lands before optional compute setup can fail or another warm can start.
    if owner is not None:
        owner.retrieval_warmup = warm_thread

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


def _start_dreamer(vault_root: Path) -> Any | None:
    """Start the default-off upkeep worker, or nothing.

    Local service only. A hosted cell never reaches this starter, and a cloud
    cell declines here: both would need their own worker registration and
    review before a background proposal worker runs for a tenant.
    """
    from . import cloud_cell, dreamer

    if cloud_cell.cloud_mode_enabled():
        return None
    try:
        return dreamer.start(vault_root)
    except Exception as exc:  # noqa: BLE001 - upkeep must never break startup
        log.warning("dreamer start failed: %s", exc)
        return None


def _stop_dreamer() -> None:
    from . import dreamer

    dreamer.stop(timeout=2.0)


def _start_derived_drain(vault_root: Path) -> Any | None:
    """Start exact component convergence independently of the file watcher.

    The three production callbacks the drain needs -- the component router, the
    canonical-generation observer and Lane 2's pending publisher -- are the
    drain module's own defaults, so the server keeps naming only the cell it is
    starting and there is exactly one place that knows what production wiring
    is.
    """
    from . import derived_drain

    try:
        return derived_drain.start(vault_root)
    except Exception as exc:  # noqa: BLE001 - custody survives startup failure
        log.warning("derived component drain start failed: %s", exc)
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

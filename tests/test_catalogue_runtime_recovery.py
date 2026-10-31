from __future__ import annotations

import asyncio
import errno
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import initialize_vault_state_offline
from fastmcp import FastMCP

from exomem import freshness, lexstore, readiness, server_assets, server_runtime, warmup


@pytest.mark.parametrize("during_startup", [True, False])
def test_readiness_only_traffic_recovers_a_failed_catalogue_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during_startup: bool
) -> None:
    """A quiet replica must recover before admission and after it starts serving."""
    readiness.reset()
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "auto")
    monkeypatch.setattr(
        server_runtime, "CATALOGUE_RECOVERY_FIRST_BACKOFF_SECONDS", 0.05, raising=False
    )
    monkeypatch.setattr(
        server_runtime, "CATALOGUE_RECOVERY_MAX_BACKOFF_SECONDS", 0.1, raising=False
    )
    vault = tmp_path / "vault"
    page = vault / "Knowledge Base" / "probe.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Probe\n\ninitialmarker\n", encoding="utf-8")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    initialize_vault_state_offline(vault, source="catalogue recovery fixture")
    for scope in freshness.SCOPES:
        freshness.seed(vault, scope, [(str(page), freshness.stat_signature(page))])
    lexstore.ensure_fresh(vault)
    store = lexstore.get_store(vault)
    # This exercises a failed whole-corpus publication. An exact delta replay
    # would answer the one-page lag below without publishing at all.
    monkeypatch.setattr(store, "catch_up_lagging_delta", lambda: False)
    publication_attempts: list[Path] = []
    allow_publication = threading.Event()
    compute_finished = threading.Event()
    publication_failed = threading.Event()
    admitted = threading.Event()
    real_move = lexstore.reserved_paths._move_owner_file

    def publish(root, src, src_descriptor, dst, dst_descriptor, **kwargs):
        if Path(dst) == store.path and ".rebuild-" in Path(src).name:
            assert Path(src).is_file(), "failure must follow the completed build"
            publication_attempts.append(Path(src))
            if len(publication_attempts) == 1:
                publication_failed.set()
                raise OSError(errno.ENOSPC, "injected publication failure")
            assert allow_publication.wait(3), "the test never released publication"
        return real_move(root, src, src_descriptor, dst, dst_descriptor, **kwargs)

    monkeypatch.setattr(lexstore.reserved_paths, "_move_owner_file", publish)

    def compute(root, **_kwargs):
        readiness.begin_warm()
        assert warmup.warm_retrieval_catalog(root) is not during_startup
        assert lexstore.await_repairs_idle(root, timeout=3)
        readiness.finish_warm()
        compute_finished.set()

    # Seed authority belongs to this fixture; no optional watcher runs.
    monkeypatch.setattr(
        server_runtime.LocalRuntimeActivation, "_downgrade_recall_runtime", lambda self: None
    )
    monkeypatch.setattr(server_runtime, "_start_compute_runtime", compute)
    for name in (
        "_start_derived_drain",
        "_start_file_watcher",
        "_start_graph_drain",
        "_start_media_worker",
        "_warm_request_path",
        "_warm_embedding_matrix",
    ):
        monkeypatch.setattr(server_runtime, name, lambda _root: None)
    for name in (
        "_start_vocabulary_recovery",
        "_start_recall_reembed",
        "_start_dreamer",
        "_resume_upload_sessions",
    ):
        monkeypatch.setattr(
            server_runtime.LocalRuntimeActivation, name, lambda self, _root: None
        )
    monkeypatch.setattr(
        server_runtime.LocalRuntimeActivation,
        "_start_semantic_drain",
        lambda self, _root: admitted.set(),
    )
    activation = server_runtime.LocalRuntimeActivation(vault, fallback_seconds=60)

    def change_page():
        page.write_text("# Probe\n\nrecoverymarker changed content\n", encoding="utf-8")
        freshness.on_files_changed(vault, changed=[page])

    if during_startup:
        change_page()

    async def exercise():
        app = FastMCP("catalogue-recovery")
        server_assets.register_health_routes(app)
        async with activation.lifespan()(SimpleNamespace()):
            activation.start()
            assert await asyncio.to_thread(compute_finished.wait, 3)
            assert activation.file_watcher is None
            if not during_startup:
                assert await asyncio.to_thread(admitted.wait, 3)
                change_page()
                assert not lexstore.runtime_retrieval_catalog_current(vault)
                assert await asyncio.to_thread(publication_failed.wait, 3)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.http_app()), base_url="http://cell.test"
            ) as client:
                lagging = await client.get("/health/ready")
                if during_startup:
                    # Never proved: a replacement still waits for a strict proof.
                    assert lagging.status_code == 503
                else:
                    # Proved, then lagging: routable while repair converges.
                    assert lagging.status_code == 200, lagging.json()
                    assert lagging.json()["retrieval"]["state"] == "lagging"
                allow_publication.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    response = await client.get("/health/ready")
                    if (
                        response.status_code == 200
                        and response.json()["retrieval"]["state"] == "ready"
                    ):
                        break
                    await asyncio.sleep(0.01)
                assert response.status_code == 200, response.json()
                assert response.json()["retrieval"]["state"] == "ready", response.json()
                assert await asyncio.to_thread(admitted.wait, 3)
                assert await asyncio.to_thread(lexstore.await_repairs_idle, vault, 3)
                assert len(publication_attempts) == 2
                assert len(
                    lexstore.search_bm25(vault, "recoverymarker", k=3, scope="kb", repair=False)
                    or []
                ) == 1

    try:
        asyncio.run(exercise())
    finally:
        allow_publication.set()
        assert lexstore.await_repairs_idle(vault, timeout=3)
        readiness.reset()
        freshness.clear()
        with lexstore._REPAIRS_LOCK:
            lexstore._FULL_REBUILD_REQUESTED.discard(vault.resolve())


def test_persistent_catalogue_failure_backs_off_without_duplicate_or_post_shutdown_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked repair stays single-flight; shutdown prevents further retries."""
    attempts: queue.Queue[int] = queue.Queue()
    ticks: queue.Queue[None] = queue.Queue()
    delays: queue.Queue[float] = queue.Queue()
    release = threading.Event()
    builds = 0

    class Store:
        def rebuild_atomic(self):
            nonlocal builds
            builds += 1
            attempts.put(builds)
            if builds == 2:
                assert release.wait(3)
            return False

    monkeypatch.setattr(lexstore, "get_store", lambda _root: Store())
    monkeypatch.setattr(server_runtime, "CATALOGUE_RECOVERY_FIRST_BACKOFF_SECONDS", 1.0)
    monkeypatch.setattr(server_runtime, "CATALOGUE_RECOVERY_MAX_BACKOFF_SECONDS", 4.0)
    activation = server_runtime.LocalRuntimeActivation(tmp_path, fallback_seconds=60)

    def wait(delay):
        delays.put(delay)
        while not activation._catalogue_stop.is_set():
            try:
                ticks.get(timeout=0.01)
                return activation._catalogue_stop.is_set()
            except queue.Empty:
                pass
        return True

    monkeypatch.setattr(activation._catalogue_stop, "wait", wait)
    lexstore._schedule_repair(tmp_path)
    assert attempts.get(timeout=1) == 1
    assert lexstore.await_repairs_idle(tmp_path, timeout=1)

    async def exercise():
        async with activation.lifespan()(SimpleNamespace()):
            activation._start_catalogue_recovery(tmp_path)
            assert delays.get(timeout=1) == 1.0
            ticks.put(None)
            assert attempts.get(timeout=1) == 2
            assert delays.get(timeout=1) == 2.0
            ticks.put(None)
            assert delays.get(timeout=1) == 4.0
            assert builds == 2, "a pending-only retry duplicated the blocked repair"
            release.set()
            assert lexstore.await_repairs_idle(tmp_path, timeout=1)
            for expected in (3, 4):
                ticks.put(None)
                assert attempts.get(timeout=1) == expected
                assert delays.get(timeout=1) == 4.0
                assert lexstore.await_repairs_idle(tmp_path, timeout=1)
        assert activation.catalogue_recovery is not None
        ticks.put(None)
        activation._start_catalogue_recovery(tmp_path)
        assert builds == 4, "shutdown scheduled another repair"

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        monitor = activation.catalogue_recovery
        if monitor is not None:
            monitor.join(timeout=1)
            assert not monitor.is_alive()
        assert lexstore.await_repairs_idle(tmp_path, timeout=3)
        with lexstore._REPAIRS_LOCK:
            lexstore._FULL_REBUILD_REQUESTED.discard(tmp_path.resolve())


def test_deferred_runtime_never_starts_catalogue_recovery(tmp_path: Path) -> None:
    activation = server_runtime.LocalRuntimeActivation(
        tmp_path, fallback_seconds=0.001, deferred=True
    )

    async def exercise():
        async with activation.lifespan()(SimpleNamespace()):
            activation.start()
            await asyncio.sleep(0.02)
            assert activation._thread is None
            assert activation.catalogue_recovery is None

    asyncio.run(exercise())

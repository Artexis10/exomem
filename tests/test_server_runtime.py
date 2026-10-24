from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import textwrap
import threading
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import initialize_vault_state_offline

from exomem import media_processing, readiness, server_runtime
from exomem.governance import projection_runtime


@pytest.mark.parametrize("cancel_shutdown", [False, True], ids=["normal", "cancel_shutdown"])
def test_owned_model_callbacks_finish_before_a_clean_subprocess_exit(tmp_path, cancel_shutdown):
    """Daemon inference callbacks must finish before lifecycle exit can tear Python down."""
    code = textwrap.dedent("""\
        import asyncio
        import sys
        import threading
        from pathlib import Path
        from types import SimpleNamespace
        from exomem import recall_migration, server_runtime, warmup, working_set_index, working_set_runtime

        root = Path(sys.argv[1])
        cancel_shutdown = sys.argv[2] == "1"
        root.mkdir()
        activation = server_runtime.LocalRuntimeActivation(root, deferred=True)
        working_set_runtime.bind_shutdown(root, activation._shutdown)
        entered = {name: threading.Event() for name in ("warm", "recall", "refresh")}
        release = threading.Event()
        completed = set()

        def model_callback(name):
            entered[name].set()
            assert release.wait(5), name
            assert activation._shutdown.is_set()
            completed.add(name)
            return {}

        warmup.warm_all = lambda root: model_callback("warm")
        recall_migration.run = lambda root, stop: model_callback("recall")
        working_set_index.WorkingSetIndex.update = lambda self, **kwargs: model_callback("refresh")

        async def exercise():
            lifetime = activation.lifespan()(SimpleNamespace())
            await lifetime.__aenter__()
            activation.retrieval_warmup = warmup.start_background(root, activation._shutdown)
            activation.recall_reembed = recall_migration.start(root, activation._shutdown)
            working_set_runtime._schedule_build(root)
            for event in entered.values():
                assert await asyncio.to_thread(event.wait, 3)
            exiting = asyncio.create_task(lifetime.__aexit__(None, None, None))
            while not activation._shutdown.is_set():
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.02)
            assert not exiting.done()
            if cancel_shutdown:
                assert exiting.cancel()
                await asyncio.sleep(0.02)
                assert not exiting.done()
                assert exiting.cancel()
                await asyncio.sleep(0.02)
                assert not exiting.done()
            release.set()
            try:
                await asyncio.wait_for(exiting, 3)
            except asyncio.CancelledError:
                assert cancel_shutdown
            else:
                assert not cancel_shutdown
            assert completed == set(entered)
            assert not activation.retrieval_warmup.is_alive()
            assert not activation.recall_reembed.is_alive()
            assert not any(t.name == "exomem-working-set-warm" for t in threading.enumerate())
            working_set_runtime._schedule_build(root)
            assert not any(t.name == "exomem-working-set-warm" for t in threading.enumerate())

        try:
            asyncio.run(exercise())
        finally:
            release.set()
        print("all owned callbacks joined")
        """)
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "worker"), "1" if cancel_shutdown else "0"],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "all owned callbacks joined" in result.stdout


def test_final_join_recovers_a_timed_semantic_stop(tmp_path, monkeypatch):
    """A timed stop is temporary when the final join proves the forward completed."""
    from exomem import semantic_drain

    activation = server_runtime.LocalRuntimeActivation(tmp_path, deferred=True)
    owner = semantic_drain.SemanticDrain(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def forward():
        entered.set()
        assert release.wait(5)
        finished.set()

    owner._bulk_thread = threading.Thread(target=forward, daemon=True)
    owner._bulk_thread.start()
    activation.semantic_drain = owner
    stop = owner.stop
    monkeypatch.setattr(owner, "stop", lambda: stop(timeout=0))

    async def exercise():
        lifetime = activation.lifespan()(SimpleNamespace())
        await lifetime.__aenter__()
        assert entered.wait(1)
        exiting = asyncio.create_task(lifetime.__aexit__(None, None, None))
        assert await asyncio.to_thread(owner._stop.wait, 1)
        await asyncio.sleep(0.02)
        assert not exiting.done()
        release.set()
        await asyncio.wait_for(exiting, 2)
        assert finished.is_set()
        assert not owner._bulk_thread.is_alive()

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        owner.join()


def test_stopped_warmup_finishes_its_forward_without_loading_the_next_model(tmp_path, monkeypatch):
    """An owned warmup must leave the native callback before it acknowledges cancellation."""
    from exomem import embeddings, recall_migration, semantic_contract, warmup

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    stop = threading.Event()
    later_loads = []

    class Model:
        def encode(self, _texts):
            entered.set()
            assert release.wait(5)
            finished.set()

    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(warmup, "model_preload_allowed", lambda *_args: True)
    monkeypatch.setattr(warmup, "warm_retrieval_catalog", lambda _root: True)
    monkeypatch.setattr(warmup, "warm_graph_handoff", lambda _root: {})
    monkeypatch.setattr(warmup, "warm_caches", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(warmup, "prime_writer_resolver", lambda _root: None)
    monkeypatch.setattr(semantic_contract, "warm_corpus_context", lambda _root: None)
    monkeypatch.setattr(embeddings, "get_model", lambda: Model())
    monkeypatch.setattr(recall_migration, "preload_serving_encoder",
                        lambda _root: later_loads.append("serving") or False)
    worker = warmup.start_background(tmp_path, stop)
    try:
        assert entered.wait(3)
        stop.set()
        assert worker.is_alive()
        release.set()
        worker.join(3)
        assert finished.is_set()
        assert not worker.is_alive()
        assert later_loads == []
        assert not readiness.is_warming()
    finally:
        stop.set()
        release.set()
        worker.join(3)
        readiness.reset()


def test_concurrent_warmup_cannot_replace_an_activations_owned_worker(tmp_path, monkeypatch):
    """A later compute failure must retain this activation's warm, even if another one starts."""
    from exomem import auto_quiet, mode, warmup

    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    monkeypatch.delenv("EXOMEM_EAGER_BOOT", raising=False)
    first = server_runtime.LocalRuntimeActivation(tmp_path / "first", deferred=True)
    second = server_runtime.LocalRuntimeActivation(tmp_path / "second", deferred=True)
    entered = threading.Event()
    later_setup = threading.Event()
    release_setup = threading.Event()
    release_first = threading.Event()
    workers = {}

    def warm(root, stop=None):
        worker = threading.Thread(target=release_first.wait if root == first.vault_root else lambda: None,
                                  daemon=True)
        workers[root] = worker
        worker.start()
        warmup._WARM_THREAD = worker
        return worker

    def config_watch():
        if threading.current_thread().name == "first-setup":
            entered.set()
            assert release_setup.wait(3)
            raise RuntimeError("optional setup failed")
        later_setup.set()

    monkeypatch.setattr(warmup, "start_background", warm)
    monkeypatch.setattr(mode, "release_when_idle", lambda: False)
    monkeypatch.setattr(mode, "start_config_watch", config_watch)
    monkeypatch.setattr(auto_quiet, "start_if_enabled", lambda: None)
    setup = threading.Thread(target=first._start_component,
                             args=("retrieval", first._start_compute_runtime), name="first-setup")
    setup.start()
    try:
        assert entered.wait(2)
        second._start_compute_runtime(second.vault_root)
        assert later_setup.is_set()
        release_setup.set()
        setup.join(2)
        assert not setup.is_alive()
        assert first.retrieval_warmup is workers[first.vault_root]
        assert second.retrieval_warmup is workers[second.vault_root]

        async def exercise():
            lifetime = first.lifespan()(SimpleNamespace())
            await lifetime.__aenter__()
            exiting = asyncio.create_task(lifetime.__aexit__(None, None, None))
            assert await asyncio.to_thread(first._shutdown.wait, 1)
            await asyncio.sleep(0.02)
            assert not exiting.done()
            release_first.set()
            await asyncio.wait_for(exiting, 2)
            assert not workers[first.vault_root].is_alive()

        asyncio.run(exercise())
    finally:
        release_setup.set()
        release_first.set()
        setup.join(3)
        for worker in workers.values():
            worker.join(3)


def test_unavailable_projection_startup_keeps_owner_governance_reachable_and_content_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands, writer_lease
    from exomem.governance import authorization_custody, principal
    from exomem.init import init_vault

    vault = tmp_path / "vault"
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    vault.mkdir()
    init_vault(vault)
    monkeypatch.setenv(authorization_custody.KEYRING_FILE_ENV, str(tmp_path / "missing-keyring"))
    monkeypatch.setattr(server_runtime, "_start_metrics_persistence", lambda: None)
    monkeypatch.setattr(server_runtime, "_start_retrieval_runtime", lambda _root, **_kwargs: None)
    monkeypatch.setattr(server_runtime, "_start_derived_drain", lambda _root: None)
    projection_runtime._clear_preactivated_runtimes_for_tests()

    runtime = server_runtime.initialize_runtime(load_dotenv_func=lambda **_kwargs: None)
    assert runtime.vault_root == vault
    assert projection_runtime.requires_fixed_projected_completion(vault)
    assert not projection_runtime.has_preactivated_projection_runtime(vault)
    command = next(
        command for command in commands.PRODUCT_COMMANDS if command.name == "govern_memory"
    )
    with principal.request_scope(principal.resolve_rest_principal(None)):
        governed = writer_lease.invoke_command(command, vault, operation="list")
        assert "rules" in governed and "scopes" in governed
        with pytest.raises(
            projection_runtime.ProjectionRuntimeUnavailable,
            match="^governed projected retrieval is unavailable$",
        ):
            commands.op_find(vault, query="private term", mode="keyword")


def test_initialize_runtime_loads_dotenv_from_service_working_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    initialize_vault_state_offline(vault, source="server runtime dotenv fixture")
    calls: list[tuple[object, bool]] = []

    def load_dotenv(*, dotenv_path, override):
        calls.append((dotenv_path, override))

    monkeypatch.setattr(server_runtime, "resolve_vault", lambda: vault)
    monkeypatch.setattr(
        server_runtime.schema,
        "load_source_schema",
        lambda _vault: SimpleNamespace(source_types=("session",)),
    )
    monkeypatch.setattr(server_runtime.project_keys, "keys_hint", lambda _vault: "")
    monkeypatch.setattr(server_runtime, "_start_compute_runtime", lambda _vault, **_kwargs: None)
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _vault: None)
    monkeypatch.setattr(server_runtime, "_start_file_watcher", lambda _vault: None)

    runtime = server_runtime.initialize_runtime(load_dotenv_func=load_dotenv)

    assert calls == [(tmp_path / ".env", True)]
    assert runtime.vault_root == vault


def test_initialize_runtime_disables_usage_boost_and_relevance_check_in_cloud_mode(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design D1.2: usage boost and the relevance check both read the query
    journals that `query_log` now turns off under content-private logging --
    without also disabling the features that read them, each would find its
    journals silently empty forever instead of being told why."""
    vault = tmp_path / "vault"
    vault.mkdir()
    initialize_vault_state_offline(vault, source="server runtime cloud fixture")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_USAGE_BOOST", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_RELEVANCE_CHECK", raising=False)
    monkeypatch.setattr(server_runtime, "resolve_vault", lambda: vault)
    monkeypatch.setattr(
        server_runtime.schema,
        "load_source_schema",
        lambda _vault: SimpleNamespace(source_types=("session",)),
    )
    monkeypatch.setattr(server_runtime.project_keys, "keys_hint", lambda _vault: "")
    monkeypatch.setattr(server_runtime, "_start_compute_runtime", lambda _vault, **_kwargs: None)
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _vault: None)
    monkeypatch.setattr(server_runtime, "_start_file_watcher", lambda _vault: None)

    server_runtime.initialize_runtime(load_dotenv_func=lambda **_kwargs: None)

    assert os.environ["EXOMEM_DISABLE_USAGE_BOOST"] == "1"
    assert os.environ["EXOMEM_DISABLE_RELEVANCE_CHECK"] == "1"


def test_initialize_runtime_does_not_start_workers_before_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    initialize_vault_state_offline(vault, source="server runtime transport fixture")
    events: list[str] = []

    monkeypatch.setattr(server_runtime, "resolve_vault", lambda: vault)
    monkeypatch.setattr(
        server_runtime.schema,
        "load_source_schema",
        lambda _vault: SimpleNamespace(source_types=("session",)),
    )
    monkeypatch.setattr(server_runtime.project_keys, "keys_hint", lambda _vault: "")
    monkeypatch.setattr(server_runtime, "_start_metrics_persistence", lambda: None)
    monkeypatch.setattr(
        projection_runtime,
        "preactivate_projection_runtime",
        lambda root: events.append(f"projection:{root}"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_compute_runtime",
        lambda root, **_kwargs: events.append(f"compute:{root}"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_media_worker",
        lambda root: events.append(f"media:{root}"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_file_watcher",
        lambda root: events.append(f"watcher:{root}"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_graph_drain",
        lambda root: events.append(f"graph:{root}"),
    )

    server_runtime.initialize_runtime(load_dotenv_func=lambda **_kwargs: None)

    assert events == [f"projection:{vault}"]


def test_initialize_runtime_requires_ready_state_before_any_state_consumer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import state_migration

    vault = tmp_path / "vault"
    vault.mkdir()
    events: list[str] = []
    monkeypatch.setattr(server_runtime, "resolve_vault", lambda: vault)
    monkeypatch.setattr(
        state_migration,
        "require_vault_state_ready",
        lambda root: events.append(f"ready:{root}"),
        raising=False,
    )
    monkeypatch.setattr(
        server_runtime.schema,
        "load_source_schema",
        lambda root: (
            events.append(f"schema:{root}"),
            SimpleNamespace(source_types=("session",)),
        )[1],
    )
    monkeypatch.setattr(
        server_runtime.project_keys,
        "keys_hint",
        lambda root: (events.append(f"keys:{root}"), "")[1],
    )
    monkeypatch.setattr(
        projection_runtime,
        "preactivate_projection_runtime",
        lambda root: events.append(f"projection:{root}"),
    )
    monkeypatch.setattr(server_runtime, "_start_metrics_persistence", lambda: None)

    server_runtime.initialize_runtime(load_dotenv_func=lambda **_kwargs: None)

    assert events == [
        f"ready:{vault}",
        f"schema:{vault}",
        f"keys:{vault}",
        f"projection:{vault}",
    ]


def test_local_runtime_activation_waits_for_liveness_and_starts_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []
    compute_started = threading.Event()
    seed_started = threading.Event()
    release_seed = threading.Event()
    activated = threading.Event()

    class Watcher:
        def wait_until_seeded(self, timeout=None) -> bool:
            calls.append("watcher-seed:wait")
            seed_started.set()
            assert release_seed.wait(timeout=timeout or 1.0)
            calls.append("watcher-seed:ready")
            return True

        def finish_startup_recovery(self) -> None:
            calls.append("watcher-recovery")

    def start_compute(root, **_kwargs) -> None:
        calls.append(f"compute:{root}")
        readiness.begin_warm()
        compute_started.set()

    monkeypatch.setattr(
        server_runtime,
        "_start_compute_runtime",
        start_compute,
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_file_watcher",
        lambda root: (calls.append(f"watcher:{root}"), Watcher())[1],
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_graph_drain",
        lambda root: calls.append(f"graph:{root}"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_media_worker",
        lambda root: (calls.append(f"media:{root}"), activated.set())[0],
    )

    activation = server_runtime.LocalRuntimeActivation(vault, fallback_seconds=60.0)

    async def exercise() -> None:
        assert calls == []
        async with activation.lifespan()(SimpleNamespace()):
            assert calls == []
            activation.start()
            assert seed_started.wait(timeout=1.0)
            assert not compute_started.wait(timeout=0.05)
            assert calls == [f"watcher:{vault}", "watcher-seed:wait"]
            release_seed.set()
            assert compute_started.wait(timeout=1.0)
            assert not activated.wait(timeout=0.05)
            assert calls == [
                f"watcher:{vault}",
                "watcher-seed:wait",
                "watcher-seed:ready",
                f"compute:{vault}",
            ]
            readiness.mark_ready("retrieval_catalog")
            assert not activated.wait(timeout=0.05)
            assert calls[-1] == f"compute:{vault}"
            readiness.mark_ready("semantic_corpus")
            assert activated.wait(timeout=1.0)
            assert calls == [
                f"watcher:{vault}",
                "watcher-seed:wait",
                "watcher-seed:ready",
                f"compute:{vault}",
                "watcher-recovery",
                f"graph:{vault}",
                f"media:{vault}",
            ]
            activation.start()
            assert len(calls) == 7

    try:
        asyncio.run(exercise())
    finally:
        readiness.reset()


def test_local_runtime_activation_bounds_failed_seed_wait_before_compute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness.reset()
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    calls: list[str] = []
    observed_timeouts: list[float | None] = []
    managed_at_compute: list[bool] = []

    class Watcher:
        def wait_until_seeded(self, timeout=None) -> bool:
            calls.append("watcher-seed:wait")
            observed_timeouts.append(timeout)
            return False

        def finish_startup_recovery(self) -> None:
            calls.append("watcher-recovery")

        def stop(self) -> None:
            calls.append("watcher-stop")

    monkeypatch.setattr(
        server_runtime,
        "_start_file_watcher",
        lambda _root: (calls.append("watcher"), Watcher())[1],
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_compute_runtime",
        lambda _root, **_kwargs: (
            calls.append("compute"),
            managed_at_compute.append(readiness.runtime_managed()),
        ),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_graph_drain",
        lambda _root: calls.append("graph"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_media_worker",
        lambda _root: calls.append("media"),
    )

    activation = server_runtime.LocalRuntimeActivation(Path("unused-vault"))
    try:
        activation._activate()
    finally:
        activation._stop_background_workers()
        readiness.reset()

    assert observed_timeouts == [server_runtime.RECALL_SEED_WAIT_SECONDS]
    assert 0 < server_runtime.RECALL_SEED_WAIT_SECONDS <= 180
    assert managed_at_compute == [False]
    assert calls == [
        "watcher",
        "watcher-seed:wait",
        "watcher-stop",
        "compute",
        "graph",
        "media",
    ]


def test_local_runtime_activation_downgrades_when_watcher_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness.reset()
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    managed_at_compute: list[bool] = []
    monkeypatch.setattr(server_runtime, "_start_file_watcher", lambda _root: None)
    monkeypatch.setattr(
        server_runtime,
        "_start_compute_runtime",
        lambda _root, **_kwargs: managed_at_compute.append(readiness.runtime_managed()),
    )
    monkeypatch.setattr(server_runtime, "_start_graph_drain", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _root: None)

    activation = server_runtime.LocalRuntimeActivation(Path("unused-vault"))
    try:
        assert readiness.runtime_managed() is True
        activation._activate()
        assert managed_at_compute == [False]
        assert readiness.runtime_managed() is False
    finally:
        activation._stop_background_workers()
        readiness.reset()


def test_local_runtime_activation_waits_for_terminal_warm_after_catalog_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    vault = tmp_path / "vault"
    vault.mkdir()
    compute_started = threading.Event()
    watcher_started = threading.Event()
    downstream_activated = threading.Event()

    def start_compute(_root, **_kwargs) -> None:
        readiness.begin_warm()
        compute_started.set()

    monkeypatch.setattr(server_runtime, "_start_compute_runtime", start_compute)
    monkeypatch.setattr(
        server_runtime,
        "_start_file_watcher",
        lambda _root: watcher_started.set(),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_graph_drain",
        lambda _root: downstream_activated.set(),
    )
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _root: None)
    activation = server_runtime.LocalRuntimeActivation(vault, fallback_seconds=60.0)

    async def exercise() -> None:
        async with activation.lifespan()(SimpleNamespace()):
            activation.start()
            assert watcher_started.wait(timeout=1.0)
            assert compute_started.wait(timeout=1.0)
            readiness.mark_ready("semantic_corpus")
            assert not downstream_activated.wait(timeout=0.05)
            readiness.finish_warm()
            assert not downstream_activated.wait(timeout=0.05)
            readiness.mark_ready("retrieval_catalog")
            assert downstream_activated.wait(timeout=1.0)

    try:
        asyncio.run(exercise())
    finally:
        readiness.reset()


def test_disable_warmup_preserves_unverified_lazy_runtime_admission(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Start before any warm-up; earlier tests in the same shard may leave
    # process-global readiness from a different scenario behind.
    readiness.reset()
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    vault = tmp_path / "vault"
    vault.mkdir()

    server_runtime.LocalRuntimeActivation(vault, fallback_seconds=60.0)

    try:
        assert readiness.retrieval_admission(vault) == {
            "state": "unverified",
            "admitted": False,
        }
    finally:
        readiness.reset()


def test_a_server_commits_to_the_re_embed_job_before_it_admits_a_request(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The job starts last of the workers: recall on a vault no build covered
    # must be marked from construction, not from the job's thread.
    from exomem import recall_migration

    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    serving, standby = tmp_path / "serving", tmp_path / "standby"
    serving.mkdir()
    standby.mkdir()

    server_runtime.LocalRuntimeActivation(serving, fallback_seconds=60.0)
    held = server_runtime.LocalRuntimeActivation(standby, fallback_seconds=60.0, deferred=True)

    assert recall_migration.coverage_unchecked(serving)
    assert not recall_migration.coverage_unchecked(standby)  # a standby serves nothing yet
    monkeypatch.setattr(held, "start", lambda: None)
    held.release()
    assert recall_migration.coverage_unchecked(standby)


def test_terminal_semantic_warm_failure_does_not_strand_startup_recovery(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    vault = tmp_path / "vault"
    vault.mkdir()
    activation = server_runtime.LocalRuntimeActivation(vault, fallback_seconds=60.0)
    readiness.begin_warm()
    readiness.mark_ready("retrieval_catalog")
    returned = threading.Event()

    thread = threading.Thread(
        target=lambda: (activation._wait_for_required_admission(), returned.set()),
        daemon=True,
    )
    thread.start()
    try:
        assert not returned.wait(timeout=0.05)
        readiness.finish_warm()
        assert returned.wait(timeout=1.0)
    finally:
        readiness.reset()
        thread.join(timeout=1.0)


def test_local_runtime_activation_falls_back_without_liveness_probe(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    activated = threading.Event()
    monkeypatch.setattr(
        server_runtime,
        "_start_compute_runtime",
        lambda root, **_kwargs: activated.set() if root == vault else None,
    )
    monkeypatch.setattr(server_runtime, "_start_file_watcher", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_graph_drain", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _root: None)
    activation = server_runtime.LocalRuntimeActivation(vault, fallback_seconds=0.01)

    async def exercise() -> None:
        async with activation.lifespan()(SimpleNamespace()):
            assert await asyncio.to_thread(activated.wait, 1.0)

    try:
        asyncio.run(exercise())
    finally:
        readiness.reset()


def test_stdio_transport_activates_when_the_pipe_opens_not_on_the_fallback_timer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stdio client has no `/health` probe. Workers that start on the 5 s
    # fallback timer land their CPU burst inside the client's first queries.
    import io

    from exomem import server
    from exomem.governance import authorization_transport

    vault = tmp_path / "vault"
    vault.mkdir()
    activated = threading.Event()
    monkeypatch.setattr(
        server_runtime,
        "_start_compute_runtime",
        lambda root, **_kwargs: activated.set() if root == vault else None,
    )
    monkeypatch.setattr(server_runtime, "_start_file_watcher", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_graph_drain", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _root: None)
    activation = server_runtime.LocalRuntimeActivation(vault, fallback_seconds=60.0)
    mcp = server.ExomemFastMCP("stdio-activation-probe", lifespan=activation.lifespan())
    mcp._exomem_local_runtime_activation = activation

    class _ClientClosesOnceWorkersStart(io.RawIOBase):
        # The client sends nothing and closes stdin when the workers start,
        # or after 5 s, far inside the 60 s fallback timer.
        def readable(self) -> bool:
            return True

        def readinto(self, _buffer) -> int:
            activated.wait(5.0)
            return 0

    stdin = io.BufferedReader(_ClientClosesOnceWorkersStart())
    monkeypatch.setattr(authorization_transport.sys, "stdin", SimpleNamespace(buffer=stdin))
    monkeypatch.setattr(authorization_transport.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))

    try:
        asyncio.run(mcp.run_stdio_async(show_banner=False))
        assert activated.is_set()
    finally:
        readiness.reset()


def test_local_runtime_lifespan_waits_for_inflight_activation_before_teardown(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    vault = tmp_path / "vault"
    vault.mkdir()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def start_compute(_root: Path, **_kwargs) -> None:
        entered.set()
        assert release.wait(timeout=2.0)
        finished.set()

    monkeypatch.setattr(server_runtime, "_start_file_watcher", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_compute_runtime", start_compute)
    monkeypatch.setattr(server_runtime, "_start_graph_drain", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _root: None)
    activation = server_runtime.LocalRuntimeActivation(vault, fallback_seconds=60.0)

    async def exercise() -> None:
        lifetime = activation.lifespan()(SimpleNamespace())
        await lifetime.__aenter__()
        activation.start()
        assert await asyncio.to_thread(entered.wait, 1.0)
        exiting = asyncio.create_task(lifetime.__aexit__(None, None, None))
        await asyncio.sleep(0.05)
        assert not exiting.done()
        release.set()
        await asyncio.wait_for(exiting, timeout=1.0)
        assert finished.is_set()
        assert activation._thread is not None
        assert not activation._thread.is_alive()

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        thread = activation._thread
        if thread is not None:
            thread.join(timeout=1.0)
        readiness.reset()


def test_local_runtime_lifespan_refuses_to_finish_with_live_semantic_worker(tmp_path, monkeypatch) -> None:
    """Shutdown must not report completion while an uncancelled encode still runs."""
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    activation = server_runtime.LocalRuntimeActivation(tmp_path, deferred=True)

    def still_running():
        raise RuntimeError("semantic drain is still running during shutdown")

    activation.semantic_drain = SimpleNamespace(stop=still_running)

    async def exercise():
        with pytest.raises(RuntimeError, match="semantic drain is still running"):
            async with activation.lifespan()(SimpleNamespace()):
                pass

    asyncio.run(exercise())


def test_local_runtime_shutdown_rechecks_a_worker_assigned_after_first_stop(tmp_path, monkeypatch) -> None:
    """Joining activation is insufficient when its late worker cannot stop."""
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    activation = server_runtime.LocalRuntimeActivation(tmp_path, deferred=True)
    stopped = threading.Event()
    release = threading.Event()
    original_stop = activation._stop_background_workers

    def stop():
        original_stop()
        stopped.set()

    def late_start():
        assert release.wait(timeout=2)
        def live_stop():
            raise RuntimeError("still encoding")
        activation.semantic_drain = SimpleNamespace(stop=live_stop)

    monkeypatch.setattr(activation, "_stop_background_workers", stop)

    async def exercise():
        lifetime = activation.lifespan()(SimpleNamespace())
        await lifetime.__aenter__()
        activation._thread = threading.Thread(target=late_start)
        activation._thread.start()
        exiting = asyncio.create_task(lifetime.__aexit__(None, None, None))
        assert await asyncio.to_thread(stopped.wait, 1)
        release.set()
        with pytest.raises(RuntimeError, match="semantic drain is still running"):
            await asyncio.wait_for(exiting, 2)

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        if activation._thread is not None:
            activation._thread.join(timeout=2)


def test_local_runtime_lifespan_interrupts_a_stuck_admission_wait(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    vault = tmp_path / "vault"
    vault.mkdir()
    compute_started = threading.Event()
    exited = threading.Event()

    def start_compute(_root: Path, **_kwargs) -> None:
        readiness.begin_warm()
        compute_started.set()

    monkeypatch.setattr(server_runtime, "_start_file_watcher", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_compute_runtime", start_compute)
    monkeypatch.setattr(server_runtime, "_start_graph_drain", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _root: None)
    activation = server_runtime.LocalRuntimeActivation(vault, fallback_seconds=60.0)

    async def exercise() -> None:
        async with activation.lifespan()(SimpleNamespace()):
            activation.start()
            assert await asyncio.to_thread(compute_started.wait, 1.0)
        exited.set()

    runner = threading.Thread(target=lambda: asyncio.run(exercise()), daemon=True)
    runner.start()
    try:
        assert compute_started.wait(timeout=1.0)
        assert exited.wait(timeout=0.5)
        assert activation._thread is not None
        assert not activation._thread.is_alive()
    finally:
        readiness.mark_ready("retrieval_catalog")
        readiness.mark_ready("semantic_corpus")
        readiness.finish_warm()
        runner.join(timeout=1.0)
        readiness.reset()


def test_media_worker_startup_reconciles_media_missed_while_service_was_stopped(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    recording = vault / "Knowledge Base" / "Evidence" / "Audio" / "offline.m4a"
    recording.parent.mkdir(parents=True)
    recording.write_bytes(b"created while the service was stopped")
    calls: list[tuple[str, object]] = []

    class Worker:
        def start(self) -> None:
            calls.append(("start", None))

        def stop(self) -> None:
            calls.append(("stop", None))

        def scan_pending(self) -> int:
            calls.append(("scan_pending", None))
            return 0

    worker = Worker()

    def reconcile_all_media(root, *, limit: int, reconcile_one) -> None:
        assert calls and calls[0][0] == "start"
        assert recording.is_file()
        assert callable(reconcile_one)
        calls.append(("reconcile_all_media", (root, limit)))

    monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _vault: worker)
    monkeypatch.setattr(
        media_processing,
        "reconcile_all_media",
        reconcile_all_media,
        raising=False,
    )

    result = server_runtime._start_media_worker(vault)

    assert result is worker
    discovery = [payload for name, payload in calls if name == "reconcile_all_media"]
    assert len(discovery) == 1
    root, limit = discovery[0]
    assert root == vault
    assert isinstance(limit, int) and limit > 0


def test_media_startup_reconciles_when_worker_is_disabled(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[tuple[object, int]] = []
    monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _vault: None)
    monkeypatch.setattr(
        media_processing,
        "reconcile_all_media",
        lambda root, *, limit, reconcile_one: calls.append((root, limit)),
    )

    result = server_runtime._start_media_worker(vault)

    assert result is None
    assert len(calls) == 1
    assert calls[0][0] == vault
    assert calls[0][1] > 0


def test_media_startup_reconciles_after_worker_start_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    events: list[str] = []

    class Worker:
        def start(self) -> None:
            events.append("start")
            raise RuntimeError("worker unavailable")

        def stop(self) -> None:
            events.append("stop")

        def scan_pending(self) -> int:
            pytest.fail("failed worker must not scan pending sidecars")

    monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _vault: Worker())
    monkeypatch.setattr(
        media_processing,
        "reconcile_all_media",
        lambda root, *, limit, reconcile_one: events.append(
            f"reconcile:{root}:{limit}"
        ),
    )

    result = server_runtime._start_media_worker(vault)

    assert result is None
    assert events[:2] == ["start", "stop"]
    assert len([event for event in events if event.startswith("reconcile:")]) == 1


def test_media_startup_reconciliation_uses_writer_authority(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from exomem import writer_lease

    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    depth = 0

    class Manager:
        @contextmanager
        def mutation_guard(self, root, **metadata):
            nonlocal depth
            assert root == vault
            assert metadata["operation"] == "startup_media_reconcile_commit"
            assert metadata["holder_kind"] == "background"
            depth += 1
            try:
                yield
            finally:
                depth -= 1

    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())
    monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _root: None)

    def reconcile_media(*_args, commit_guard=None, **_kwargs):  # noqa: ANN001
        assert depth == 0
        assert commit_guard is not None
        with commit_guard():
            assert depth == 1

    monkeypatch.setattr(media_processing, "reconcile_media", reconcile_media)
    monkeypatch.setattr(
        media_processing,
        "reconcile_all_media",
        lambda _root, *, limit, reconcile_one: reconcile_one(
            vault / "Knowledge Base" / "Evidence" / "Audio" / "startup.m4a"
        ),
    )

    assert server_runtime._start_media_worker(vault) is None
    assert depth == 0


def test_media_startup_hashing_yields_foreground_guard_then_commits_guarded(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from exomem import writer_lease

    vault = tmp_path / "vault"
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "startup-large.m4a"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"large-enough-for-blocked-startup-provenance")
    initialize_vault_state_offline(vault, source="media startup guard fixture")
    state_dir = tmp_path / "state"
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=state_dir),
        mutation_timeout_seconds=1.0,
    )
    contender = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=state_dir),
        mutation_timeout_seconds=0.05,
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _root: None)
    hash_started = threading.Event()
    continue_hash = threading.Event()
    commit_seen = threading.Event()
    errors: list[BaseException] = []
    original_read = media_processing._read_provenance
    original_batch = media_processing.batch_atomic_write

    def blocked_read(*args, **kwargs):  # noqa: ANN002, ANN003
        hash_started.set()
        assert continue_hash.wait(2.0)
        return original_read(*args, **kwargs)

    def guarded_batch(*args, **kwargs):  # noqa: ANN002, ANN003
        boundary = manager.status(vault)["mutation_boundary"]
        assert boundary["state"] == "held"
        assert boundary["operation"] == "startup_media_reconcile_commit"
        commit_seen.set()
        return original_batch(*args, **kwargs)

    monkeypatch.setattr(media_processing, "_read_provenance", blocked_read)
    monkeypatch.setattr(media_processing, "batch_atomic_write", guarded_batch)

    def start() -> None:
        try:
            server_runtime._start_media_worker(vault)
        except BaseException as error:  # noqa: BLE001 - inspect thread outcome
            errors.append(error)

    startup = threading.Thread(target=start)
    startup.start()
    try:
        if not hash_started.wait(1.0):
            frame = sys._current_frames().get(startup.ident)
            stack = (
                "".join(traceback.format_stack(frame, limit=12))
                if frame is not None else "<thread exited>"
            )
            warnings = [
                record.getMessage() for record in caplog.records[-8:]
                if record.levelno >= logging.WARNING
            ]
            pytest.fail(
                f"startup missed provenance hook: alive={startup.is_alive()}, "
                f"errors={[type(error).__name__ for error in errors]}, warnings={warnings}\n{stack}"
            )
        with contender.mutation_guard(
            vault,
            request_id="foreground-during-startup-hash",
            operation="remember",
        ):
            pass
    finally:
        continue_hash.set()
        startup.join(timeout=3.0)

    assert not startup.is_alive()
    assert errors == []
    assert commit_seen.is_set()


def test_disabled_media_runtime_persists_actionable_blocked_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import media_jobs

    vault = tmp_path / "vault"
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "disabled.m4a"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"audio")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")

    assert server_runtime._start_media_worker(vault) is None

    [job] = media_jobs.status(vault)["jobs"]
    assert job["state"] == "blocked"
    assert job["retryable"] is True
    assert "enable media extraction" in job["next_action"]
    frontmatter = (binary.with_name(binary.name + ".md")).read_text(encoding="utf-8")
    assert "processing_state: blocked" in frontmatter
    assert "EXOMEM_DISABLE_MEDIA_EXTRACTION" in frontmatter


def test_failed_media_runtime_start_persists_actionable_blocked_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import media_jobs

    vault = tmp_path / "vault"
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "failed-start.m4a"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"audio")
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)

    class Worker:
        def start(self):
            raise OSError("child executable unavailable")

        def stop(self):
            return None

    monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _root: Worker())

    assert server_runtime._start_media_worker(vault) is None

    [job] = media_jobs.status(vault)["jobs"]
    assert job["state"] == "blocked"
    assert job["retryable"] is True
    assert job["error"].startswith("MediaRuntimeUnavailable: OSError:")
    assert "restart the service" in job["next_action"]


def test_later_drop_is_immediately_blocked_after_runtime_start_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import media_jobs

    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)

    class Worker:
        def start(self):
            raise OSError("worker boot failed")

        def stop(self):
            return None

    monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _root: Worker())

    assert server_runtime._start_media_worker(vault) is None
    assert not media_jobs.job_store_path(vault).exists()

    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "later.m4a"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"later audio")
    result = media_processing.reconcile_media(vault, binary)

    assert result.state == media_jobs.BLOCKED
    [job] = media_jobs.status(vault)["jobs"]
    assert job["state"] == media_jobs.BLOCKED
    assert "worker boot failed" in job["error"]
    assert "restart the service" in job["next_action"]
    sidecar = binary.with_name(binary.name + ".md").read_text(encoding="utf-8")
    assert "processing_state: blocked" in sidecar
    sidecar_path = binary.with_name(binary.name + ".md")
    before = sidecar_path.read_bytes()
    before_mtime = sidecar_path.stat().st_mtime_ns

    repeated = media_processing.reconcile_media(vault, binary)

    assert repeated.state == media_jobs.BLOCKED
    assert sidecar_path.read_bytes() == before
    assert sidecar_path.stat().st_mtime_ns == before_mtime


def _quiet_starters(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    monkeypatch.setattr(
        server_runtime.LocalRuntimeActivation, "_start_catalogue_recovery", lambda self, _root: None
    )
    monkeypatch.setattr(server_runtime, "_start_file_watcher", lambda _root: None)
    monkeypatch.setattr(
        server_runtime, "_start_compute_runtime", lambda _root, **_kwargs: calls.append("compute")
    )
    monkeypatch.setattr(
        server_runtime, "_start_graph_drain", lambda _root: calls.append("graph")
    )
    monkeypatch.setattr(
        server_runtime, "_start_media_worker", lambda _root: calls.append("media")
    )
    monkeypatch.setattr(
        server_runtime.LocalRuntimeActivation,
        "_start_vocabulary_recovery",
        lambda self, _root: calls.append("vocabulary"),
    )


def test_dreamer_starts_last_and_only_when_enabled(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import dreamer

    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []
    _quiet_starters(monkeypatch, calls)
    real_start = dreamer.start
    monkeypatch.setattr(
        dreamer, "start", lambda root: (calls.append("dreamer"), real_start(root))[1]
    )
    try:
        # Default off: the starter runs last and starts nothing.
        activation = server_runtime.LocalRuntimeActivation(vault)
        activation.start()
        activation._thread.join(5)
        assert calls == ["compute", "graph", "media", "vocabulary", "dreamer"]
        assert activation.dreamer is None
        assert not dreamer.running()

        calls.clear()
        monkeypatch.setenv("EXOMEM_DREAMER", "on")
        enabled = server_runtime.LocalRuntimeActivation(vault)
        enabled.start()
        enabled._thread.join(5)
        assert calls[-1] == "dreamer"
        assert enabled.dreamer is not None and enabled.dreamer.name == "exomem-dreamer"
        assert dreamer.running()
    finally:
        dreamer.reset_for_tests()
        readiness.reset()


def test_a_service_started_with_the_dreamer_off_starts_it_when_switched_on(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The managed service is upgraded in place and rarely restarted. A worker
    that only the boot-time setting could start never started at all: the
    owner's `exomem dreamer on` changed nothing, and no tick ever created the
    sidecar."""
    import time

    import dreamer_fixture as fx

    from exomem import dreamer, dreamer_policy, dreamer_store, freshness, mode

    vault = fx.build(tmp_path)
    monkeypatch.delenv("EXOMEM_DREAMER", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_MODE_WATCH", raising=False)
    monkeypatch.setattr(dreamer_policy, "IDLE_SECONDS", 0.0)
    monkeypatch.setattr(dreamer_policy, "settle_seconds", lambda _last: 0.0)
    _quiet_starters(monkeypatch, [])
    # `fx.build` seeded freshness as the watcher does; a stand-in that says so
    # keeps activation from revoking it.
    watcher = SimpleNamespace(
        wait_until_seeded=lambda timeout: True,
        arm_optional_recovery=lambda: None,
        stop=lambda: None,
    )
    monkeypatch.setattr(server_runtime, "_start_file_watcher", lambda _root: watcher)
    # The compute runtime's own config poll, at a test interval.
    monkeypatch.setattr(
        server_runtime,
        "_start_compute_runtime",
        lambda _root, **_kwargs: mode.start_config_watch(interval=0.05),
    )
    try:
        activation = server_runtime.LocalRuntimeActivation(vault)
        activation.start()
        activation._thread.join(5)
        assert not dreamer.running()
        assert not dreamer_store.sidecar_path(vault).exists()

        dreamer.write_setting("on")
        deadline = time.monotonic() + 15
        while dreamer.status()["ticks"] < 1 and time.monotonic() < deadline:
            time.sleep(0.05)

        assert dreamer.status()["ticks"] >= 1
        assert dreamer_store.sidecar_path(vault).is_file()
    finally:
        mode.stop_config_watch()
        dreamer.reset_for_tests()
        freshness.clear()
        readiness.reset()


def test_standby_defers_the_dreamer_until_release(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import dreamer

    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []
    _quiet_starters(monkeypatch, calls)
    monkeypatch.setenv("EXOMEM_DREAMER", "on")
    try:
        activation = server_runtime.LocalRuntimeActivation(vault, deferred=True)
        activation.start()
        assert activation._thread is None
        assert not dreamer.running()
        activation.release()
        activation._thread.join(5)
        assert calls[-1:] == ["vocabulary"]
        assert activation.dreamer is not None
        assert dreamer.running()
    finally:
        dreamer.reset_for_tests()
        readiness.reset()


def test_shutdown_stops_the_dreamer(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import dreamer

    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []
    _quiet_starters(monkeypatch, calls)
    monkeypatch.setenv("EXOMEM_DREAMER", "on")
    activation = server_runtime.LocalRuntimeActivation(vault, fallback_seconds=60.0)

    async def exercise() -> None:
        async with activation.lifespan()(SimpleNamespace()):
            activation.start()
            await asyncio.to_thread(activation._thread.join, 5)
            assert dreamer.running()
        assert not dreamer.running()
        assert [t for t in threading.enumerate() if t.name == "exomem-dreamer"] == []

    try:
        asyncio.run(exercise())
    finally:
        dreamer.reset_for_tests()
        readiness.reset()


def test_shutdown_reports_a_live_owned_graph_producer(tmp_path, monkeypatch):
    """Actual activation must not report cleanup while its producer still runs."""
    from exomem import graph_drain

    _quiet_starters(monkeypatch, [])
    entered = threading.Event()
    release = threading.Event()

    def work(_root):
        entered.set()
        release.wait(10)

    monkeypatch.delenv("EXOMEM_DISABLE_GRAPH_DRAIN", raising=False)
    monkeypatch.setattr(graph_drain, "_run", work)
    monkeypatch.setattr(server_runtime, "_start_graph_drain", graph_drain.start)
    activation = server_runtime.LocalRuntimeActivation(tmp_path, fallback_seconds=60)

    async def exercise():
        with pytest.raises(RuntimeError, match="graph drain is still running"):
            async with activation.lifespan()(SimpleNamespace()):
                activation.start()
                await asyncio.to_thread(activation._thread.join, 5)
                assert entered.wait(1)
                assert activation.graph_drain.is_alive()
        assert activation.graph_cleanup_attempted

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        graph_drain.stop()


def test_shutdown_while_vocabulary_watcher_starts_does_not_crash_or_orphan_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    activation = server_runtime.LocalRuntimeActivation(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_start = threading.Thread.start

    def paused_start(thread):
        if thread.name == "exomem-vocabulary-recovery":
            entered.set()
            assert release.wait(timeout=2.0)
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", paused_start)
    starter = threading.Thread(target=activation._start_vocabulary_recovery, args=(tmp_path,))
    starter.start()
    try:
        assert entered.wait(timeout=1.0)
        activation._stop_background_workers()
    finally:
        release.set()
        starter.join(timeout=2.0)
        activation._join_vocabulary_recovery()
    assert not starter.is_alive()
    assert activation.vocabulary_recovery is not None
    assert not activation.vocabulary_recovery.is_alive()


def test_stopping_background_workers_joins_the_vocabulary_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery watcher is a background worker like any other: it stops
    and is joined with the rest, not left running until the process exits."""
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    for starter in (
        "_start_derived_drain",
        "_start_file_watcher",
        "_start_compute_runtime",
        "_start_graph_drain",
        "_start_media_worker",
        "_start_dreamer",
    ):
        monkeypatch.setattr(server_runtime, starter, lambda _root, **_kwargs: None)
    activation = server_runtime.LocalRuntimeActivation(tmp_path)
    monkeypatch.setattr(activation, "_start_recall_reembed", lambda _root: None)

    activation._activate()
    watcher = activation.vocabulary_recovery
    assert watcher is not None and watcher.name == "exomem-vocabulary-recovery"
    activation._stop_background_workers()

    assert not watcher.is_alive()
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name == "exomem-vocabulary-recovery" and thread.is_alive()
    ]


def test_shutdown_during_watcher_recovery_does_not_launch_matrix_warm(tmp_path, monkeypatch):
    """Returning from cancelled recovery must end the activation chain."""
    activation = server_runtime.LocalRuntimeActivation(tmp_path)

    def start(label, _starter):
        if label == "file watcher recovery":
            activation._shutdown.set()

    monkeypatch.setattr(activation, "_start_component", start)
    monkeypatch.setattr(activation, "_wait_for_recall_seed", lambda: None)
    monkeypatch.setattr(activation, "_wait_for_required_admission", lambda: None)
    monkeypatch.setattr(activation, "_downgrade_recall_runtime", lambda: None)
    monkeypatch.setattr(activation, "_stop_background_workers", lambda: None)
    monkeypatch.setattr(
        server_runtime.threading, "Thread",
        lambda **_kwargs: pytest.fail("shutdown launched fresh matrix warm work"),
    )
    activation._activate()

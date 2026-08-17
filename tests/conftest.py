"""Per-test fixture-vault copy. Repo fixtures NEVER mutate."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from exomem import activation_manifest as activation_manifest_module
from exomem import embeddings as embeddings_module
from exomem import find as find_module
from exomem import graph_sync as graph_sync_module
from exomem import schema as schema_module
from exomem import semantic_contract as semantic_contract_module

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures"

# The benchmark package (benchmarks/membench) deliberately lives outside src/
# and outside the wheel/sdist; tests reach it via this guarded path insert.
_BENCHMARKS_DIR = REPO_ROOT / "benchmarks"
if _BENCHMARKS_DIR.is_dir() and str(_BENCHMARKS_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCHMARKS_DIR))

# POSIX-only by construction. These modules reach `fcntl` or `os.O_DIRECTORY`
# at *import* scope -- `exomem.hosted_restore` and `benchmarks/protocol/custody.py`
# -- so on Windows they abort collection with ImportError rather than skipping,
# and a single unimportable module interrupts the entire run. Declaring them
# here rather than passing `--ignore` on the command line keeps `pytest` one
# command on every platform, for CI and for a developer on Windows alike.
collect_ignore: list[str] = []
if os.name == "nt":
    collect_ignore += [
        "test_dataset_identity_case_count.py",  # -> lme.runner -> protocol.custody
        "test_hosted_restore_candidate.py",  # -> exomem.hosted_restore -> fcntl
        "test_lme_reader_gate.py",  # -> lme.runner -> protocol.custody
        "test_lme_runner.py",  # -> lme.runner -> protocol.custody
        "test_protocol_custody.py",  # imports fcntl directly
    ]


@pytest.fixture(autouse=True)
def _process_env_isolation():
    """Restore os.environ after every test.

    Benchmark adapters apply EXOMEM_* profile pins to the process env, and a
    test that drives setup() without a paired cleanup() leaks them into every
    later test: PR #390's CI failed six query-log/usage-ranking tests because
    a stale EXOMEM_VAULT_PATH and EXOMEM_DISABLE_RELEVANCE_CHECK survived from
    an earlier adapter test — failures invisible in isolation and under -k
    filters. Autouse setup runs before test-requested fixtures, so this
    teardown runs after monkeypatch undo and restores the pre-test
    environment exactly.
    """
    saved = os.environ.copy()
    yield
    for key in set(os.environ) - set(saved):
        del os.environ[key]
    for key, value in saved.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _reset_corpus_context_cache():
    """Every test starts with a cold corpus-context cache.

    The cache invalidates on filesystem changes, which production writes
    always make; tests additionally monkeypatch corpus-affecting internals
    (access tiers, registries, walks), which no filesystem census can see.
    A per-test reset keeps such patches from serving a context built under a
    different test's (or an unpatched) environment.
    """
    semantic_contract_module.reset_corpus_context_cache()
    activation_manifest_module.reset_manifest_cache()
    yield
    semantic_contract_module.reset_corpus_context_cache()
    activation_manifest_module.reset_manifest_cache()


#: The single thread name every graph rebuild runs under, in both the
#: registered-flight path (`graph_sync.GraphRebuildCoordinator.ensure_started`)
#: and the warming path (`epistemic_graph.schedule_background_rebuild`).
_GRAPH_REBUILD_THREAD_NAME = "exomem-graph-rebuild"
#: Generous: a rebuild over a test vault is milliseconds. A pass that cannot
#: finish in 30 s has not been slow, it has wedged, and that is worth failing on
#: rather than leaving for whichever test inherits it.
_GRAPH_QUIESCE_TIMEOUT_SECONDS = 30.0


def _drain_graph_rebuild_threads(timeout: float = _GRAPH_QUIESCE_TIMEOUT_SECONDS) -> None:
    """Join every graph rebuild still running, and say so if one will not stop.

    The clock is read only once there is something to wait for. This is autouse
    teardown, so it runs after *every* test, and a test is entitled to replace
    `time.monotonic` with a scripted sequence of its own -- one does. Charging
    the empty case a clock read exhausted that sequence and failed the test in
    teardown with `generator raised StopIteration`, from a fixture that had
    nothing to drain.
    """
    deadline: float | None = None
    while True:
        alive = [
            thread
            for thread in threading.enumerate()
            if thread.name == _GRAPH_REBUILD_THREAD_NAME and thread.is_alive()
        ]
        if not alive:
            return
        if deadline is None:
            deadline = time.monotonic() + timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"{len(alive)} graph rebuild thread(s) did not finish within "
                f"{timeout:.0f}s; a wedged rebuild must not be inherited by the next test"
            )
        for thread in alive:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))


@pytest.fixture(autouse=True)
def _quiesce_graph_rebuilds():
    """Let no graph rebuild outlive the test that started it.

    Production runs one long-lived process against one vault. The suite runs
    many vaults through one process, and since an interactive write stopped
    joining its rebuild (#576) that rebuild is a daemon thread which outlives
    the request -- and therefore, here, the test. It then keeps touching
    process-global projections (`find.unload_ram_caches`, the shared resolver
    and corpus-context caches) while the *next* test is already running against
    a different vault. That is not a race the production shape has; it is an
    artefact of the suite's process sharing.

    Draining at teardown restores the isolation the write's join used to
    provide by accident, without putting that wait back on the write path.
    `graph_sync` coordinators are dropped afterwards so a suite of thousands of
    vaults does not retain one coordinator per vault for the whole run.

    Deliberately not a convergence helper: a test that needs the graph to be
    current asserts that for itself with `graph_sync.await_active_rebuild`.
    This fixture only guarantees that nothing is still running.
    """
    yield
    try:
        _drain_graph_rebuild_threads()
    finally:
        with graph_sync_module._COORDINATORS_LOCK:
            graph_sync_module._COORDINATORS.clear()


@pytest.fixture(autouse=True)
def _disable_embeddings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Skip the heavy bge-base load by default in the test suite.

    Individual tests that exercise embeddings (test_hybrid_search.py)
    delete this env var via their own monkeypatch.
    """
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    # Isolate compute-mode resolution from the developer's real ~/.exomem/config.json
    # and any ambient EXOMEM_MODE/EXOMEM_DEVICE — the suite must resolve to the
    # `normal` default deterministically (device selection now consults mode.py).
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "no-such-exomem-config.json"))
    for _var in (
        "EXOMEM_MODE",
        "EXOMEM_QUIET_MODE",
        "EXOMEM_DEVICE",
        "EXOMEM_GPU_MIN_FREE_GB",
        # Model residency policy derives from these; an ambient value would otherwise
        # move `mode.resolved()`, `status.models`, and the reaper's decision.
        "EXOMEM_PRELOAD_MODELS",
        "EXOMEM_RELEASE_GPU_WHEN_IDLE",
        "EXOMEM_MODEL_OFFLINE",
    ):
        monkeypatch.delenv(_var, raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    # Never spawn the background warm thread from build_server in tests — it
    # would outlive the per-test tmp vault. Warm/readiness tests manage their
    # own env + readiness.reset().
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    # A committed repo-root ranking_config.json must never perturb the suite:
    # force find()'s adopted-config seam to DEFAULT_RANKING. Tests that exercise
    # the load seam delete this var via their own monkeypatch.
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING_CONFIG", "1")
    # No real ASR/OCR in the suite: keep uploads from enqueuing GPU work. Tests that
    # exercise the worker enable it explicitly and stub extract.extract_text.
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    # No real CLIP either; tests that exercise it stub embeddings.embed_image/embed_clip_text.
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    # Opt-in media upgrades must not leak from a developer's real environment into
    # default tests. Tests that exercise these gates set them explicitly.
    monkeypatch.delenv("EXOMEM_SEMANTIC_SEGMENTS", raising=False)
    monkeypatch.delenv("EXOMEM_VIDEO_SCENE_FRAMES", raising=False)
    # The watcher now starts independently of embeddings (it maintains the
    # freshness/inbound registries too), so build_server would spawn a real
    # watchdog observer in the suite without this. Watcher tests opt back in.
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    # Don't spawn the mode-config watch daemon from build_server in the suite; mode-watch
    # tests drive it directly.
    monkeypatch.setenv("EXOMEM_DISABLE_MODE_WATCH", "1")
    # The corpus-context cache invalidates on filesystem changes, which is
    # complete for production inputs — but tests also monkeypatch
    # corpus-affecting internals (access tiers, registries, walks), which no
    # filesystem census can observe. Default it off in the suite;
    # test_corpus_context_cache.py deletes this var to exercise it.
    monkeypatch.setenv("EXOMEM_DISABLE_CORPUS_CACHE", "1")


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copy tests/fixtures/ into a tmp dir; return it as the vault root."""
    dest = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, dest)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(dest))
    # Clear find's in-process cache so previous test runs don't bleed in.
    find_module.clear_cache()
    # Drop the process-shared embedding index memo — a stale instance keyed by a
    # prior tmp vault's path would otherwise persist across tests.
    embeddings_module.clear_embedding_indexes()
    return dest


@pytest.fixture
def source_schema(vault: Path) -> schema_module.SourceSchema:
    return schema_module.load_source_schema(vault)

"""Per-test fixture-vault copy. Repo fixtures NEVER mutate."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from exomem import embeddings as embeddings_module
from exomem import find as find_module
from exomem import schema as schema_module
from exomem import semantic_contract as semantic_contract_module

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures"


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
    yield
    semantic_contract_module.reset_corpus_context_cache()


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
    for _var in ("EXOMEM_MODE", "EXOMEM_QUIET_MODE", "EXOMEM_DEVICE", "EXOMEM_GPU_MIN_FREE_GB"):
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

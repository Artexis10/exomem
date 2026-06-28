"""Per-test fixture-vault copy. Repo fixtures NEVER mutate."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kb_mcp import find as find_module
from kb_mcp import schema as schema_module


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def _disable_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the heavy bge-base load by default in the test suite.

    Individual tests that exercise embeddings (test_hybrid_search.py)
    delete this env var via their own monkeypatch.
    """
    monkeypatch.setenv("KB_MCP_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("KB_MCP_DISABLE_RELEVANCE_CHECK", "1")
    # A committed repo-root ranking_config.json must never perturb the suite:
    # force find()'s adopted-config seam to DEFAULT_RANKING. Tests that exercise
    # the load seam delete this var via their own monkeypatch.
    monkeypatch.setenv("KB_MCP_DISABLE_RANKING_CONFIG", "1")
    # No real ASR/OCR in the suite: keep uploads from enqueuing GPU work. Tests that
    # exercise the worker enable it explicitly and stub extract.extract_text.
    monkeypatch.setenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", "1")
    # No real CLIP either; tests that exercise it stub embeddings.embed_image/embed_clip_text.
    monkeypatch.setenv("KB_MCP_DISABLE_CLIP", "1")


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copy tests/fixtures/ into a tmp dir; return it as the vault root."""
    dest = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, dest)
    monkeypatch.setenv("KB_MCP_VAULT_PATH", str(dest))
    # Clear find's in-process cache so previous test runs don't bleed in.
    find_module.clear_cache()
    return dest


@pytest.fixture
def source_schema(vault: Path) -> schema_module.SourceSchema:
    return schema_module.load_source_schema(vault)

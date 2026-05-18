"""Per-test fixture-vault copy. Repo fixtures NEVER mutate."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kb_mcp import find as find_module
from kb_mcp import schema as schema_module


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures"


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

"""Correctness of the corpus-context cache (semantic_contract).

The cache may only ever serve a context that is indistinguishable from a
fresh build. Object identity is the detector below: a cache hit returns the
same object, a rebuild returns a new one.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from exomem import relation_registry, semantic_contract, semantic_language_registry

_PAGE_REL = "Knowledge Base/Notes/Insights/one.md"


def _page(*, title: str = "Page", body: str = "Body.\n") -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: insight\n"
        "status: active\n"
        "project: atlas\n"
        "---\n\n"
        f"{body}"
    )


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch: pytest.MonkeyPatch):
    # The suite-wide conftest defaults the cache OFF; this suite exists to
    # exercise it, so opt back in and start cold.
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.reset_corpus_context_cache()
    yield
    semantic_contract.reset_corpus_context_cache()


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    notes = tmp_path / "Knowledge Base" / "Notes" / "Insights"
    notes.mkdir(parents=True)
    (notes / "one.md").write_text(_page(title="One"), encoding="utf-8")
    (notes / "two.md").write_text(_page(title="Two"), encoding="utf-8")
    return tmp_path


def test_unchanged_corpus_is_reused(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    second = semantic_contract.build_corpus_context(vault)
    assert second is first
    assert set(second.pages) == {
        "Knowledge Base/Notes/Insights/one.md",
        "Knowledge Base/Notes/Insights/two.md",
    }


def test_content_change_rebuilds(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    (vault / _PAGE_REL).write_text(
        _page(title="One", body="Entirely new body text.\n"), encoding="utf-8"
    )
    second = semantic_contract.build_corpus_context(vault)
    assert second is not first
    assert second.pages[_PAGE_REL].source_hash != first.pages[_PAGE_REL].source_hash


def test_mtime_preserving_sync_edit_rebuilds(vault: Path) -> None:
    """The Syncthing trap: new content materialized with an OLDER mtime.

    A max-mtime freshness key would serve the stale corpus here. The census
    compares (path, size, mtime_ns) per file, so the synced file's changed
    mtime — even though it is older than every other timestamp in the vault —
    invalidates the entry.
    """
    page = vault / _PAGE_REL
    first = semantic_contract.build_corpus_context(vault)
    original = page.stat()
    original_bytes = page.read_bytes()
    replacement = original_bytes.replace(b"One", b"Uno")
    assert replacement != original_bytes
    assert len(replacement) == original.st_size
    page.write_bytes(replacement)
    hour_ns = 3_600_000_000_000
    os.utime(page, ns=(original.st_mtime_ns - hour_ns, original.st_mtime_ns - hour_ns))
    assert page.stat().st_size == original.st_size
    second = semantic_contract.build_corpus_context(vault)
    assert second is not first
    assert second.pages[_PAGE_REL].title == "Uno"


def test_added_page_rebuilds(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    (vault / "Knowledge Base" / "Notes" / "Insights" / "three.md").write_text(
        _page(title="Three"), encoding="utf-8"
    )
    second = semantic_contract.build_corpus_context(vault)
    assert second is not first
    assert "Knowledge Base/Notes/Insights/three.md" in second.pages


def test_removed_page_rebuilds(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    (vault / "Knowledge Base" / "Notes" / "Insights" / "two.md").unlink()
    second = semantic_contract.build_corpus_context(vault)
    assert second is not first
    assert "Knowledge Base/Notes/Insights/two.md" not in second.pages


def test_access_config_change_rebuilds(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    (vault / "Knowledge Base" / "_access.yaml").write_text(
        "readonly:\n- Notes\n", encoding="utf-8"
    )
    second = semantic_contract.build_corpus_context(vault)
    assert second is not first


def test_census_covers_non_markdown_inputs(vault: Path) -> None:
    census = semantic_contract._corpus_census(vault)
    assert census is not None
    markers = {entry[0] for entry in census if entry[1] in {"cfg", "absent"}}
    assert "Knowledge Base/_access.yaml" in markers
    assert "Knowledge Base/_Schema/relation-registry.yaml" in markers
    assert "Knowledge Base/_Schema/semantic-language-registry.yaml" in markers


def test_candidate_build_bypasses_and_does_not_pollute_cache(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    candidate = semantic_contract.build_page_state(
        vault,
        "Knowledge Base/Notes/Insights/draft.md",
        _page(title="Draft"),
        relation_registry=relation_registry.core_registry(),
        language_registry=semantic_language_registry.core_registry(),
    )
    with_candidate = semantic_contract.build_corpus_context(vault, candidate=candidate)
    assert with_candidate is not first
    assert "Knowledge Base/Notes/Insights/draft.md" in with_candidate.pages
    again = semantic_contract.build_corpus_context(vault)
    assert again is first
    assert "Knowledge Base/Notes/Insights/draft.md" not in again.pages


def test_disk_equal_registries_share_the_cache(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    registry = relation_registry.load_registry(vault)
    language = semantic_language_registry.load_registry(vault)
    second = semantic_contract.build_corpus_context(
        vault, registry=registry, language_registry=language
    )
    assert second is first


def test_synthetic_registry_bypasses_cache(vault: Path) -> None:
    first = semantic_contract.build_corpus_context(vault)
    core = relation_registry.load_registry(vault)
    synthetic = dataclasses.replace(core, extension_hash="0" * 64)
    second = semantic_contract.build_corpus_context(vault, registry=synthetic)
    assert second is not first


def test_kill_switch_disables_cache(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_CORPUS_CACHE", "1")
    first = semantic_contract.build_corpus_context(vault)
    second = semantic_contract.build_corpus_context(vault)
    assert second is not first

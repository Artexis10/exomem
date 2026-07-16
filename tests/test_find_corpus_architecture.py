"""Architecture checks for the find corpus-access split."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import find, find_corpus


def test_find_reexports_corpus_cache_and_helpers() -> None:
    assert find.FrontmatterCache is find_corpus.FrontmatterCache
    assert find._CACHE is find_corpus.CACHE
    assert find._walk_md is find_corpus.walk_md
    assert find._parse_page is find_corpus.parse_page
    assert find._passes_filters is find_corpus.passes_filters


def test_walk_md_skips_private_batch_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    visible = tmp_path / "visible.md"
    visible.write_text("visible", encoding="utf-8")
    workspace = tmp_path / f".exomem-batch-{'a' * 32}"
    workspace.mkdir()

    real_iterdir = Path.iterdir

    def guarded_iterdir(path: Path):
        if path == workspace:
            raise PermissionError("private service workspace")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    assert list(find_corpus.walk_md(tmp_path)) == [visible]

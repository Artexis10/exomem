from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from exomem import bm25
from exomem import find as find_module
from exomem import freshness


@pytest.fixture(autouse=True)
def _isolated_python_bm25(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    find_module.clear_cache()
    bm25.clear_cache()
    yield
    find_module.clear_cache()
    bm25.clear_cache()


def _write_page(root: Path, name: str, body: str) -> Path:
    path = root / "Knowledge Base" / "Notes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: insight\ntitle: "
        f"{path.stem}\nupdated: 2026-08-20\n---\n\n# {path.stem}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _make_pages(root: Path, count: int) -> list[Path]:
    return [
        _write_page(root, f"page-{index:03d}.md", f"shared topic token-{index:03d}")
        for index in range(count)
    ]


def _seed(root: Path) -> None:
    kb_paths = list(find_module._walk_md(root / "Knowledge Base"))
    entries = [(str(path), freshness.stat_signature(path)) for path in kb_paths]
    freshness.seed(root, "kb", entries)
    freshness.seed(root, "vault", entries)


def _edit(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    future = time.time() + 10_000
    os.utime(path, (future, future))


def _entry(index: bm25.BM25Index, root: Path):
    return index._cache[(root, "kb")]


def _path_token_pairs(entry) -> set[tuple[str, tuple[str, ...]]]:
    return {(path, tuple(tokens)) for path, tokens in entry.tokens_by_path.items()}


def _scores_by_path(entry, query: str) -> dict[str, float]:
    scores = entry.bm25.get_scores(bm25.tokenize(query))
    return {
        path: float(score)
        for path, score in zip(entry.paths, scores, strict=True)
    }


def test_single_write_repairs_without_walking_the_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = _make_pages(tmp_path, 20)
    _seed(tmp_path)
    index = bm25.BM25Index()
    index.search(tmp_path, "shared topic", 10)

    walks = 0
    real_walk = bm25._recall_walk

    def counted_walk(root: Path, scope: str):
        nonlocal walks
        walks += 1
        return real_walk(root, scope)

    monkeypatch.setattr(bm25, "_recall_walk", counted_walk)
    _edit(pages[0], "# changed\n\nshared topic repaired-page-marker\n")
    freshness.on_files_changed(tmp_path, changed=[pages[0]])

    index.search(tmp_path, "repaired-page-marker", 10)

    assert walks == 0
    assert index.last_tokenized == 1
    assert index.last_reused == 0
    assert _entry(index, tmp_path).checkpoint == freshness.recall_checkpoint(
        tmp_path, "kb"
    )


def test_repaired_corpus_matches_fresh_rebuild_after_mixed_changes(tmp_path: Path) -> None:
    # Three changed identities stay below the measured 10% repair cutoff.
    pages = _make_pages(tmp_path, 40)
    _seed(tmp_path)
    repaired = bm25.BM25Index()
    repaired.search(tmp_path, "shared topic", 50)

    _edit(pages[0], "# edited\n\nshared topic edited-marker\n")
    created = _write_page(
        tmp_path, "page-created.md", "shared topic created-marker"
    )
    pages[1].unlink()
    freshness.on_files_changed(
        tmp_path,
        changed=[pages[0], created],
        deleted=[pages[1]],
    )
    repaired.search(tmp_path, "shared topic", 50)

    rebuilt = bm25.BM25Index()
    rebuilt.search(tmp_path, "shared topic", 50)
    repaired_entry = _entry(repaired, tmp_path)
    rebuilt_entry = _entry(rebuilt, tmp_path)

    assert _path_token_pairs(repaired_entry) == _path_token_pairs(rebuilt_entry)
    assert _scores_by_path(repaired_entry, "shared topic") == _scores_by_path(
        rebuilt_entry, "shared topic"
    )


def test_recall_policy_change_forces_full_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = _write_page(tmp_path, "Private/secret.md", "private-policy-marker")
    _write_page(tmp_path, "public.md", "public-policy-marker")
    _seed(tmp_path)
    index = bm25.BM25Index()
    assert index.search(tmp_path, "private-policy-marker", 10)

    walks = 0
    real_walk = bm25._recall_walk

    def counted_walk(root: Path, scope: str):
        nonlocal walks
        walks += 1
        return real_walk(root, scope)

    monkeypatch.setattr(bm25, "_recall_walk", counted_walk)
    (tmp_path / "Knowledge Base" / "_access.yaml").write_text(
        "excluded:\n  - Notes/Private\n", encoding="utf-8"
    )

    after = index.search(tmp_path, "private-policy-marker", 10)
    assert walks == 1
    secret_rel = secret.relative_to(tmp_path).as_posix()
    assert secret_rel not in {path for path, _score in after}
    assert secret_rel not in _entry(index, tmp_path).paths


def test_incomplete_delta_forces_full_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = _make_pages(tmp_path, 20)
    _seed(tmp_path)
    index = bm25.BM25Index()
    index.search(tmp_path, "shared topic", 10)

    freshness.clear()
    _seed(tmp_path)
    _edit(pages[0], "# foreign checkpoint\n\nshared topic foreign-marker\n")
    freshness.on_files_changed(tmp_path, changed=[pages[0]])

    walks = 0
    real_walk = bm25._recall_walk

    def counted_walk(root: Path, scope: str):
        nonlocal walks
        walks += 1
        return real_walk(root, scope)

    monkeypatch.setattr(bm25, "_recall_walk", counted_walk)
    index.search(tmp_path, "foreign-marker", 10)

    assert walks == 1


def test_deleted_page_leaves_and_created_page_enters_repaired_corpus(
    tmp_path: Path,
) -> None:
    pages = _make_pages(tmp_path, 20)
    _seed(tmp_path)
    index = bm25.BM25Index()
    index.search(tmp_path, "shared topic", 50)

    deleted_rel = pages[0].relative_to(tmp_path).as_posix()
    pages[0].unlink()
    created = _write_page(tmp_path, "created.md", "shared topic new-entry-marker")
    created_rel = created.relative_to(tmp_path).as_posix()
    freshness.on_files_changed(tmp_path, changed=[created], deleted=[pages[0]])
    index.search(tmp_path, "shared topic", 50)

    paths = set(_entry(index, tmp_path).paths)
    assert deleted_rel not in paths
    assert created_rel in paths


def test_oversized_delta_falls_back_to_full_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = _make_pages(tmp_path, 100)
    _seed(tmp_path)
    index = bm25.BM25Index()
    index.search(tmp_path, "shared topic", 10)

    assert 0 < bm25.MAX_INCREMENTAL_REPAIR_FRACTION < 1
    repair_budget = max(
        1, int(len(pages) * bm25.MAX_INCREMENTAL_REPAIR_FRACTION)
    )
    changed = pages[: repair_budget + 1]
    for position, path in enumerate(changed):
        _edit(path, f"# changed {position}\n\nshared topic oversized-marker\n")
    freshness.on_files_changed(tmp_path, changed=changed)

    walks = 0
    real_walk = bm25._recall_walk

    def counted_walk(root: Path, scope: str):
        nonlocal walks
        walks += 1
        return real_walk(root, scope)

    monkeypatch.setattr(bm25, "_recall_walk", counted_walk)
    index.search(tmp_path, "oversized-marker", 10)

    assert walks == 1

"""Structural gates for foreground retrieval cost.

These tests warm the rebuildable lexical sidecar first, then prove the request
path is bounded to indexed candidates instead of returning to the Markdown
corpus as a prerequisite to recall.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from exomem import bm25, freshness, lexstore, readiness, semantic_index
from exomem import find as find_module

pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="this SQLite build lacks FTS5"
)


def _write_page(root: Path, rel_path: str, body: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    page_id = uuid.uuid5(uuid.NAMESPACE_URL, f"bounded-retrieval:{rel_path}")
    path.write_text(
        "---\n"
        "type: insight\n"
        f"title: {path.stem}\n"
        f"exomem_id: {page_id}\n"
        "status: active\n"
        "updated: 2026-07-22\n"
        "---\n\n"
        f"# {path.stem}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _seed_live_freshness(root: Path, paths: list[Path]) -> None:
    vault_entries = [(str(path), freshness.stat_signature(path)) for path in paths]
    kb_entries = [
        entry for entry in vault_entries if Path(entry[0]).is_relative_to(root / "Knowledge Base")
    ]
    freshness.seed(root, "kb", kb_entries)
    freshness.seed(root, "vault", vault_entries)


def test_warm_keyword_unit_recall_hydrates_only_indexed_parent_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _write_page(
        tmp_path,
        "Knowledge Base/Notes/target.md",
        "- [runtime reliability] bounded recall needle ^target",
    )
    pages = [target]
    for index in range(60):
        pages.append(
            _write_page(
                tmp_path,
                f"Knowledge Base/Notes/filler-{index:03d}.md",
                f"- [observation] unrelated filler {index} ^filler-{index}",
            )
        )
    _seed_live_freshness(tmp_path, pages)
    snapshot = find_module.FreshnessSnapshot(tmp_path)
    assert lexstore.search_semantic_units(
        tmp_path,
        "bounded recall needle",
        k=10,
        scope="kb",
        freshness=snapshot.kb(),
    )

    walked = 0
    reparsed: list[str] = []
    original_walk = find_module._walk_md
    original_state = semantic_index.current_parent_index_state

    def observed_walk(*args: Any, **kwargs: Any) -> Any:
        nonlocal walked
        walked += 1
        return original_walk(*args, **kwargs)

    def observed_state(root: Path, path: Path | str, **kwargs: Any) -> Any:
        reparsed.append(Path(path).as_posix())
        return original_state(root, path, **kwargs)

    monkeypatch.setattr(find_module, "_walk_md", observed_walk)
    monkeypatch.setattr(semantic_index, "current_parent_index_state", observed_state)

    hits = find_module.find(
        tmp_path,
        query="bounded recall needle",
        scope="kb-only",
        mode="keyword",
        graph=False,
        result_level="unit",
        limit=5,
    )

    assert [hit.parent_path for hit in hits] == ["Knowledge Base/Notes/target.md"]
    assert walked == 0
    assert reparsed == [target.relative_to(tmp_path).as_posix()]


def test_warm_keyword_page_recall_never_walks_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _write_page(
        tmp_path,
        "Knowledge Base/Notes/page-target.md",
        "bounded page recall needle",
    )
    pages = [target]
    for index in range(60):
        pages.append(
            _write_page(
                tmp_path,
                f"Knowledge Base/Notes/page-filler-{index:03d}.md",
                f"unrelated page filler {index}",
            )
        )
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("warm keyword recall must query the lexical sidecar")

    monkeypatch.setattr(find_module, "_walk_md", forbidden)
    hits = find_module.find(
        tmp_path,
        query="bounded page recall needle",
        scope="kb-only",
        mode="keyword",
        graph=False,
        result_level="page",
        limit=5,
    )

    assert [hit.path for hit in hits] == [target.relative_to(tmp_path).as_posix()]


def test_warm_hybrid_unit_recall_never_builds_all_parent_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _write_page(
        tmp_path,
        "Knowledge Base/Notes/hybrid-target.md",
        "- [runtime reliability] bounded hybrid needle ^hybrid-target",
    )
    pages = [target]
    for index in range(60):
        pages.append(
            _write_page(
                tmp_path,
                f"Knowledge Base/Notes/hybrid-filler-{index:03d}.md",
                f"- [observation] unrelated hybrid filler {index} ^hybrid-{index}",
            )
        )
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(readiness, "should_defer", lambda lane: lane == "embeddings")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("hybrid unit recall must not build an all-parent projection")

    monkeypatch.setattr(find_module, "_eligible_unit_records", forbidden)
    hits = find_module.find(
        tmp_path,
        query="bounded hybrid needle",
        scope="kb-only",
        mode="hybrid",
        graph=False,
        rerank=False,
        result_level="unit",
        limit=5,
    )

    assert hits[0].parent_path == target.relative_to(tmp_path).as_posix()


def test_vector_unit_recall_hydrates_only_the_bounded_winner_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/vector-{index:03d}.md",
            f"- [observation] vector payload {index} ^vector-{index}",
        )
        for index in range(40)
    ]
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)
    states = [semantic_index.build_parent_index_state(tmp_path, page) for page in pages]
    vector_hits = [
        SimpleNamespace(
            unit_ref=state.document.units[0].unit_ref,
            parent_path=page.relative_to(tmp_path).as_posix(),
            parent_generation=state.parent_generation,
            cosine=1.0 - index / 100.0,
        )
        for index, (page, state) in enumerate(zip(pages[:21], states[:21], strict=True))
    ]
    monkeypatch.setattr(
        find_module,
        "_vector_unit_candidates",
        lambda *_args, **_kwargs: (
            vector_hits,
            {"status": "participated", "backend": "bounded-test"},
            "kb",
        ),
    )
    reparsed: list[str] = []
    original_state = semantic_index.current_parent_index_state

    def observed_state(root: Path, path: Path | str, **kwargs: Any) -> Any:
        reparsed.append(Path(path).as_posix())
        return original_state(root, path, **kwargs)

    monkeypatch.setattr(semantic_index, "current_parent_index_state", observed_state)

    hits = find_module.find(
        tmp_path,
        query="vector-only-no-literal",
        scope="kb-only",
        mode="vector",
        graph=False,
        result_level="unit",
        limit=1,
    )

    assert len(hits) == 1
    assert len(reparsed) == 20


def test_warm_outside_kb_widening_queries_lexstore_without_python_bm25(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        _write_page(tmp_path, "Knowledge Base/Notes/inside.md", "inside only"),
        _write_page(tmp_path, "Reference/target.md", "outside bounded needle"),
    ]
    for index in range(60):
        pages.append(
            _write_page(
                tmp_path,
                f"Reference/filler-{index:03d}.md",
                f"unrelated filler {index}",
            )
        )
    _seed_live_freshness(tmp_path, pages)
    snapshot = find_module.FreshnessSnapshot(tmp_path)
    assert lexstore.search_bm25(
        tmp_path,
        "outside bounded needle",
        10,
        scope="vault",
        freshness=snapshot.vault(),
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("warm widening must not rebuild or query Python BM25")

    monkeypatch.setattr(bm25, "search", forbidden)
    monkeypatch.setattr(find_module, "_outside_kb_keyword_paths", forbidden)

    hits = find_module._find_outside_kb(
        tmp_path,
        query="outside bounded needle",
        query_norm="outside bounded needle",
        types=None,
        projects=None,
        tags=None,
        limit=5,
        snapshot=snapshot,
    )

    assert [hit.path for hit in hits] == ["Reference/target.md"]
    assert hits[0].outside_kb is True


def test_unavailable_outside_lexstore_never_walks_the_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(tmp_path, "Reference/target.md", "outside unavailable needle")
    monkeypatch.setattr(lexstore, "search_bm25", lambda *_args, **_kwargs: None)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an unavailable sidecar must not trigger a vault walk")

    monkeypatch.setattr(bm25, "search", forbidden)
    monkeypatch.setattr(find_module, "_outside_kb_keyword_paths", forbidden)
    failed: list[str] = []

    hits = find_module._find_outside_kb(
        tmp_path,
        query="outside unavailable needle",
        query_norm="outside unavailable needle",
        types=None,
        projects=None,
        tags=None,
        limit=5,
        failed_out=failed,
    )

    assert hits == []
    assert failed == ["outside_kb_lexical"]

"""Query-cache reuse across bounded recall-freshness deltas."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import bm25, find as find_module, freshness
from exomem.vault import walk_vault_md


def _write_page(root: Path, rel: str, *, body: str, updated: str = "2026-08-20") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        f"title: {path.stem}\n"
        f"updated: {updated}\n"
        "---\n"
        f"# {path.stem}\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def _seed(root: Path) -> None:
    kb = root / "Knowledge Base"
    freshness.seed(
        root,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(kb)),
    )
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in walk_vault_md(root)),
    )


def _query(root: Path, query: str = "needle") -> tuple[list, dict]:
    timings = find_module.FindTimings()
    hits = find_module.find(
        root,
        query=query,
        limit=10,
        scope="kb",
        mode="keyword",
        graph=False,
        rerank=False,
        timings=timings,
    )
    return hits, timings.as_dict()


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    find_module.clear_cache()
    bm25.clear_cache()
    yield
    find_module.clear_cache()
    bm25.clear_cache()


def test_identical_repeated_query_hits_cache_without_intervening_write(tmp_path: Path) -> None:
    _write_page(tmp_path, "Knowledge Base/Notes/match.md", body="needle target")
    _seed(tmp_path)

    first, first_timings = _query(tmp_path)
    second, second_timings = _query(tmp_path)

    assert first_timings["cache"]["hit"] is False
    assert second_timings["cache"]["hit"] is True
    assert [hit.as_dict() for hit in second] == [hit.as_dict() for hit in first]


def test_freshness_key_is_deterministic_for_identical_inputs(tmp_path: Path) -> None:
    _write_page(tmp_path, "Knowledge Base/Notes/match.md", body="needle target")
    _seed(tmp_path)

    def current_key() -> tuple:
        return find_module._freshness_key(
            tmp_path,
            scope="kb",
            query_norm="needle",
            mode="keyword",
            graph=False,
            snapshot=find_module.FreshnessSnapshot(tmp_path),
        )

    assert current_key() == current_key()


def test_unrelated_write_does_not_evict_cached_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(tmp_path, "Knowledge Base/Notes/match.md", body="needle target")
    _seed(tmp_path)
    calls = {"count": 0}
    original = find_module._find_keyword

    def counting(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(find_module, "_find_keyword", counting)
    first, _ = _query(tmp_path)
    unrelated = _write_page(
        tmp_path,
        "Knowledge Base/Notes/unrelated.md",
        body="orthogonal material with no matching token",
    )
    freshness.on_files_changed(tmp_path, changed=[unrelated])

    second, timings = _query(tmp_path)

    assert timings["cache"]["hit"] is True
    assert calls["count"] == 1
    assert [hit.as_dict() for hit in second] == [hit.as_dict() for hit in first]


def test_matching_write_evicts_cached_query_and_returns_new_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/original.md",
        body="needle original",
        updated="2026-08-19",
    )
    _seed(tmp_path)
    calls = {"count": 0}
    original = find_module._find_keyword

    def counting(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(find_module, "_find_keyword", counting)
    _query(tmp_path)
    matching = _write_page(
        tmp_path,
        "Knowledge Base/Notes/new-match.md",
        body="needle newly matching page",
    )
    freshness.on_files_changed(tmp_path, changed=[matching])

    hits, timings = _query(tmp_path)

    assert timings["cache"]["hit"] is False
    assert calls["count"] == 2
    assert any(hit.path.endswith("new-match.md") for hit in hits)


def test_scene_frame_change_evicts_cached_emitted_parent(tmp_path: Path) -> None:
    parent = _write_page(
        tmp_path,
        "Knowledge Base/Evidence/video.mp4.md",
        body="parent transcript",
    )
    child = tmp_path / "Knowledge Base/Evidence/video.mp4.frames/scene-000.jpg.md"
    child.parent.mkdir(parents=True, exist_ok=True)
    child.write_text(
        "---\n"
        "type: source\n"
        "title: scene frame\n"
        "updated: 2026-08-20\n"
        "parent_media: Knowledge Base/Evidence/video.mp4\n"
        "media_file: Knowledge Base/Evidence/video.mp4.frames/scene-000.jpg\n"
        "frame_ts: 1.0\n"
        "---\n"
        "# scene frame\n\nneedle on the captured slide\n",
        encoding="utf-8",
    )
    _seed(tmp_path)
    first, _ = _query(tmp_path)
    assert [hit.path for hit in first] == [parent.relative_to(tmp_path).as_posix()]

    child.write_text(
        "---\ntype: source\ntitle: retired frame\nupdated: 2026-08-20\n---\n"
        "# retired frame\n\nno longer contributes to the parent\n",
        encoding="utf-8",
    )
    freshness.on_files_changed(tmp_path, changed=[child])

    second, timings = _query(tmp_path)

    assert timings["cache"]["hit"] is False
    assert second == []


def test_access_policy_change_always_evicts_cached_query(tmp_path: Path) -> None:
    secret = _write_page(
        tmp_path,
        "Knowledge Base/Private/secret.md",
        body="needle private material",
    )
    _write_page(tmp_path, "Knowledge Base/Notes/public.md", body="needle public material")
    _seed(tmp_path)
    first, _ = _query(tmp_path)
    assert any(hit.path == secret.relative_to(tmp_path).as_posix() for hit in first)

    policy = tmp_path / "Knowledge Base" / "_access.yaml"
    policy.write_text("excluded:\n  - Private\n", encoding="utf-8")
    freshness.on_files_changed(tmp_path, changed=[policy])

    second, timings = _query(tmp_path)

    assert timings["cache"]["hit"] is False
    assert all(hit.path != secret.relative_to(tmp_path).as_posix() for hit in second)


def test_incomplete_recall_delta_always_misses(tmp_path: Path) -> None:
    _write_page(tmp_path, "Knowledge Base/Notes/match.md", body="needle target")
    _seed(tmp_path)
    _query(tmp_path)
    with find_module._FIND_CACHE_LOCK:
        cache_key = next(iter(find_module._FIND_CACHE_CHECKPOINTS))
        checkpoints = find_module._FIND_CACHE_CHECKPOINTS[cache_key]
        find_module._FIND_CACHE_CHECKPOINTS[cache_key] = tuple(
            (scope, checkpoint._replace(instance_id="foreign-instance"))
            for scope, checkpoint in checkpoints
        )
    unrelated = _write_page(
        tmp_path,
        "Knowledge Base/Notes/unrelated.md",
        body="orthogonal material",
    )
    freshness.on_files_changed(tmp_path, changed=[unrelated])

    _hits, timings = _query(tmp_path)

    assert timings["cache"]["hit"] is False

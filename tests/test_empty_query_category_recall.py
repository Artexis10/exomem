"""Task 1.2 — RED: empty-query direct eligible-path iteration + emission parity.

Pins OpenSpec change ``restore-indexed-category-recall`` decision 3 and the
spec *Empty-Query Category Recall Avoids A Corpus Walk*:

* an empty query against a complete catalog and a safe indexed category plan
  iterates the catalog's eligible parents directly — no Markdown scope walk;
* a non-empty query intersects text candidates with those eligible paths
  without walking the scope merely to rediscover category candidates;
* navigation, access policy, and scene-frame emitted-parent rules stay enforced
  through the indexed path, matching the full-scan oracle.

RED today because ``find`` resolves ``eligible_paths`` via
``_eligible_filter_paths``, which walks and parses every Markdown parent
whenever the plan carries a unit (category/kind) predicate.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from exomem import find as find_module
from exomem import freshness, lexstore
from exomem.structured_filters import compile_filter

needs_fts5 = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="this SQLite build lacks FTS5"
)


@pytest.fixture(autouse=True)
def _fresh_state() -> Any:
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    freshness.clear()
    yield
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    freshness.clear()


def _write_note(root: Path, rel_path: str, body: str, *, status: str = "active") -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    page_id = uuid.uuid5(uuid.NAMESPACE_URL, f"empty-query-category:{rel_path}")
    path.write_text(
        "---\n"
        "type: insight\n"
        f"title: {path.stem}\n"
        f"exomem_id: {page_id}\n"
        f"status: {status}\n"
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


def _forbid_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("indexed category recall must not walk the Markdown scope")

    monkeypatch.setattr(find_module, "_walk_md", forbidden)


@needs_fts5
def test_empty_query_category_hydrates_eligible_parents_without_a_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _write_note(
        tmp_path,
        "Knowledge Base/Notes/eligible.md",
        "- [config] eligible parent ^eligible",
    )
    pages = [target]
    for index in range(20):
        pages.append(
            _write_note(
                tmp_path,
                f"Knowledge Base/Notes/other-{index:03d}.md",
                f"- [observation] not a config candidate {index} ^o{index}",
            )
        )
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)
    _forbid_walk(monkeypatch)

    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        graph=False,
        result_level="page",
        categories=["config"],
        limit=20,
    )

    assert [hit.path for hit in hits] == ["Knowledge Base/Notes/eligible.md"]


@needs_fts5
def test_nonempty_query_intersects_text_candidates_with_eligible_paths_no_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _write_note(
        tmp_path,
        "Knowledge Base/Notes/needle-config.md",
        "- [config] shared needle marker ^needle",
    )
    # Same needle, wrong category — must be excluded by the category seed.
    other = _write_note(
        tmp_path,
        "Knowledge Base/Notes/needle-rule.md",
        "- [rule] shared needle marker ^rule",
    )
    _seed_live_freshness(tmp_path, [target, other])
    lexstore.ensure_fresh(tmp_path)
    _forbid_walk(monkeypatch)
    opened: list[str] = []
    original_get = find_module._CACHE.get

    def observed_get(path: Path, root: Path) -> Any:
        opened.append(path.relative_to(root).as_posix())
        return original_get(path, root)

    monkeypatch.setattr(find_module._CACHE, "get", observed_get)

    hits = find_module.find(
        tmp_path,
        query="needle marker",
        scope="kb-only",
        mode="keyword",
        graph=False,
        result_level="page",
        categories=["config"],
        limit=20,
    )

    assert [hit.path for hit in hits] == ["Knowledge Base/Notes/needle-config.md"]
    assert "Knowledge Base/Notes/needle-rule.md" not in opened


@needs_fts5
def test_empty_query_category_recall_emits_parent_and_enforces_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scene-frame child candidate emits its parent video through the indexed
    path, and navigation files never surface — exactly the scan oracle's rules,
    without a corpus walk."""
    parent = tmp_path / "Knowledge Base" / "Evidence" / "clip.mp4.md"
    child = tmp_path / "Knowledge Base" / "Evidence" / "clip.mp4.frames" / "frame.jpg.md"
    nav = tmp_path / "Knowledge Base" / "Evidence" / "index.md"
    parent.parent.mkdir(parents=True, exist_ok=True)
    child.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        "---\ntype: source\nstatus: active\nmedia_type: video\n"
        f"exomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, 'clip-parent')}\n"
        "updated: 2026-07-22\n---\n# Clip\n\n- [config] parent-level candidate ^clip\n",
        encoding="utf-8",
    )
    child.write_text(
        "---\ntype: source\nparent_media: Knowledge Base/Evidence/clip.mp4\n"
        "media_type: image\nstatus: active\n"
        f"exomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, 'clip-frame')}\n"
        "updated: 2026-07-22\n---\n# Frame\n\n- [config] frame-level candidate ^frame\n",
        encoding="utf-8",
    )
    nav.write_text(
        "---\ntype: source\nstatus: active\n---\n# Index\n\n- [config] navigation noise ^nav\n",
        encoding="utf-8",
    )
    _seed_live_freshness(tmp_path, [parent, child, nav])
    lexstore.ensure_fresh(tmp_path)
    _forbid_walk(monkeypatch)

    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        graph=False,
        result_level="page",
        categories=["config"],
        limit=20,
    )
    paths = {hit.path for hit in hits}

    # The frame child collapses onto its emitted parent video; the navigation
    # file is excluded regardless of its matching category unit.
    assert "Knowledge Base/Evidence/clip.mp4.md" in paths
    assert "Knowledge Base/Evidence/clip.mp4.frames/frame.jpg.md" not in paths
    assert "Knowledge Base/Evidence/index.md" not in paths


@needs_fts5
def test_indexed_empty_query_recall_matches_the_full_scan_oracle(
    tmp_path: Path,
) -> None:
    """The indexed candidate hits equal the canonical full-scan eligibility
    oracle (``_eligible_filter_paths``) for the same safe category plan."""
    pages = [
        _write_note(tmp_path, "Knowledge Base/Notes/a.md", "- [config] alpha ^a"),
        _write_note(tmp_path, "Knowledge Base/Notes/b.md", "- [config] bravo ^b"),
        _write_note(tmp_path, "Knowledge Base/Notes/c.md", "- [rule] charlie ^c"),
    ]
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)

    oracle = find_module._eligible_filter_paths(
        tmp_path,
        scope="kb",
        plan=compile_filter({"unit.category": {"$eq": "config"}}),
    )
    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="page",
        categories=["config"],
        limit=20,
    )

    assert {hit.path for hit in hits} == {
        path for path in oracle if path.endswith(("a.md", "b.md"))
    }
    assert {hit.path for hit in hits} == {
        "Knowledge Base/Notes/a.md",
        "Knowledge Base/Notes/b.md",
    }


@needs_fts5
def test_nonempty_frame_text_inherits_parent_category_without_a_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A text hit on a frame remains eligible when the emitted video parent,
    rather than the child itself, owns the requested category."""
    parent = tmp_path / "Knowledge Base" / "Evidence" / "clip.mp4.md"
    child = tmp_path / "Knowledge Base" / "Evidence" / "clip.mp4.frames" / "frame.jpg.md"
    parent.parent.mkdir(parents=True, exist_ok=True)
    child.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        "---\ntype: source\nstatus: active\nmedia_type: video\n"
        f"exomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, 'query-parent')}\n"
        "updated: 2026-07-22\n---\n# Clip\n\n- [config] parent category ^parent\n",
        encoding="utf-8",
    )
    child.write_text(
        "---\ntype: source\nparent_media: Knowledge Base/Evidence/clip.mp4\n"
        "media_type: image\nstatus: active\n"
        f"exomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, 'query-frame')}\n"
        "updated: 2026-07-22\n---\n# Frame\n\n"
        "- [observation] unique telescope needle on the frame ^frame\n",
        encoding="utf-8",
    )
    _seed_live_freshness(tmp_path, [parent, child])
    lexstore.ensure_fresh(tmp_path)
    _forbid_walk(monkeypatch)

    hits = find_module.find(
        tmp_path,
        query="unique telescope needle",
        scope="kb-only",
        mode="keyword",
        graph=False,
        result_level="page",
        categories=["config"],
        limit=20,
    )

    assert [hit.path for hit in hits] == ["Knowledge Base/Evidence/clip.mp4.md"]

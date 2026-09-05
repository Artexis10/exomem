"""find grouping for scene frames: one video = one hit, frame text as the why."""

from __future__ import annotations

from pathlib import Path

from exomem import find as find_mod
from exomem import find_candidates
from exomem import lexstore, preserve, scene_frames, semantic_contract
from exomem.find_types import ParsedPage
from exomem.embeddings import Scene
from exomem.find import find


class _FakeImg:
    size = (640, 360)

    def resize(self, size):
        return self

    def convert(self, mode):
        return self

    def save(self, path, format=None, quality=None):
        Path(path).write_bytes(b"\xff\xd8x")


VIDEO_REL = "Knowledge Base/Evidence/Test/clips/demo.mp4"


def _setup_video_with_frames(vault: Path) -> Path:
    """A video sidecar (transcript) + two OCR'd scene frames via the real writer."""
    video = vault / VIDEO_REL
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00video")
    (vault / (VIDEO_REL + ".md")).write_text(
        "---\n"
        "type: source\n"
        "source_type: other\n"
        "captured: 2026-07-01\n"
        "media_type: video\n"
        f"evidence_file: {VIDEO_REL}\n"
        "extracted_by: whisper\n"
        "tags: [evidence]\n"
        "---\n\n# Evidence: demo.mp4\n\n## Extracted text\n\n"
        "welcome to the quarterly planning walkthrough\n",
        encoding="utf-8",
    )
    pairs = scene_frames.write_scene_frames(
        vault,
        video,
        [
            (Scene(start_ts=0.0, end_ts=10.0, rep_ts=5.0, boundary_score=0.0), _FakeImg()),
            (Scene(start_ts=10.0, end_ts=90.0, rep_ts=75.0, boundary_score=0.6), _FakeImg()),
        ],
    )
    assert len(pairs) == 2
    # Fill frame OCR through the real seam (worker path).
    preserve.update_sidecar_extraction(
        vault, pairs[0][1], text="dashboard shows unobtainium flux levels", engine="tesseract"
    )
    preserve.update_sidecar_extraction(
        vault, pairs[1][1], text="stack trace NullPointerException in FluxService",
        engine="tesseract",
    )
    return video


def test_frame_ocr_match_groups_into_one_video_hit(vault: Path) -> None:
    _setup_video_with_frames(vault)
    hits = find(vault, query="unobtainium flux", mode="hybrid")
    assert len(hits) == 1
    h = hits[0].as_dict()
    assert h["path"] == VIDEO_REL + ".md"
    assert h["media_type"] == "video"
    assert h["scene_frame"].startswith(VIDEO_REL + ".frames/scene-000")
    assert h["scene_match_at"] == "0:05"
    assert "unobtainium" in h["excerpt"]  # the frame's OCR text is the why


def test_multiple_matching_frames_still_one_hit(vault: Path) -> None:
    _setup_video_with_frames(vault)
    # "flux" appears in BOTH frames' OCR text.
    hits = find(vault, query="flux", mode="hybrid")
    paths = [h.path for h in hits]
    assert paths.count(VIDEO_REL + ".md") == 1
    assert not any(".frames/" in p for p in paths)


def test_parent_and_frame_match_fuse_as_one(vault: Path) -> None:
    _setup_video_with_frames(vault)
    # "walkthrough" in the video transcript; "unobtainium" in a frame. A query
    # hitting both must still yield one candidate for the video.
    hits = find(vault, query="quarterly planning walkthrough", mode="hybrid")
    paths = [h.path for h in hits]
    assert paths.count(VIDEO_REL + ".md") == 1


def test_file_types_video_matches_via_frame_text(vault: Path) -> None:
    _setup_video_with_frames(vault)
    hits = find(vault, query="unobtainium flux", mode="hybrid", file_types=["video"])
    assert [h.path for h in hits] == [VIDEO_REL + ".md"]


def test_orphan_frame_surfaces_standalone(vault: Path) -> None:
    _setup_video_with_frames(vault)
    (vault / (VIDEO_REL + ".md")).unlink()  # parent sidecar gone → orphan frames
    hits = find(vault, query="unobtainium flux", mode="hybrid")
    assert len(hits) == 1
    assert ".frames/scene-000" in hits[0].path  # the frame's own sidecar


def test_keyword_mode_groups_frames_too(vault: Path) -> None:
    _setup_video_with_frames(vault)
    hits = find(vault, query="unobtainium", mode="keyword")
    assert len(hits) == 1
    h = hits[0].as_dict()
    assert h["path"] == VIDEO_REL + ".md"
    assert h["scene_frame"].startswith(VIDEO_REL + ".frames/scene-000")


def test_plain_images_unaffected(vault: Path) -> None:
    _setup_video_with_frames(vault)
    photo = vault / "Knowledge Base/Evidence/Test/clips/whiteboard.jpg"
    photo.write_bytes(b"\xff\xd8x")
    sidecar = photo.with_name("whiteboard.jpg.md")
    sidecar.write_text(
        "---\ntype: source\nsource_type: other\ncaptured: 2026-07-01\n"
        "media_type: image\n"
        "evidence_file: Knowledge Base/Evidence/Test/clips/whiteboard.jpg\n"
        "extracted_by: tesseract\ntags: [evidence]\n---\n\n"
        "# Evidence: whiteboard.jpg\n\n## Extracted text\n\n"
        "architecture sketch for the ingestion pipeline\n",
        encoding="utf-8",
    )
    # This test writes out of band while the suite watcher is disabled. Publish
    # the same corpus event a real watcher/self-write path would emit so the
    # warmed recall checkpoint admits the new sidecar.
    semantic_contract.publish_corpus_files_changed(vault, changed=(sidecar,))
    hits = find(vault, query="architecture sketch ingestion", mode="hybrid")
    assert len(hits) == 1
    h = hits[0].as_dict()
    assert h["path"].endswith("whiteboard.jpg.md")
    assert "scene_frame" not in h


def test_collapse_keeps_parent_best_rank(vault: Path) -> None:
    """When both the parent and a frame rank in a lane, the parent keeps its
    better (earlier) position and the frame's aux values don't clobber it."""
    _setup_video_with_frames(vault)
    attribution: dict = {}
    scores = {VIDEO_REL + ".md": 0.9}
    frame_sidecar = None
    frames_dir = vault / (VIDEO_REL + ".frames")
    for f in frames_dir.glob("scene-000-*.jpg.md"):
        frame_sidecar = f.relative_to(vault).as_posix()
    assert frame_sidecar is not None
    scores[frame_sidecar] = 0.4
    ranking = [VIDEO_REL + ".md", frame_sidecar]
    collapsed = find_mod._collapse_frame_children(ranking, vault, attribution, scores)
    assert collapsed == [VIDEO_REL + ".md"]
    assert scores[VIDEO_REL + ".md"] == 0.9  # parent's own score kept (keep-best)
    assert (VIDEO_REL + ".md") in attribution  # frame still attributed for enrichment


def _frame_page(vault: Path, rel: str, parent_media: str | None) -> ParsedPage:
    return ParsedPage(
        path=vault / rel,
        rel_path=rel,
        frontmatter={
            "type": "source",
            **({"parent_media": parent_media} if parent_media else {}),
            "evidence_file": rel.removesuffix(".md"),
            "frame_ts": 5.0,
        },
        body="frame",
        title="frame",
        mtime=0.0,
    )


def test_collapse_hints_skip_ordinary_hydration_and_keep_child_authoritative(vault: Path) -> None:
    parent = "Knowledge Base/video.mp4.md"
    child = "Knowledge Base/video.mp4.frames/scene-000-005.000.jpg.md"
    ordinary = "Knowledge Base/ordinary.md"
    for rel in (parent, child, ordinary):
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\ntype: insight\n---\n# page\n", encoding="utf-8")

    calls: list[str] = []

    def page_of(rel: str) -> ParsedPage | None:
        calls.append(rel)
        return _frame_page(vault, rel, "Knowledge Base/video.mp4") if rel == child else None

    attribution: dict[str, tuple[str, float | None]] = {}
    collapsed = find_candidates.collapse_frame_children(
        [ordinary, child],
        vault,
        page_of,
        attribution,
        parent_hints={ordinary: None, child: parent},
        recall_paths={ordinary, child, parent},
    )

    assert collapsed == [ordinary, parent]
    assert calls == [child]
    assert attribution == {parent: (child.removesuffix(".md"), 5.0)}


def test_collapse_large_hints_match_legacy_and_hydrate_only_child(vault: Path) -> None:
    parent = "Knowledge Base/video.mp4.md"
    child = "Knowledge Base/video.mp4.frames/scene-000-005.000.jpg.md"
    ordinary = [f"Knowledge Base/ordinary-{number:03d}.md" for number in range(449)]
    for rel in [parent, child, *ordinary]:
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\ntype: insight\n---\n# page\n", encoding="utf-8")
    ranking = [*ordinary, child]
    recall_paths = set(ranking) | {parent}

    def page_of(rel: str) -> ParsedPage | None:
        return _frame_page(vault, rel, "Knowledge Base/video.mp4") if rel == child else None

    legacy_attribution: dict[str, tuple[str, float | None]] = {}
    legacy_aux = {child: 0.4, ordinary[0]: 0.9}
    legacy = find_candidates.collapse_frame_children(
        ranking, vault, page_of, legacy_attribution, legacy_aux, recall_paths=recall_paths
    )

    calls: list[str] = []

    def counting_page_of(rel: str) -> ParsedPage | None:
        calls.append(rel)
        return page_of(rel)

    hinted_attribution: dict[str, tuple[str, float | None]] = {}
    hinted_aux = {child: 0.4, ordinary[0]: 0.9}
    hinted = find_candidates.collapse_frame_children(
        ranking,
        vault,
        counting_page_of,
        hinted_attribution,
        hinted_aux,
        parent_hints={**{rel: None for rel in ordinary}, child: parent},
        recall_paths=recall_paths,
    )

    assert calls == [child]
    assert hinted == legacy
    assert hinted_attribution == legacy_attribution
    assert hinted_aux == legacy_aux


def test_collapse_hinted_child_stays_standalone_when_parent_disappears(vault: Path) -> None:
    parent = "Knowledge Base/video.mp4.md"
    child = "Knowledge Base/video.mp4.frames/scene-000-005.000.jpg.md"
    child_path = vault / child
    child_path.parent.mkdir(parents=True, exist_ok=True)
    child_path.write_text("---\ntype: source\n---\n# frame\n", encoding="utf-8")

    collapsed = find_candidates.collapse_frame_children(
        [child],
        vault,
        lambda rel: _frame_page(vault, rel, None),
        {},
        parent_hints={child: parent},
        recall_paths={child, parent},
    )

    assert collapsed == [child]


def test_incomplete_parent_hints_fall_back_to_legacy_hydration(
    vault: Path, monkeypatch
) -> None:
    _setup_video_with_frames(vault)
    calls = 0

    def incomplete(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return lexstore.CatalogQueryResult(
            None, lexstore.CatalogReadiness("stale", False, lexstore.backend())
        )

    monkeypatch.setattr(lexstore, "emitted_parent_hints_result", incomplete)
    hits = find(vault, query="unobtainium flux", mode="hybrid")

    assert [hit.path for hit in hits] == [VIDEO_REL + ".md"]
    assert calls == 1

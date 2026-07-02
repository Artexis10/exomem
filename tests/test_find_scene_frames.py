"""find grouping for scene frames: one video = one hit, frame text as the why."""

from __future__ import annotations

from pathlib import Path

from kb_mcp import find as find_mod
from kb_mcp import preserve, scene_frames
from kb_mcp.embeddings import Scene
from kb_mcp.find import find


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
    photo.with_name("whiteboard.jpg.md").write_text(
        "---\ntype: source\nsource_type: other\ncaptured: 2026-07-01\n"
        "media_type: image\n"
        "evidence_file: Knowledge Base/Evidence/Test/clips/whiteboard.jpg\n"
        "extracted_by: tesseract\ntags: [evidence]\n---\n\n"
        "# Evidence: whiteboard.jpg\n\n## Extracted text\n\n"
        "architecture sketch for the ingestion pipeline\n",
        encoding="utf-8",
    )
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

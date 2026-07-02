"""Scene-frame writer: JPEG + sidecar persistence, naming, lifecycle (PIL faked)."""

from __future__ import annotations

from pathlib import Path

from exomem import scene_frames
from exomem.embeddings import Scene


class _FakeImg:
    """PIL stand-in for the writer: size/resize/convert/save."""

    size = (1920, 1080)

    def __init__(self, fail_save: bool = False) -> None:
        self._fail_save = fail_save

    def resize(self, size: tuple[int, int]) -> _FakeImg:
        assert max(size) <= scene_frames.JPEG_MAX_SIDE
        return self

    def convert(self, mode: str) -> _FakeImg:
        return self

    def save(self, path: str, format: str | None = None, quality: int | None = None) -> None:
        if self._fail_save:
            raise OSError("disk full")
        Path(path).write_bytes(b"\xff\xd8fakejpg")


def _scene(rep: float) -> Scene:
    return Scene(start_ts=rep - 2, end_ts=rep + 2, rep_ts=rep, boundary_score=0.5)


def _video(vault: Path) -> Path:
    p = vault / "Knowledge Base/Evidence/Test/clips/demo.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00video")
    return p


def test_frame_filename_roundtrip() -> None:
    name = scene_frames.frame_filename(3, 734.5)
    assert name == "scene-003-t734500ms.jpg"
    assert scene_frames.parse_frame_ts(name) == 734.5
    assert scene_frames.parse_frame_ts("demo.mp4") is None
    assert scene_frames.parse_frame_ts("scene-003-t734500ms.jpg.md") is None


def test_write_creates_jpeg_and_sidecar(vault: Path) -> None:
    video = _video(vault)
    pairs = scene_frames.write_scene_frames(
        vault, video, [(_scene(10.0), _FakeImg()), (_scene(75.0), _FakeImg())]
    )
    assert len(pairs) == 2
    jpg, sidecar = pairs[0]
    assert jpg.exists() and sidecar.exists()
    assert jpg.parent == scene_frames.frames_dir_for(video)
    content = sidecar.read_text(encoding="utf-8")
    assert "media_type: image" in content
    assert "extracted_by: pending" in content
    assert "parent_media: Knowledge Base/Evidence/Test/clips/demo.mp4" in content
    assert "frame_ts: 10.0" in content
    assert "scene-frame" in content  # tag
    assert "evidence_file: Knowledge Base/Evidence/Test/clips/demo.mp4.frames/" in content
    assert "01:15" in pairs[1][1].read_text(encoding="utf-8")  # mm:ss caption for 75s


def test_rewrite_clears_stale_frames(vault: Path) -> None:
    video = _video(vault)
    scene_frames.write_scene_frames(
        vault, video, [(_scene(t), _FakeImg()) for t in (5.0, 15.0, 25.0)]
    )
    d = scene_frames.frames_dir_for(video)
    assert len(list(d.glob("scene-*.jpg"))) == 3
    scene_frames.write_scene_frames(vault, video, [(_scene(40.0), _FakeImg())])
    assert len(list(d.glob("scene-*.jpg"))) == 1
    assert len(list(d.glob("scene-*.jpg.md"))) == 1
    assert scene_frames.parse_frame_ts(next(d.glob("scene-*.jpg")).name) == 40.0


def test_failed_save_skips_frame_without_raising(vault: Path) -> None:
    video = _video(vault)
    pairs = scene_frames.write_scene_frames(
        vault, video, [(_scene(10.0), _FakeImg(fail_save=True)), (_scene(20.0), _FakeImg())]
    )
    assert len(pairs) == 1  # bad frame skipped, good frame persisted
    assert scene_frames.parse_frame_ts(pairs[0][0].name) == 20.0


def test_video_outside_vault_is_skipped(vault: Path, tmp_path: Path) -> None:
    stray = tmp_path / "elsewhere" / "clip.mp4"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"\x00")
    assert scene_frames.write_scene_frames(vault, stray, [(_scene(1.0), _FakeImg())]) == []


def test_nearest_frame_resolves_by_filename(vault: Path) -> None:
    video = _video(vault)
    scene_frames.write_scene_frames(
        vault, video, [(_scene(t), _FakeImg()) for t in (5.0, 60.0, 300.0)]
    )
    rel = "Knowledge Base/Evidence/Test/clips/demo.mp4"
    hit = scene_frames.nearest_frame(vault, rel, 70.0)
    assert hit is not None
    jpg_rel, fts = hit
    assert fts == 60.0
    assert jpg_rel.startswith(rel + scene_frames.FRAMES_DIR_SUFFIX + "/")
    assert scene_frames.nearest_frame(vault, "Knowledge Base/Evidence/nope.mp4", 1.0) is None


def test_clear_scene_frames_leaves_foreign_files(vault: Path) -> None:
    video = _video(vault)
    scene_frames.write_scene_frames(vault, video, [(_scene(5.0), _FakeImg())])
    d = scene_frames.frames_dir_for(video)
    foreign = d / "notes.md"
    foreign.write_text("mine", encoding="utf-8")
    removed = scene_frames.clear_scene_frames(vault, video)
    assert removed == 2  # jpg + sidecar
    assert foreign.exists()  # only owned scene-* files are touched

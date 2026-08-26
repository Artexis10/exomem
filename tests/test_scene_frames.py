"""Scene-frame writer: JPEG + sidecar persistence, naming, lifecycle (PIL faked)."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from exomem import scene_frames
from exomem import vault as vault_module
from exomem.embeddings import Scene
from exomem.governance import catalog_publication, companions, projected_retrieval


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

    def save(self, target, format: str | None = None, quality: int | None = None) -> None:
        if self._fail_save:
            raise OSError("disk full")
        payload = b"\xff\xd8fakejpg"
        if hasattr(target, "write"):
            target.write(payload)
        else:
            Path(target).write_bytes(payload)


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


def test_frame_timestamp_ms_is_derived_once_with_bounded_ties_to_even() -> None:
    assert scene_frames.frame_timestamp_ms(0.0005) == 0
    assert scene_frames.frame_timestamp_ms(0.0015) == 2
    assert scene_frames.frame_filename(1, 0.0015) == "scene-001-t2ms.jpg"
    for invalid in (
        -0.001,
        math.inf,
        -math.inf,
        math.nan,
        4_294_967.296,
    ):
        with pytest.raises(ValueError, match="timestamp"):
            scene_frames.frame_timestamp_ms(invalid)


def test_parent_clip_samples_must_match_the_exact_scene_set(vault: Path) -> None:
    video = _video(vault)
    samples = (projected_retrieval.ProjectionClipSample(9_000, (1.0, 0.0)),)

    with pytest.raises(ValueError, match="do not match"):
        scene_frames.write_scene_frames(
            vault,
            video,
            [(_scene(10.0), _FakeImg())],
            parent_clip_samples=samples,
        )

    assert not scene_frames.frames_dir_for(video).exists()


def test_parent_sidecar_drift_rolls_back_scene_and_clip_publication(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = _video(vault)
    sidecar = video.with_name(f"{video.name}.md")
    sidecar.write_text("---\nmedia_type: video\n---\n\nbefore\n", encoding="utf-8")
    samples = (projected_retrieval.ProjectionClipSample(10_000, (1.0, 0.0)),)
    real_batch = scene_frames.batch_atomic_write

    def drift_then_write(*args, **kwargs):
        sidecar.write_text("---\nmedia_type: video\n---\n\nafter\n", encoding="utf-8")
        return real_batch(*args, **kwargs)

    monkeypatch.setattr(scene_frames, "batch_atomic_write", drift_then_write)

    assert scene_frames.write_scene_frames(
        vault,
        video,
        [(_scene(10.0), _FakeImg())],
        parent_clip_samples=samples,
    ) == []
    assert not scene_frames.frames_dir_for(video).exists()


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


def test_write_creates_exact_classified_scene_frame_companion(vault: Path) -> None:
    video = _video(vault)

    pairs = scene_frames.write_scene_frames(vault, video, [(_scene(10.0), _FakeImg())])

    assert len(pairs) == 1
    jpg, sidecar = pairs[0]
    jpg_rel = jpg.relative_to(vault).as_posix()
    classified = companions.classify(vault, jpg_rel)
    assert classified.projects == ()
    assert classified.tags == ()
    assert classified.types == ()
    assert classified.classes == ()
    assert {snapshot.role for snapshot in classified.identities} == {
        "artifact",
        "companion",
        "parent",
    }
    content = sidecar.read_text(encoding="utf-8")
    assert "artifact_class: scene_frame" in content
    assert f"artifact_sha256: {hashlib.sha256(jpg.read_bytes()).hexdigest()}" in content
    assert "parent_path: Knowledge Base/Evidence/Test/clips/demo.mp4" in content
    assert f"parent_sha256: {hashlib.sha256(video.read_bytes()).hexdigest()}" in content
    assert "frame_timestamp_ms: 10000" in content


def test_write_rolls_back_jpeg_when_sidecar_publication_fails(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = _video(vault)
    real_hook = vault_module._after_batch_destination_published

    def fail_after_sidecar(path: Path) -> None:
        real_hook(path)
        if path.name.endswith(".jpg.md"):
            raise RuntimeError("injected scene sidecar failure")

    monkeypatch.setattr(
        vault_module,
        "_after_batch_destination_published",
        fail_after_sidecar,
    )

    assert scene_frames.write_scene_frames(
        vault, video, [(_scene(10.0), _FakeImg())]
    ) == []
    frame_dir = scene_frames.frames_dir_for(video)
    assert not list(frame_dir.glob("scene-*.jpg"))
    assert not list(frame_dir.glob("scene-*.jpg.md"))


def test_write_refuses_parent_drift_before_any_frame_is_visible(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = _video(vault)
    real_batch = scene_frames.batch_atomic_write

    def drift_then_write(*args, **kwargs):
        video.write_bytes(b"changed video")
        return real_batch(*args, **kwargs)

    monkeypatch.setattr(scene_frames, "batch_atomic_write", drift_then_write)

    assert scene_frames.write_scene_frames(
        vault, video, [(_scene(10.0), _FakeImg())]
    ) == []
    frame_dir = scene_frames.frames_dir_for(video)
    assert not list(frame_dir.glob("scene-*.jpg"))
    assert not list(frame_dir.glob("scene-*.jpg.md"))


def test_v4_preflight_refusal_does_not_create_frame_bytes(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = _video(vault)

    def refuse(*_args, **_kwargs):
        raise catalog_publication.CatalogPublicationError("model family unavailable")

    monkeypatch.setattr(
        catalog_publication,
        "prepare_planned_markdown_batch",
        refuse,
    )

    with pytest.raises(catalog_publication.CatalogCommitError) as blocked:
        scene_frames.write_scene_frames(vault, video, [(_scene(10.0), _FakeImg())])

    assert blocked.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert not scene_frames.frames_dir_for(video).exists()


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

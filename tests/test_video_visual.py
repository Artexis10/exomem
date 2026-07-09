"""Video visual search — no-audio handling + CLIP keyframe embedding (engines stubbed)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from exomem import backfill, embeddings, extract, media_worker, preserve, scene_frames
from exomem.embeddings import Scene


def test_transcribe_silent_video_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # A video with no audio stream → empty transcript, engine "no-audio", never raises,
    # and Whisper is never even loaded.
    monkeypatch.setattr(extract, "_has_audio_stream", lambda p: False)
    monkeypatch.setattr(extract, "_get_whisper", lambda: (_ for _ in ()).throw(AssertionError("loaded whisper")))
    r = extract._transcribe(Path("clip.mp4"), "video")
    assert r.text == "" and r.engine == "no-audio" and r.media_type == "video"


def _three_frames():
    """Stub embed_video_frames output: three keyframes at distinct timestamps."""
    return [
        (5.0, np.eye(1, embeddings.CLIP_DIM, 0, dtype=np.float32)[0]),
        (15.0, np.eye(1, embeddings.CLIP_DIM, 1, dtype=np.float32)[0]),
        (25.0, np.eye(1, embeddings.CLIP_DIM, 2, dtype=np.float32)[0]),
    ]


def test_scene_frame_gate_accepts_falsey_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_VIDEO_SCENE_FRAMES", raising=False)
    assert embeddings.scene_frames_enabled() is False
    monkeypatch.setenv("EXOMEM_VIDEO_SCENE_FRAMES", "0")
    assert embeddings.scene_frames_enabled() is False
    monkeypatch.setenv("EXOMEM_VIDEO_SCENE_FRAMES", "off")
    assert embeddings.scene_frames_enabled() is False
    monkeypatch.setenv("EXOMEM_VIDEO_SCENE_FRAMES", "1")
    assert embeddings.scene_frames_enabled() is True


def test_worker_clip_embeds_video_via_keyframes(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="clips", filename="demo.mp4", data=b"\x00\x00video", text="x",
    )
    called = {}
    monkeypatch.setattr(embeddings, "embed_video_frames", lambda p: called.setdefault("v", _three_frames()))
    monkeypatch.setattr(embeddings, "embed_image", lambda p: (_ for _ in ()).throw(AssertionError("used embed_image for video")))
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(media_worker._Job(
        binary_path=vault / res.path, sidecar_path=vault / res.sidecar_path,
        media_type="video", do_ocr=False, do_clip=True,
    ))
    assert "v" in called  # per-keyframe path was used, not embed_image
    idx = embeddings.ClipIndex(vault)
    assert idx.has(res.path)
    paths, _, _ = idx.all_vectors()
    assert paths.count(res.path) == 3  # one row per keyframe, not one mean-pool


class _FrameImg:
    """PIL stand-in accepted by scene_frames._save_jpeg."""

    size = (640, 360)

    def resize(self, size):
        return self

    def convert(self, mode):
        return self

    def save(self, path, format=None, quality=None):
        Path(path).write_bytes(b"\xff\xd8x")


def _two_scenes():
    vecs = [
        (5.0, np.eye(1, embeddings.CLIP_DIM, 0, dtype=np.float32)[0]),
        (15.0, np.eye(1, embeddings.CLIP_DIM, 1, dtype=np.float32)[0]),
    ]
    pairs = [
        (Scene(start_ts=0.0, end_ts=10.0, rep_ts=5.0, boundary_score=0.0), _FrameImg()),
        (Scene(start_ts=10.0, end_ts=20.0, rep_ts=15.0, boundary_score=0.5), _FrameImg()),
    ]
    return vecs, pairs


def test_worker_gate_on_writes_frames_and_queues_ocr(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setenv("EXOMEM_VIDEO_SCENE_FRAMES", "1")
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="clips", filename="demo.mp4", data=b"\x00video", text="x",
    )
    monkeypatch.setattr(embeddings, "embed_video_scenes", lambda p: _two_scenes())
    monkeypatch.setattr(
        embeddings,
        "embed_video_frames",
        lambda p: (_ for _ in ()).throw(AssertionError("uniform path used with gate on")),
    )
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(media_worker._Job(
        binary_path=vault / res.path, sidecar_path=vault / res.sidecar_path,
        media_type="video", do_ocr=False, do_clip=True,
    ))
    idx = embeddings.ClipIndex(vault)
    paths, _, _ = idx.all_vectors()
    assert paths.count(res.path) == 2  # scene vectors upserted
    frames_dir = scene_frames.frames_dir_for(vault / res.path)
    assert len(list(frames_dir.glob("scene-*.jpg"))) == 2
    assert len(list(frames_dir.glob("scene-*.jpg.md"))) == 2
    # One OCR-only job queued per frame — never a CLIP job for a frame child.
    jobs = []
    while not w._q.empty():
        jobs.append(w._q.get_nowait())
    assert len(jobs) == 2
    assert all(j.do_ocr and not j.do_clip and j.media_type == "image" for j in jobs)


def test_worker_gate_off_never_touches_scene_path(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.delenv("EXOMEM_VIDEO_SCENE_FRAMES", raising=False)
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="clips", filename="demo.mp4", data=b"\x00video", text="x",
    )
    monkeypatch.setattr(embeddings, "embed_video_frames", lambda p: _three_frames())
    monkeypatch.setattr(
        embeddings,
        "embed_video_scenes",
        lambda p: (_ for _ in ()).throw(AssertionError("scene path used with gate off")),
    )
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(media_worker._Job(
        binary_path=vault / res.path, sidecar_path=vault / res.sidecar_path,
        media_type="video", do_ocr=False, do_clip=True,
    ))
    assert not scene_frames.frames_dir_for(vault / res.path).exists()
    assert w._q.empty()


def test_worker_frame_write_failure_still_upserts_vectors(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setenv("EXOMEM_VIDEO_SCENE_FRAMES", "1")
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="clips", filename="demo.mp4", data=b"\x00video", text="x",
    )
    monkeypatch.setattr(embeddings, "embed_video_scenes", lambda p: _two_scenes())
    monkeypatch.setattr(
        media_worker.scene_frames,
        "write_scene_frames",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk on fire")),
    )
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(media_worker._Job(
        binary_path=vault / res.path, sidecar_path=vault / res.sidecar_path,
        media_type="video", do_ocr=False, do_clip=True,
    ))
    idx = embeddings.ClipIndex(vault)
    assert idx.has(res.path)  # vectors survived the frame failure
    assert w._q.empty()


def test_startup_scan_skips_frame_children(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    kb = vault / "Knowledge Base/Evidence/Test/clips"
    kb.mkdir(parents=True, exist_ok=True)
    # A normal image with a plain sidecar → should be CLIP-queued.
    normal = kb / "photo.jpg"
    normal.write_bytes(b"\xff\xd8x")
    normal.with_name("photo.jpg.md").write_text(
        "---\ntype: source\nmedia_type: image\nevidence_file: x\n---\n", encoding="utf-8"
    )
    # A scene-frame child (sidecar carries parent_media) → must be skipped.
    frames_dir = kb / "demo.mp4.frames"
    frames_dir.mkdir()
    child = frames_dir / "scene-000-t5000ms.jpg"
    child.write_bytes(b"\xff\xd8x")
    child.with_name(child.name + ".md").write_text(
        "---\ntype: source\nmedia_type: image\nparent_media: Knowledge Base/Evidence/Test/clips/demo.mp4\n---\n",
        encoding="utf-8",
    )
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    n = w._scan_unindexed_images()
    assert n == 1
    job = w._q.get_nowait()
    assert job.binary_path == normal


def test_backfill_clip_indexes_video(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    p = vault / "Knowledge Base/Evidence/Old/clips/legacy.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00video")
    monkeypatch.setattr(embeddings, "embed_video_frames", lambda f: _three_frames())
    stats = backfill.backfill_media(vault, do_ocr=False, log_fn=lambda *a: None)
    assert stats.clip_indexed == 1
    rel = "Knowledge Base/Evidence/Old/clips/legacy.mp4"
    idx = embeddings.ClipIndex(vault)
    assert idx.has(rel)
    paths, _, _ = idx.all_vectors()
    assert paths.count(rel) == 3


def _legacy_video(vault) -> tuple[Path, str]:
    """A pre-feature video: binary + done sidecar + legacy uniform CLIP rows."""
    rel = "Knowledge Base/Evidence/Old/clips/legacy.mp4"
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00video")
    p.with_name("legacy.mp4.md").write_text(
        "---\ntype: source\nmedia_type: video\n"
        f"evidence_file: {rel}\nextracted_by: whisper\n---\n\n## Extracted text\n\nhello\n",
        encoding="utf-8",
    )
    idx = embeddings.ClipIndex(vault)
    idx.upsert_frames(rel, _three_frames(), p.stat().st_mtime)
    return p, rel


def test_backfill_upgrades_legacy_video_to_scene_frames(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setenv("EXOMEM_VIDEO_SCENE_FRAMES", "1")
    p, rel = _legacy_video(vault)
    calls = {"scenes": 0}

    def _fake_scenes(path):
        calls["scenes"] += 1
        return _two_scenes()

    monkeypatch.setattr(embeddings, "embed_video_scenes", _fake_scenes)
    monkeypatch.setattr(
        extract, "extract_text",
        lambda f, media_type=None: extract.ExtractResult(
            text="slide words", engine="stub", media_type="image"
        ),
    )
    stats = backfill.backfill_media(vault, log_fn=lambda *a: None)
    assert stats.scene_frames_written == 2
    assert calls["scenes"] == 1
    # Legacy 3 uniform rows replaced by 2 scene-aware rows (delete-then-insert).
    idx = embeddings.ClipIndex(vault)
    paths, _, _ = idx.all_vectors()
    assert paths.count(rel) == 2
    # Frames written + OCR'd inline through the real seam.
    frames_dir = scene_frames.frames_dir_for(p)
    sidecars = sorted(frames_dir.glob("scene-*.jpg.md"))
    assert len(sidecars) == 2
    content = sidecars[0].read_text(encoding="utf-8")
    assert "extracted_by: stub" in content
    assert "slide words" in content

    # Second run: fully idempotent — nothing re-detected, no frame CLIP rows added.
    monkeypatch.setattr(
        embeddings, "embed_image",
        lambda f: (_ for _ in ()).throw(AssertionError("frame child was CLIP-indexed")),
    )
    stats2 = backfill.backfill_media(vault, log_fn=lambda *a: None)
    assert stats2.scene_frames_written == 0
    assert calls["scenes"] == 1
    paths2, _, _ = embeddings.ClipIndex(vault).all_vectors()
    assert paths2.count(rel) == 2


def test_backfill_gate_off_writes_no_frames(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.delenv("EXOMEM_VIDEO_SCENE_FRAMES", raising=False)
    p, rel = _legacy_video(vault)
    monkeypatch.setattr(
        embeddings, "embed_video_scenes",
        lambda f: (_ for _ in ()).throw(AssertionError("scene path used with gate off")),
    )
    stats = backfill.backfill_media(vault, do_ocr=False, log_fn=lambda *a: None)
    assert stats.scene_frames_written == 0
    assert not scene_frames.frames_dir_for(p).exists()


# ---------------- semantic segments: trailing re-embed ordering ----------------


def test_worker_gate_on_enqueues_parent_reembed_after_ocr(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setenv("EXOMEM_VIDEO_SCENE_FRAMES", "1")
    monkeypatch.setenv("EXOMEM_SEMANTIC_SEGMENTS", "1")
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="clips", filename="demo.mp4", data=b"\x00video", text="x",
    )
    monkeypatch.setattr(embeddings, "embed_video_scenes", lambda p: _two_scenes())
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(media_worker._Job(
        binary_path=vault / res.path, sidecar_path=vault / res.sidecar_path,
        media_type="video", do_ocr=False, do_clip=True,
    ))
    jobs = []
    while not w._q.empty():
        jobs.append(w._q.get_nowait())
    # FIFO: 2 frame-OCR jobs first, then exactly ONE parent re-embed.
    assert [j.do_reembed for j in jobs] == [False, False, True]
    assert jobs[-1].sidecar_path == vault / res.sidecar_path
    assert not jobs[-1].do_ocr and not jobs[-1].do_clip


def test_worker_reembed_gate_off_not_enqueued(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setenv("EXOMEM_VIDEO_SCENE_FRAMES", "1")
    monkeypatch.delenv("EXOMEM_SEMANTIC_SEGMENTS", raising=False)
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="clips", filename="demo.mp4", data=b"\x00video", text="x",
    )
    monkeypatch.setattr(embeddings, "embed_video_scenes", lambda p: _two_scenes())
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(media_worker._Job(
        binary_path=vault / res.path, sidecar_path=vault / res.sidecar_path,
        media_type="video", do_ocr=False, do_clip=True,
    ))
    jobs = []
    while not w._q.empty():
        jobs.append(w._q.get_nowait())
    assert [j.do_reembed for j in jobs] == [False, False]


def test_process_reembed_calls_upsert(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    called = {}
    monkeypatch.setattr(
        media_worker.embeddings, "upsert_after_write",
        lambda root, paths: called.setdefault("paths", paths),
    )
    sidecar = vault / "Knowledge Base/Evidence/T/clips/demo.mp4.md"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("---\nmedia_type: video\n---\n", encoding="utf-8")
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(media_worker._Job(
        binary_path=sidecar.with_suffix(""), sidecar_path=sidecar,
        media_type="video", do_ocr=False, do_clip=False, do_reembed=True,
    ))
    assert called["paths"] == [sidecar]


def test_scan_pending_enqueues_deduped_parent_reembed(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_SEMANTIC_SEGMENTS", "1")
    parent_rel = "Knowledge Base/Evidence/T/clips/demo.mp4"
    parent = vault / parent_rel
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_bytes(b"\x00video")
    (vault / (parent_rel + ".md")).write_text(
        f"---\nmedia_type: video\nevidence_file: {parent_rel}\nextracted_by: whisper+timed\n---\n",
        encoding="utf-8",
    )
    frames = vault / (parent_rel + ".frames")
    frames.mkdir()
    for i, ms in enumerate((5000, 15000)):
        jpg = frames / f"scene-00{i}-t{ms}ms.jpg"
        jpg.write_bytes(b"\xff\xd8x")
        jpg.with_name(jpg.name + ".md").write_text(
            "---\nmedia_type: image\n"
            f"evidence_file: {parent_rel}.frames/{jpg.name}\n"
            "extracted_by: pending\n"
            f"parent_media: {parent_rel}\n---\n",
            encoding="utf-8",
        )
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    n = w._scan_pending_ocr()
    jobs = []
    while not w._q.empty():
        jobs.append(w._q.get_nowait())
    reembeds = [j for j in jobs if j.do_reembed]
    assert len(reembeds) == 1  # two children, ONE deduped parent re-embed
    assert reembeds[0].sidecar_path == vault / (parent_rel + ".md")
    assert jobs.index(reembeds[0]) == len(jobs) - 1  # after the pending children
    assert n == 3

"""Deletion propagation to media-derived indexes (CLIP, scene-frames, reconcile).

Closes the gap where `index_sync.delete_after_remove` purged text/graph
sidecars but left CLIP image/frame vectors and scene-frame derivatives behind,
and where deleting a media *binary* directly (not its `.md` sidecar) triggered
no fan-out at all. See openspec/changes/fix-media-deletion-propagation/.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pytest

from exomem import claims, embeddings, file_watcher, index_sync, scene_frames
from exomem import delete_directory as delete_dir_module
from exomem import delete_file as delete_module
from exomem import reconcile as reconcile_module
from exomem.embeddings import Scene


def _unit(i: int, dim: int = embeddings.CLIP_DIM) -> np.ndarray:
    v = np.zeros(dim, dtype=np.float32)
    v[i] = 1.0
    return v


def _outcome(report: index_sync.IndexSyncReport, component: str):
    return next(item for item in report.components if item.component == component)


class _FakeImg:
    """PIL stand-in (mirrors tests/test_scene_frames.py): size/resize/convert/save."""

    size = (1920, 1080)

    def resize(self, size: tuple[int, int]) -> _FakeImg:
        return self

    def convert(self, mode: str) -> _FakeImg:
        return self

    def save(self, path: str, format: str | None = None, quality: int | None = None) -> None:
        Path(path).write_bytes(b"\xff\xd8fakejpg")


def _scene(rep: float) -> Scene:
    return Scene(start_ts=rep - 2, end_ts=rep + 2, rep_ts=rep, boundary_score=0.5)


def _video(vault: Path, rel: str = "Knowledge Base/Notes/Clips/demo.mp4") -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00video")
    return p


TODAY = dt.date(2026, 7, 24)


# ---- 1. CLIP purge in the fan-out ------------------------------------------


def test_delete_after_remove_drops_clip_rows(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    rel = "Knowledge Base/Evidence/Yolo/photos/a.jpg"
    idx = embeddings.ClipIndex(vault)
    idx.upsert(rel, _unit(0), 1.0)
    assert idx.has(rel)
    _epoch, gen_before, _instance = embeddings.ClipIndex.cache_token(vault)

    report = index_sync.delete_after_remove(vault, [rel])

    assert not embeddings.ClipIndex(vault).has(rel)
    _epoch, gen_after, _instance = embeddings.ClipIndex.cache_token(vault)
    assert gen_after > gen_before
    clip_outcome = _outcome(report, "clip")
    assert clip_outcome.outcome == "completed"
    assert clip_outcome.code == "clip_delete_completed"


def test_delete_after_remove_clip_extra_absent_delete_still_succeeds(
    vault: Path,
) -> None:
    """EXOMEM_DISABLE_CLIP=1 (default suite env; stands in for a lean install
    without the `embeddings` extra) must not break the rest of the delete."""
    rel = "Knowledge Base/Evidence/Yolo/photos/a.jpg"
    (vault / "Knowledge Base/Evidence/Yolo/photos").mkdir(parents=True, exist_ok=True)
    (vault / rel).write_bytes(b"\xff\xd8\xff")

    report = index_sync.delete_after_remove(vault, [rel])

    clip_outcome = _outcome(report, "clip")
    assert clip_outcome.outcome == "accepted"  # never breaks the delete
    assert clip_outcome.code == "clip_disabled"
    assert report.reconcile_required is False


# ---- 2. Scene-frame cleanup -------------------------------------------------


def test_delete_after_remove_clears_scene_frames(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    video = _video(vault)
    video_rel = video.resolve().relative_to(vault.resolve()).as_posix()
    pairs = scene_frames.write_scene_frames(
        vault, video, [(_scene(5.0), _FakeImg()), (_scene(75.0), _FakeImg())]
    )
    assert len(pairs) == 2
    frames_dir = scene_frames.frames_dir_for(video)
    assert list(frames_dir.glob("scene-*.jpg"))

    # Seed the video's own per-keyframe CLIP rows (the parent video's rel_path
    # owns visual search for its scenes; frame children never get their own row).
    idx = embeddings.ClipIndex(vault)
    idx.upsert_frames(video_rel, [(5.0, _unit(0)), (75.0, _unit(1))], 1.0)
    assert idx.has(video_rel)

    report = index_sync.delete_after_remove(vault, [video_rel])

    assert not list(frames_dir.glob("scene-*.jpg"))
    assert not list(frames_dir.glob("scene-*.jpg.md"))
    assert not embeddings.ClipIndex(vault).has(video_rel)
    assert _outcome(report, "clip").outcome == "completed"

    # Idempotent: clearing an already-clean video is a no-op, not an error.
    report_again = index_sync.delete_after_remove(vault, [video_rel])
    assert not list(frames_dir.glob("scene-*.jpg"))
    assert _outcome(report_again, "clip").outcome == "completed"


def test_delete_after_remove_scene_frames_noop_when_no_frames_dir(
    vault: Path,
) -> None:
    """A video with no persisted `.frames/` directory must not error."""
    video = _video(vault, "Knowledge Base/Notes/Clips/no-frames.mp4")
    video_rel = video.resolve().relative_to(vault.resolve()).as_posix()

    report = index_sync.delete_after_remove(vault, [video_rel])

    assert _outcome(report, "clip").as_dict() == {
        "component": "clip",
        "outcome": "accepted",
        "code": "clip_disabled",
    }


# ---- 3. Media-binary fan-out gate -------------------------------------------


def test_delete_file_media_binary_enters_fanout(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the media BINARY directly (not its `.md` sidecar) must still
    fan out to the derived indexes — previously `delete_file` gated the whole
    fan-out on `rel_path.endswith(".md")` and silently skipped binaries."""
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    rel = "Knowledge Base/Notes/Clips/photo.jpg"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8\xff")
    embeddings.ClipIndex(vault).upsert(rel, _unit(0), 1.0)
    assert embeddings.ClipIndex(vault).has(rel)

    result = delete_module.delete_file(
        vault, path=rel, confirm=True, today=TODAY,
    )

    assert not target.exists()
    assert result.trash_path.startswith("Knowledge Base/_trash/")
    assert result.index is not None  # fan-out ran (previously skipped for non-.md)
    clip_component = next(
        c for c in result.index["components"] if c["component"] == "clip"
    )
    assert clip_component["outcome"] == "completed"
    assert not embeddings.ClipIndex(vault).has(rel)


def test_delete_file_media_binary_video_suppresses_frame_children(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a video binary purges its scene-frame derivatives too, and the
    watcher suppression (register_self_delete) must not choke on the frame
    children being removed out from under it. Mock-asserts the exact rel set
    passed to register_self_delete so a future refactor that drops
    `+ frame_children` from delete_file's suppress_rels is caught (mirrors
    tests/test_semantic_lifecycle_writers.py:797-830's call-args style)."""
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    video = _video(vault, "Knowledge Base/Notes/Clips/meeting.mp4")
    video_rel = video.resolve().relative_to(vault.resolve()).as_posix()
    scene_frames.write_scene_frames(vault, video, [(_scene(5.0), _FakeImg())])
    frames_dir = scene_frames.frames_dir_for(video)
    assert list(frames_dir.glob("scene-*.jpg"))
    # list_scene_frame_children (and clear_scene_frames) touch BOTH the jpg and
    # its `.md` sidecar per owned frame — expect both in the suppression call.
    frame_children_rels = {
        p.resolve().relative_to(vault.resolve()).as_posix()
        for p in list(frames_dir.glob("scene-*.jpg")) + list(frames_dir.glob("scene-*.jpg.md"))
    }
    assert frame_children_rels

    watcher_calls: list[list[str]] = []
    real_register = file_watcher.register_self_delete
    monkeypatch.setattr(
        file_watcher,
        "register_self_delete",
        lambda root, rels: (watcher_calls.append(list(rels)), real_register(root, rels))[1],
    )

    result = delete_module.delete_file(
        vault, path=video_rel, confirm=True, today=TODAY,
    )

    assert not video.exists()
    assert not list(frames_dir.glob("scene-*.jpg"))
    assert not list(frames_dir.glob("scene-*.jpg.md"))
    assert result.index is not None
    # rel_path itself (the .mp4) is excluded — register_self_delete only ever
    # suppresses `.md` paths; the frame sidecars are the ones that need it.
    assert len(watcher_calls) == 1
    assert set(watcher_calls[0]) == frame_children_rels


def test_delete_file_non_media_non_md_keeps_current_behavior(
    vault: Path,
) -> None:
    """A non-.md, non-media file (no recognized extraction kind) must NOT
    enter the fan-out — unchanged behavior."""
    rel = "Knowledge Base/Notes/Clips/data.bin"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00\x01")

    result = delete_module.delete_file(
        vault, path=rel, confirm=True, today=TODAY,
    )

    assert not target.exists()
    assert result.index == {
        "components": [],
        "derived_work": "not_required",
        "paths_truncated": False,
        "reconcile_required": False,
    }


def test_delete_directory_media_binaries_enter_fanout(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`delete_directory` walked only `.md` files into its fan-out, so a
    CLIP-indexed image/video sitting inside a trashed subdir left its CLIP
    rows (and scene-frame derivatives) behind. Now image/video binaries join
    the `.md` rels in both the register_self_delete suppression call and the
    index_sync.delete_after_remove fan-out. Frame children need no dedicated
    collection here (unlike delete_file's single-target path): the recursive
    walk itself visits `.frames/`, classifying the jpg as image media and its
    `.jpg.md` sidecar as markdown."""
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    directory = "Knowledge Base/Notes/Clips/doomed"

    photo_rel = f"{directory}/photo.jpg"
    photo = vault / photo_rel
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b"\xff\xd8\xff")
    embeddings.ClipIndex(vault).upsert(photo_rel, _unit(0), 1.0)

    video = _video(vault, f"{directory}/meeting.mp4")
    video_rel = video.resolve().relative_to(vault.resolve()).as_posix()
    scene_frames.write_scene_frames(vault, video, [(_scene(5.0), _FakeImg())])
    frames_dir = scene_frames.frames_dir_for(video)
    frame_children_rels = {
        p.resolve().relative_to(vault.resolve()).as_posix()
        for p in list(frames_dir.glob("scene-*.jpg")) + list(frames_dir.glob("scene-*.jpg.md"))
    }
    assert frame_children_rels
    embeddings.ClipIndex(vault).upsert_frames(video_rel, [(5.0, _unit(1))], 1.0)

    note_rel = f"{directory}/note.md"
    (vault / note_rel).write_text(
        "---\ntype: insight\ncreated: 2026-07-24\nupdated: 2026-07-24\ntags: []\n---\n# Note\n",
        encoding="utf-8",
    )

    assert embeddings.ClipIndex(vault).has(photo_rel)
    assert embeddings.ClipIndex(vault).has(video_rel)

    watcher_calls: list[list[str]] = []
    real_register = file_watcher.register_self_delete
    monkeypatch.setattr(
        file_watcher,
        "register_self_delete",
        lambda root, rels: (watcher_calls.append(list(rels)), real_register(root, rels))[1],
    )

    result = delete_dir_module.delete_directory(
        vault, path=directory, confirm=True, recursive=True, today=TODAY,
    )

    assert not (vault / directory).exists()
    assert not embeddings.ClipIndex(vault).has(photo_rel)
    assert not embeddings.ClipIndex(vault).has(video_rel)
    assert not list(frames_dir.glob("scene-*.jpg"))
    assert not list(frames_dir.glob("scene-*.jpg.md"))
    assert result.index is not None
    clip_component = next(
        c for c in result.index["components"] if c["component"] == "clip"
    )
    assert clip_component["outcome"] == "completed"

    assert len(watcher_calls) == 1
    assert set(watcher_calls[0]) == {note_rel, photo_rel, video_rel} | frame_children_rels


def test_delete_directory_md_only_dir_keeps_current_behavior(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory containing only `.md`/non-media files keeps the exact
    prior fan-out set — no media rels leak in. Companion to the existing
    tests/test_semantic_lifecycle_writers.py::
    test_recursive_trash_registers_sorted_markdown_once_and_returns_cleanup
    contract (a `.txt` sibling must not enter the fan-out)."""
    directory = "Knowledge Base/Notes/Clips/textonly"
    (vault / directory).mkdir(parents=True, exist_ok=True)
    first = f"{directory}/a.md"
    (vault / first).write_text(
        "---\ntype: insight\ncreated: 2026-07-24\nupdated: 2026-07-24\ntags: []\n---\n# A\n",
        encoding="utf-8",
    )
    (vault / f"{directory}/raw.txt").write_text("non-Markdown", encoding="utf-8")

    watcher_calls: list[list[str]] = []
    real_register = file_watcher.register_self_delete
    monkeypatch.setattr(
        file_watcher,
        "register_self_delete",
        lambda root, rels: (watcher_calls.append(list(rels)), real_register(root, rels))[1],
    )

    result = delete_dir_module.delete_directory(
        vault, path=directory, confirm=True, recursive=True, today=TODAY,
    )

    assert result.index is not None
    assert len(watcher_calls) == 1
    assert set(watcher_calls[0]) == {first}


# ---- 4. Reconcile healing ---------------------------------------------------


def test_reconcile_heals_clip_and_frame_orphans(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault that lost content through the pre-fix gap (CLIP rows and
    `.frames/` dirs left behind by a deletion that predates this fix) gets
    healed by reconcile — idempotently, not just for future deletes."""
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)

    # Seed a CLIP orphan: a row for a path with no file on disk (as if the
    # binary had been deleted before this fix existed).
    ghost_rel = "Knowledge Base/Evidence/Yolo/photos/ghost.jpg"
    idx = embeddings.ClipIndex(vault)
    idx.upsert(ghost_rel, _unit(2), 1.0)
    assert idx.has(ghost_rel)

    # A media binary may outlive its Markdown sidecar. It still needs semantic
    # cleanup, but it is not a binary orphan.
    sidecar_only_rel = "Knowledge Base/Evidence/Yolo/photos/sidecar-only.jpg"
    sidecar_only_binary = vault / sidecar_only_rel
    sidecar_only_binary.parent.mkdir(parents=True, exist_ok=True)
    sidecar_only_binary.write_bytes(b"\xff\xd8\xff")
    idx.upsert(sidecar_only_rel, _unit(3), 1.0)
    assert idx.has(sidecar_only_rel)

    # Seed a dangling `.frames/` dir: frame files with no parent video on disk.
    ghost_video = vault / "Knowledge Base/Notes/Clips/ghost.mp4"
    ghost_video.parent.mkdir(parents=True, exist_ok=True)
    ghost_video.write_bytes(b"\x00video")
    scene_frames.write_scene_frames(vault, ghost_video, [(_scene(5.0), _FakeImg())])
    frames_dir = scene_frames.frames_dir_for(ghost_video)
    assert list(frames_dir.glob("scene-*.jpg"))
    ghost_video.unlink()  # the video is gone; the frames dir is now orphaned

    # dry_run must REPORT the true counts (like the graph/reference drift
    # sections it mirrors) without touching anything on disk or in the index.
    dry_report = reconcile_module.reconcile(vault, dry_run=True)
    assert dry_report.clip_orphans_removed == 1
    assert dry_report.frame_orphans_removed == 1
    assert embeddings.ClipIndex(vault).has(ghost_rel)  # untouched
    assert list(frames_dir.glob("scene-*.jpg"))  # untouched

    report = reconcile_module.reconcile(vault)

    assert report.clip_orphans_removed == 1
    assert not embeddings.ClipIndex(vault).has(ghost_rel)
    assert not embeddings.ClipIndex(vault).has(sidecar_only_rel)
    assert sidecar_only_binary.exists()
    assert report.frame_orphans_removed == 1
    assert not list(frames_dir.glob("scene-*.jpg"))
    assert not list(frames_dir.glob("scene-*.jpg.md"))

    # Idempotent: a second run finds nothing left to heal.
    report_again = reconcile_module.reconcile(vault)
    assert report_again.clip_orphans_removed == 0
    assert report_again.frame_orphans_removed == 0


def test_reconcile_missing_rows_do_not_create_clip_sidecar_when_disabled(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing Markdown cleanup stays model-free when no CLIP sidecar exists."""
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    missing_rel = "Knowledge Base/Records/Health/items/missing.md"
    claim_index = claims.ClaimIndex(vault)
    with claim_index._connect() as conn:
        conn.execute(
            "INSERT INTO claims(file_path, claim_text, checksum, vector, file_mtime) "
            "VALUES (?, 'private', 'checksum', X'00', 0)",
            (missing_rel,),
        )

    assert not embeddings.clip_sidecar_path(vault).exists()
    report = reconcile_module.reconcile(vault)

    assert report.semantic_missing_purged == [missing_rel]
    assert not embeddings.clip_sidecar_path(vault).exists()

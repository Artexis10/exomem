"""Bulk media back-fill — make pre-existing KB binaries searchable.

`exomem backfill-media` walks the whole `Knowledge Base/` tree (not just
`Evidence/`), and for every media file (image/audio/video/pdf):
  1. writes a `.md` sidecar if missing — so `find()` can surface it (a CLIP/text match maps
     to `<file>.md`, which must exist);
  2. extracts text (OCR / ASR / PDF) if not already done — text-searchable;
  3. CLIP-embeds images — searchable by visual content.

Coverage is the whole KB so a binary filed anywhere a note can live (an invoice
under `Finance/`, a screenshot under `Sources/`) becomes searchable — not only
the `Evidence/` claim-backing tree. Config/cruft dirs are pruned
(`vault.VAULT_SCAN_SKIP_DIRS`).

Idempotent: re-running only does outstanding work. Runs on CPU or GPU (engines auto-detect).
The *incremental* path (new uploads) is handled live by the server; this is the deliberate
one-shot pass over content that predates the feature — or for a friend's existing vault.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from . import embeddings, extract, preserve, scene_frames, semantic_segments
from .vault import VAULT_SCAN_SKIP_DIRS

log = logging.getLogger(__name__)

_EXTRACTED_BY_RE = re.compile(r"(?m)^extracted_by:\s*(.+?)\s*$")
_PARENT_MEDIA_RE = re.compile(r"(?m)^parent_media:\s*\S")
_NOT_DONE = {"none", "pending"}


def iter_kb_files(root: Path):
    """Yield every file under `root`, pruning config/cruft/index dirs.

    Replaces a bare `rglob("*")` so a whole-KB walk never descends into
    `.git`, the embedding sqlite dir, `_Schema`, etc. (`VAULT_SCAN_SKIP_DIRS`).
    Shared with the live media worker's KB scans.
    """
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            children = list(d.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if child.name not in VAULT_SCAN_SKIP_DIRS:
                    stack.append(child)
            elif child.is_file():
                yield child


def _iter_media_files(root: Path):
    """Yield media files under `root` (pruned walk). `.md` sidecars and other
    non-media files are filtered out by `extract.media_type_for`."""
    for f in iter_kb_files(root):
        if extract.media_type_for(f):
            yield f


def _sidecar_for(binary: Path) -> Path:
    name = binary.name
    if name.lower().endswith(".md"):
        return binary.with_name(name[:-3] + "-notes.md")
    return binary.with_name(name + ".md")


def _extracted_engine(sidecar: Path) -> str | None:
    """The sidecar's raw `extracted_by:` value, or None when absent/unreadable."""
    try:
        head = sidecar.read_text("utf-8")[:800]
    except OSError:
        return None
    m = _EXTRACTED_BY_RE.search(head)
    return m.group(1).strip() if m else None


def _ocr_done(sidecar: Path) -> bool:
    """True if the sidecar already has extracted text (a real engine in extracted_by)."""
    v = _extracted_engine(sidecar)
    return v is not None and v not in _NOT_DONE and not v.startswith("failed:")


def _needs_rediarize(sidecar: Path, media_type: str | None) -> bool:
    """True for an audio/video sidecar transcribed BEFORE diarization was enabled: a
    completed ASR engine (not pending/failed/no-audio) without the `+diarized` marker —
    that marker is the re-diarize done-state, which keeps the pass idempotent."""
    if media_type not in ("audio", "video"):
        return False
    v = _extracted_engine(sidecar)
    if v is None or v in _NOT_DONE or v.startswith("failed:") or v == "no-audio":
        return False
    return not v.endswith("+diarized")


def _needs_retime(sidecar: Path, media_type: str | None) -> bool:
    """True for an audio/video sidecar transcribed BEFORE timed transcripts: a
    completed ASR engine (not pending/failed/no-audio) without the `+timed`
    marker — that marker is the re-time done-state, keeping the pass idempotent."""
    if media_type not in ("audio", "video"):
        return False
    v = _extracted_engine(sidecar)
    if v is None or v in _NOT_DONE or v.startswith("failed:") or v == "no-audio":
        return False
    return "+timed" not in v


def _is_frame_child(sidecar: Path) -> bool:
    """True for a scene-frame child sidecar (`parent_media:` set). Such images never
    get their own ClipIndex rows — the parent video's per-scene vectors own visual
    search — but they DO get sidecars/OCR like any image."""
    try:
        head = sidecar.read_text("utf-8")[:800]
    except OSError:
        return False
    return bool(_PARENT_MEDIA_RE.search(head))


def _scenes_done(video: Path) -> bool:
    """True when the video already has at least one persisted scene frame + sidecar."""
    d = scene_frames.frames_dir_for(video)
    if not d.is_dir():
        return False
    for f in d.glob("scene-*.jpg"):
        if (
            scene_frames.parse_frame_ts(f.name) is not None
            and f.with_name(f.name + ".md").exists()
        ):
            return True
    return False


@dataclass
class BackfillStats:
    scanned: int = 0
    sidecars_created: int = 0
    extracted: int = 0
    extract_failed: int = 0
    rediarized: int = 0
    retimed: int = 0
    clip_indexed: int = 0
    scene_frames_written: int = 0
    skipped: int = 0


def backfill_media(
    vault_root: Path,
    *,
    do_ocr: bool = True,
    do_clip: bool = True,
    rediarize: bool = False,
    retime: bool = False,
    dry_run: bool = False,
    log_fn=log.info,
) -> BackfillStats:
    """Back-fill sidecars + text + CLIP for every media file under Knowledge Base/. Idempotent.

    `rediarize` re-extracts audio/video whose transcript predates diarization (a completed
    ASR engine without `+diarized`) so they gain labeled turns + `speakers:` frontmatter.
    `retime` re-extracts audio/video whose transcript predates timed transcripts (no
    `+timed` marker) so they gain per-segment `[m:ss]` lines — the substrate for
    semantic segmentation. One re-extraction serves both flags when both are requested.
    """
    stats = BackfillStats()
    kb = vault_root / "Knowledge Base"
    if not kb.is_dir():
        log_fn("no Knowledge Base/ directory; nothing to back-fill")
        return stats
    if rediarize and not extract._diarize_enabled():
        log_fn(
            "--rediarize requested but EXOMEM_DIARIZE is not enabled; "
            "skipping re-diarization (set EXOMEM_DIARIZE=1)"
        )
        rediarize = False
    if retime and not semantic_segments.semantic_segments_enabled():
        log_fn(
            "--retime requested but EXOMEM_SEMANTIC_SEGMENTS is not enabled; "
            "skipping re-timing (set EXOMEM_SEMANTIC_SEGMENTS=1)"
        )
        retime = False
    clip_index = embeddings.get_clip_index(vault_root) if do_clip else None
    # Fast media first (image/pdf OCR is quick) so screenshots/docs are searchable in
    # minutes; slow A/V transcription (Whisper) runs last instead of starving the queue.
    _order = {"image": 0, "pdf": 1, "audio": 2, "video": 3}
    files = sorted(
        _iter_media_files(kb),
        key=lambda p: (_order.get(extract.media_type_for(p), 9), p.as_posix()),
    )
    stats.scanned = len(files)
    log_fn(f"scanning {len(files)} media file(s) under Knowledge Base/ (dry_run={dry_run})")

    for i, f in enumerate(files, 1):
        media_type = extract.media_type_for(f)
        try:
            rel = f.resolve().relative_to(vault_root.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        sidecar = _sidecar_for(f)
        need_sidecar = not sidecar.exists()
        need_ocr = do_ocr and not _ocr_done(sidecar)
        # Video idempotency keys on per-keyframe rows (has_frames), so a legacy
        # single-vector video (frame_ts NULL) is re-indexed per-keyframe, not skipped.
        # Images stay on has() (one row = done). Scene-frame children are excluded:
        # the parent video's per-scene vectors own visual search.
        need_clip = (
            do_clip and clip_index is not None and media_type in ("image", "video")
            and not (clip_index.has_frames(rel) if media_type == "video" else clip_index.has(rel))
            and not (media_type == "image" and _is_frame_child(sidecar))
        )
        # Scene frames (EXOMEM_VIDEO_SCENE_FRAMES): a video without persisted frames
        # re-processes once — one decode pass replaces its CLIP rows with scene-aware
        # vectors AND writes the representative JPEGs. Idempotent via _scenes_done.
        need_scenes = (
            do_clip and clip_index is not None and media_type == "video"
            and scene_frames.scene_frames_enabled() and not _scenes_done(f)
        )
        # Re-diarize targets ONLY sidecars whose extraction is otherwise done (`not
        # need_ocr`): a pending/missing sidecar takes the normal OCR path, which
        # diarizes anyway when the flag is on.
        need_rediarize = (
            rediarize and do_ocr and not need_ocr and _needs_rediarize(sidecar, media_type)
        )
        need_retime = (
            retime and do_ocr and not need_ocr and _needs_retime(sidecar, media_type)
        )
        if not (need_sidecar or need_ocr or need_clip or need_scenes or need_rediarize
                or need_retime):
            stats.skipped += 1
            continue
        if dry_run:
            todo = " ".join(t for t, on in
                            (("sidecar", need_sidecar), ("ocr", need_ocr), ("clip", need_clip),
                             ("scenes", need_scenes), ("rediarize", need_rediarize),
                             ("retime", need_retime)) if on)
            log_fn(f"  [{i}/{len(files)}] {rel} -> {todo}")
            stats.sidecars_created += need_sidecar
            stats.extracted += need_ocr
            stats.clip_indexed += need_clip
            stats.rediarized += need_rediarize
            stats.retimed += need_retime
            continue

        if need_sidecar:
            sidecar, created = preserve.ensure_media_sidecar(vault_root, f)
            stats.sidecars_created += int(created)
        if need_ocr or need_rediarize or need_retime:
            try:
                res = extract.extract_text(f, media_type=media_type, vault_root=vault_root)
                # Each requested upgrade is judged by its engine marker (extract's
                # soft-fail contract). A failed upgrade disables its pass for the rest
                # (every further attempt would fail the same way); the sidecar is
                # written when first-time OCR ran or at least one upgrade materialized
                # — a fully-failed upgrade leaves bytes it already holds untouched.
                got_diarized = res.engine.endswith("+diarized")
                got_timed = "+timed" in res.engine
                if need_rediarize and not got_diarized:
                    log_fn(
                        f"  ! diarization unavailable (engine {res.engine}); "
                        "skipping re-diarization for the rest"
                    )
                    rediarize = False
                if need_retime and not got_timed:
                    log_fn(
                        f"  ! timed rendering unavailable (engine {res.engine}); "
                        "skipping re-timing for the rest"
                    )
                    retime = False
                upgrade_gained = (need_rediarize and got_diarized) or (
                    need_retime and got_timed
                )
                if need_ocr or upgrade_gained:
                    preserve.update_sidecar_extraction(
                        vault_root, sidecar,
                        text=res.text.strip() or "(no text detected)", engine=res.engine,
                        speakers=res.speakers,
                    )
                    if need_ocr:
                        stats.extracted += 1
                    if need_rediarize and got_diarized:
                        stats.rediarized += 1
                    if need_retime and got_timed:
                        stats.retimed += 1
            except extract.ExtractionUnavailable as e:
                log_fn(f"  ! extraction engine unavailable ({e}); skipping OCR for the rest")
                do_ocr = False
            except Exception:  # noqa: BLE001 — one bad file shouldn't abort the pass
                log.exception("backfill: extraction failed for %s", f.name)
                stats.extract_failed += 1
        if need_clip or need_scenes:
            try:
                mtime = f.stat().st_mtime
                if media_type == "video" and need_scenes:
                    vectors, pairs = embeddings.embed_video_scenes(f)
                    clip_index.upsert_frames(rel, vectors, mtime)
                    if need_clip:
                        stats.clip_indexed += 1
                    written = scene_frames.write_scene_frames(vault_root, f, pairs)
                    stats.scene_frames_written += len(written)
                    if do_ocr and written:
                        do_ocr = _ocr_new_scene_frames(vault_root, written, stats, log_fn)
                    if written and semantic_segments.semantic_segments_enabled():
                        # Fold the fresh visual/OCR boundary events into the parent's
                        # semantic segments (mirrors the worker's trailing re-embed).
                        embeddings.upsert_after_write(vault_root, [sidecar])
                elif media_type == "video":
                    clip_index.upsert_frames(rel, embeddings.embed_video_frames(f), mtime)
                    stats.clip_indexed += 1
                else:
                    clip_index.upsert(rel, embeddings.embed_image(f), mtime)
                    stats.clip_indexed += 1
            except embeddings.ClipUnavailable as e:
                log_fn(f"  ! CLIP unavailable ({e}); skipping CLIP for the rest")
                do_clip = False
            except Exception:  # noqa: BLE001
                log.exception("backfill: CLIP failed for %s", f.name)
        if i % 25 == 0:
            log_fn(f"  …{i}/{len(files)} processed")

    log_fn(f"backfill done: {asdict(stats)}")
    return stats


def _ocr_new_scene_frames(
    vault_root: Path,
    written: list[tuple[Path, Path]],
    stats: BackfillStats,
    log_fn,
) -> bool:
    """OCR freshly written scene frames in-line (they postdate this run's file
    snapshot). Returns the updated `do_ocr` flag — False when the engine is
    unavailable, leaving the remaining sidecars `pending` for the live worker's
    restart scan to heal."""
    for jpg, frame_sidecar in written:
        try:
            res = extract.extract_text(jpg, media_type="image")
            preserve.update_sidecar_extraction(
                vault_root, frame_sidecar,
                text=res.text.strip() or "(no text detected)", engine=res.engine,
            )
            stats.extracted += 1
        except extract.ExtractionUnavailable as e:
            log_fn(f"  ! extraction engine unavailable ({e}); scene frames left pending")
            return False
        except Exception:  # noqa: BLE001 — one bad frame shouldn't abort the pass
            log.exception("backfill: frame OCR failed for %s", jpg.name)
            stats.extract_failed += 1
    return True

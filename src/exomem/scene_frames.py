"""Persisted scene frames for videos (`EXOMEM_VIDEO_SCENE_FRAMES`).

One representative JPEG per detected scene lands in a `<video-filename>.frames/`
directory sibling to the video, each with a standard `.md` sidecar carrying
`parent_media` (the vault-relative video path) and `frame_ts` (seconds). The
timestamp is also encoded in the filename (`scene-<NNN>-t<ms>ms.jpg`) so lookups
need no extra index. Frames ride the existing image OCR path via
`extracted_by: pending`; they get NO ClipIndex rows — the parent video's
per-scene vectors own visual search (the worker scan and backfill skip
`parent_media` children at every CLIP-enqueue point).

Everything here soft-fails: a frame that can't be encoded or written is logged
and skipped; the caller's CLIP vectors are never blocked on frame persistence.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

from . import embeddings, index_sync
from .preserve import _render_sidecar
from .vault import PlannedWrite, batch_atomic_write

log = logging.getLogger(__name__)

FRAMES_DIR_SUFFIX = ".frames"
JPEG_MAX_SIDE = 1280  # downscale bound — keeps slide/terminal text legible for OCR
JPEG_QUALITY = 80

_FRAME_NAME_RE = re.compile(r"^scene-(\d{3,})-t(\d+)ms\.jpe?g$", re.IGNORECASE)


def scene_frames_enabled() -> bool:
    """Single gate, shared with the sampler (`EXOMEM_VIDEO_SCENE_FRAMES`)."""
    return embeddings.scene_frames_enabled()


def frames_dir_for(video_path: Path) -> Path:
    """`<dir>/<video-filename>.frames/` — the sibling directory owning this video's frames."""
    return video_path.with_name(video_path.name + FRAMES_DIR_SUFFIX)


def frame_filename(index: int, ts: float) -> str:
    """`scene-<NNN>-t<ms>ms.jpg` — sorts chronologically, timestamp parseable back out."""
    return f"scene-{index:03d}-t{int(round(ts * 1000))}ms.jpg"


def parse_frame_ts(name: str) -> float | None:
    """Timestamp (seconds) encoded in a frame filename, or None for non-frame files."""
    m = _FRAME_NAME_RE.match(name)
    if not m:
        return None
    return int(m.group(2)) / 1000.0


def clear_scene_frames(vault_root: Path, video_path: Path) -> int:
    """Remove the frames this feature owns (`scene-*.jpg` + sidecars) for a video.

    The delete half of delete-then-insert re-processing (mirrors
    `ClipIndex.upsert_frames`). Removed sidecars are purged from the text
    embedding index so stale rows don't linger. Returns the number of files removed.
    """
    d = frames_dir_for(video_path)
    if not d.is_dir():
        return 0
    removed_sidecars: list[str] = []
    n = 0
    for f in sorted(d.iterdir()):
        if parse_frame_ts(f.name) is None:
            continue
        for victim in (f, f.with_name(f.name + ".md")):
            if not victim.exists():
                continue
            rel: str | None
            try:
                rel = victim.resolve().relative_to(vault_root.resolve()).as_posix()
            except (ValueError, OSError):
                rel = None
            try:
                victim.unlink()
                n += 1
            except OSError as e:
                log.warning("could not remove stale scene frame %s: %s", victim.name, e)
                continue
            if rel and victim.suffix.lower() == ".md":
                removed_sidecars.append(rel)
    if removed_sidecars:
        index_sync.delete_after_remove(vault_root, removed_sidecars)
    return n


def _save_jpeg(img, path: Path) -> None:
    """Downscale (longest side ≤ JPEG_MAX_SIDE) and save as JPEG."""
    w, h = img.size
    scale = JPEG_MAX_SIDE / max(w, h)
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    img.convert("RGB").save(str(path), format="JPEG", quality=JPEG_QUALITY)


def _format_mmss(ts: float) -> str:
    total = int(ts)
    return f"{total // 60:02d}:{total % 60:02d}"


def write_scene_frames(
    vault_root: Path,
    video_path: Path,
    scenes_with_images: list[tuple[embeddings.Scene, object]],
    *,
    today: dt.date | None = None,
) -> list[tuple[Path, Path]]:
    """Persist one JPEG + pending sidecar per scene → `[(jpg_path, sidecar_path)]`.

    Clears previously-owned frames first (delete-then-insert). Soft-fails per
    frame: an unencodable/unwritable frame is logged and skipped; a sidecar batch
    failure logs and returns [] (orphan JPEGs are healed by reconcile/backfill).
    """
    try:
        video_rel = video_path.resolve().relative_to(vault_root.resolve()).as_posix()
    except (ValueError, OSError) as e:
        log.warning("scene frames skipped for %s: %s", video_path.name, e)
        return []
    clear_scene_frames(vault_root, video_path)
    d = frames_dir_for(video_path)
    # Tag context from Knowledge Base/Evidence/<scope>/<category>/… (same derivation
    # as preserve.ensure_media_sidecar).
    parts = video_rel.split("/")
    scope = parts[2] if len(parts) > 2 else "evidence"
    category = parts[3] if len(parts) > 3 else "uncategorized"
    date_iso = (today or dt.date.today()).isoformat()
    writes: list[PlannedWrite] = []
    out: list[tuple[Path, Path]] = []
    for i, (scene, img) in enumerate(scenes_with_images):
        name = frame_filename(i, scene.rep_ts)
        jpg = d / name
        try:
            d.mkdir(parents=True, exist_ok=True)
            _save_jpeg(img, jpg)
        except Exception as e:  # noqa: BLE001 — one bad frame must not block the rest
            log.warning("scene frame write failed for %s: %s", name, e)
            continue
        sidecar = jpg.with_name(name + ".md")
        md = _render_sidecar(
            artifact_name=name,
            scope=scope,
            category=category,
            date_iso=date_iso,
            description=(
                f"Scene frame of `{video_path.name}` at {_format_mmss(scene.rep_ts)} "
                f"(parent: {video_rel})."
            ),
            media_type="image",
            evidence_file=f"{video_rel}{FRAMES_DIR_SUFFIX}/{name}",
            extracted_by="pending",
            parent_media=video_rel,
            frame_ts=scene.rep_ts,
        )
        writes.append(PlannedWrite(path=sidecar, content=md))
        out.append((jpg, sidecar))
    if not writes:
        return []
    try:
        batch_atomic_write(writes, vault_root=vault_root)
    except Exception as e:  # noqa: BLE001 — sidecars are the findability layer, not the vectors
        log.warning("scene frame sidecar write failed for %s: %s", video_path.name, e)
        return []
    return out


def nearest_frame(vault_root: Path, video_rel: str, ts: float) -> tuple[str, float] | None:
    """The persisted frame nearest `ts` for a video → `(jpg_rel, frame_ts)`, or None.

    Resolved purely from filenames (no index) — used by `find` to attach a
    viewable frame to a CLIP-lane video hit.
    """
    d = vault_root / (video_rel + FRAMES_DIR_SUFFIX)
    if not d.is_dir():
        return None
    best: tuple[str, float] | None = None
    try:
        entries = list(d.iterdir())
    except OSError:
        return None
    for f in entries:
        fts = parse_frame_ts(f.name)
        if fts is None:
            continue
        if best is None or abs(fts - ts) < abs(best[1] - ts):
            best = (f"{video_rel}{FRAMES_DIR_SUFFIX}/{f.name}", fts)
    return best

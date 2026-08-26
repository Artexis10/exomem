"""Persisted scene frames for videos (`EXOMEM_VIDEO_SCENE_FRAMES`).

One representative JPEG per detected scene lands in a `<video-filename>.frames/`
directory sibling to the video, each with a standard `.md` sidecar carrying
`parent_media` (the vault-relative video path) and `frame_ts` (seconds). The
timestamp is also encoded in the filename (`scene-<NNN>-t<ms>ms.jpg`) so lookups
need no extra index. Frames ride the existing image OCR path via
`extracted_by: pending`; they get NO ClipIndex rows — the parent video's
per-scene vectors own visual search (the worker scan and backfill skip
`parent_media` children at every CLIP-enqueue point).

Encoding and ordinary batch failures soft-fail without blocking the caller's
CLIP vectors. Exact-v4 catalog refusal/uncertainty reaches the surrounding media
boundary so governed publication is never reported as an ordinary skipped frame.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import math
import os
import re
import tempfile
from pathlib import Path
from typing import BinaryIO

from . import embeddings, index_sync
from .preserve import _render_sidecar
from .vault import (
    MISSING_CONTENT_HASH,
    PathGuard,
    PlannedWrite,
    PreparedBinaryContent,
    batch_atomic_write,
)

log = logging.getLogger(__name__)

FRAMES_DIR_SUFFIX = ".frames"
JPEG_MAX_SIDE = 1280  # downscale bound — keeps slide/terminal text legible for OCR
JPEG_QUALITY = 80

_FRAME_NAME_RE = re.compile(r"^scene-(\d{3,})-t(\d+)ms\.jpe?g$", re.IGNORECASE)
_MAX_FRAME_TIMESTAMP_MS = 4_294_967_295
_JPEG_SPOOL_MEMORY_BYTES = 4 * 1024 * 1024


def scene_frames_enabled() -> bool:
    """Single gate, shared with the sampler (`EXOMEM_VIDEO_SCENE_FRAMES`)."""
    return embeddings.scene_frames_enabled()


def frames_dir_for(video_path: Path) -> Path:
    """`<dir>/<video-filename>.frames/` — the sibling directory owning this video's frames."""
    return video_path.with_name(video_path.name + FRAMES_DIR_SUFFIX)


def frame_timestamp_ms(ts: float) -> int:
    """Canonical bounded milliseconds for one binary64 scene timestamp."""
    if type(ts) not in {int, float} or isinstance(ts, bool):
        raise ValueError("scene-frame timestamp must be a finite number")
    seconds = float(ts)
    milliseconds = seconds * 1000.0
    if not math.isfinite(seconds) or not math.isfinite(milliseconds) or seconds < 0:
        raise ValueError("scene-frame timestamp must be finite and nonnegative")
    value = int(round(milliseconds))
    if not 0 <= value <= _MAX_FRAME_TIMESTAMP_MS:
        raise ValueError("scene-frame timestamp is outside the supported range")
    return value


def _frame_filename_ms(index: int, milliseconds: int) -> str:
    return f"scene-{index:03d}-t{milliseconds}ms.jpg"


def frame_filename(index: int, ts: float) -> str:
    """`scene-<NNN>-t<ms>ms.jpg` — sorts chronologically, timestamp parseable back out."""
    return _frame_filename_ms(index, frame_timestamp_ms(ts))


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


def list_scene_frame_children(vault_root: Path, video_path: Path) -> list[str]:
    """Vault-relative rel paths of this video's owned frame files (jpg + `.md`
    sidecar), read-only. Mirrors `clear_scene_frames`' iteration without
    deleting -- for a caller that needs to know what a deletion is about to
    remove (e.g. watcher self-delete suppression) before it happens. No-op
    (empty list) when no `.frames/` directory exists.
    """
    d = frames_dir_for(video_path)
    if not d.is_dir():
        return []
    out: list[str] = []
    for f in sorted(d.iterdir()):
        if parse_frame_ts(f.name) is None:
            continue
        for child in (f, f.with_name(f.name + ".md")):
            if not child.exists():
                continue
            try:
                rel = child.resolve().relative_to(vault_root.resolve()).as_posix()
            except (ValueError, OSError):
                continue
            out.append(rel)
    return out


def _save_jpeg(img, target: BinaryIO) -> None:
    """Downscale (longest side ≤ JPEG_MAX_SIDE) and save as JPEG."""
    w, h = img.size
    scale = JPEG_MAX_SIDE / max(w, h)
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    converted = img.convert("RGB")
    try:
        converted.save(target, format="JPEG", quality=JPEG_QUALITY)
        return
    except TypeError:
        # Pillow accepts a binary file object. A few compatible encoders and
        # lightweight test doubles accept only a pathname, so give them a
        # private non-canonical path and copy those reviewed bytes into the
        # spool. Nothing under the vault is visible before the held batch.
        target.seek(0)
        target.truncate()
    with tempfile.TemporaryDirectory(prefix="exomem-scene-frame-") as directory:
        stage = Path(directory) / "frame.jpg"
        converted.save(str(stage), format="JPEG", quality=JPEG_QUALITY)
        with stage.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)


def _format_mmss(ts: float) -> str:
    total = int(ts)
    return f"{total // 60:02d}:{total % 60:02d}"


def _content_guard(
    vault_root: Path,
    relative: str,
) -> tuple[str, PathGuard, PathGuard]:
    """Hash one parent and return cheap identity plus completion guards."""
    stable = PathGuard.capture(vault_root, relative, leaf_policy="stable")
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(vault_root / relative, flags)
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    finally:
        os.close(descriptor)
    stable.recheck(vault_root)
    value = digest.hexdigest()
    return (
        value,
        stable,
        PathGuard.capture(
            vault_root,
            relative,
            leaf_policy="content",
            expected_content_hash=value,
            expected_content_size=size,
        ),
    )


def _spooled_jpeg(img) -> tuple[tempfile.SpooledTemporaryFile[bytes], int, str]:
    stream = tempfile.SpooledTemporaryFile(max_size=_JPEG_SPOOL_MEMORY_BYTES, mode="w+b")
    try:
        _save_jpeg(img, stream)
        size = stream.tell()
        stream.seek(0)
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        stream.seek(0)
        return stream, size, digest.hexdigest()
    except BaseException:
        stream.close()
        raise


def _has_owned_scene_frames(video_path: Path) -> bool:
    directory = frames_dir_for(video_path)
    try:
        return directory.is_dir() and any(
            parse_frame_ts(
                child.name[:-3] if child.name.casefold().endswith(".md") else child.name
            )
            is not None
            for child in directory.iterdir()
        )
    except OSError:
        return True


def write_scene_frames(
    vault_root: Path,
    video_path: Path,
    scenes_with_images: list[tuple[embeddings.Scene, object]],
    *,
    today: dt.date | None = None,
) -> list[tuple[Path, Path]]:
    """Persist one JPEG + pending sidecar per scene → `[(jpg_path, sidecar_path)]`.

    Fresh JPEG bytes and their classified sidecars share one held rollback set.
    Open/schema-v3 vaults retain legacy delete-then-insert reprocessing. Exact-v4
    refuses an existing frame set before deletion until removal publication has
    its own atomic successor protocol. Encoding failures remain per-frame soft
    failures; exact-v4 catalog refusal/uncertainty is surfaced to its caller.
    """
    root = Path(os.path.abspath(vault_root))
    try:
        canonical_video = Path(os.path.abspath(video_path))
        video_rel = canonical_video.relative_to(root).as_posix()
        parent_sha256, parent_identity_guard, parent_content_guard = _content_guard(
            root, video_rel
        )
    except (ValueError, OSError) as e:
        log.warning("scene frames skipped for %s: %s", video_path.name, e)
        return []
    d = frames_dir_for(canonical_video)
    # Tag context from Knowledge Base/Evidence/<scope>/<category>/… (same derivation
    # as preserve.ensure_media_sidecar).
    parts = video_rel.split("/")
    scope = parts[2] if len(parts) > 2 else "evidence"
    category = parts[3] if len(parts) > 3 else "uncategorized"
    date_iso = (today or dt.date.today()).isoformat()
    writes: list[PlannedWrite] = []
    out: list[tuple[Path, Path]] = []
    streams: list[tempfile.SpooledTemporaryFile[bytes]] = []
    for i, (scene, img) in enumerate(scenes_with_images):
        try:
            timestamp_ms = frame_timestamp_ms(scene.rep_ts)
            name = _frame_filename_ms(i, timestamp_ms)
            stream, artifact_size, artifact_sha256 = _spooled_jpeg(img)
        except Exception as e:  # noqa: BLE001 — one bad frame must not block the rest
            log.warning("scene frame preparation failed for scene %d: %s", i, e)
            continue
        jpg = d / name
        sidecar = jpg.with_name(name + ".md")
        artifact_rel = jpg.relative_to(root).as_posix()
        try:
            md = _render_sidecar(
                artifact_name=name,
                scope=scope,
                category=category,
                date_iso=date_iso,
                description=(
                    f"Scene frame of `{canonical_video.name}` at "
                    f"{_format_mmss(timestamp_ms / 1000.0)} "
                    f"(parent: {video_rel})."
                ),
                media_type="image",
                evidence_file=artifact_rel,
                extracted_by="pending",
                parent_media=video_rel,
                frame_ts=timestamp_ms / 1000.0,
                binary_sha256=artifact_sha256,
                binary_size=artifact_size,
                governance_artifact_path=artifact_rel,
                governance_artifact_sha256=artifact_sha256,
                governance_artifact_size=artifact_size,
                governance_parent_path=video_rel,
                governance_parent_sha256=parent_sha256,
                governance_frame_timestamp_ms=timestamp_ms,
            )
        except Exception as error:  # noqa: BLE001 - keep per-frame isolation
            stream.close()
            log.warning("scene frame companion failed for %s: %s", name, error)
            continue
        streams.append(stream)
        writes.append(
            PlannedWrite(
                path=jpg,
                content=PreparedBinaryContent(stream, artifact_size, artifact_sha256),
                create_only=True,
                expected_hash=MISSING_CONTENT_HASH,
            )
        )
        writes.append(
            PlannedWrite(
                path=sidecar,
                content=md,
                create_only=True,
                expected_hash=MISSING_CONTENT_HASH,
            )
        )
        out.append((jpg, sidecar))
    if not writes:
        return []
    from .governance import catalog_publication

    try:
        try:
            prepared_catalog = catalog_publication.prepare_planned_markdown_batch(
                root,
                writes=tuple(writes),
            )
        except catalog_publication.CatalogPublicationError as error:
            raise catalog_publication.CatalogCommitError(
                "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED",
                str(error),
            ) from error
        if prepared_catalog is not None and _has_owned_scene_frames(canonical_video):
            raise catalog_publication.CatalogCommitError(
                "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED",
                "exact-v4 scene-frame replacement requires a removal successor",
            )
        if prepared_catalog is None:
            clear_scene_frames(root, canonical_video)
        batch_atomic_write(
            writes,
            vault_root=root,
            required_guards=(parent_identity_guard,),
            completion_guards=(parent_content_guard,),
        )
        try:
            catalog_publication.publish_markdown_batch(prepared_catalog)
        except catalog_publication.CatalogPublicationError as error:
            raise catalog_publication.CatalogCommitError(
                "GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN",
                str(error),
            ) from error
    except catalog_publication.CatalogCommitError:
        raise
    except Exception as e:  # noqa: BLE001 — sidecars are the findability layer, not the vectors
        log.warning("scene frame batch write failed for %s: %s", canonical_video.name, e)
        return []
    finally:
        for stream in streams:
            stream.close()
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

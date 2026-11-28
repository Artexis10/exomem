"""Find faces in pictures for the face index: the media worker's face step and the backfill on turn-on.

Faces come from the pictures the CLIP index holds: each image, and each video at the keyframe times of its
CLIP rows. Scene-frame images never get CLIP rows, because the parent video's keyframes own visual search,
so they never get faces either; their faces would double every count. Nothing here runs while faces are
off, and every failure is logged and leaves media processing as it was.

Logs name the file only: no face count, box, vector or name reaches a log line.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    embeddings,
    extract,
    face_groups,
    face_models,
    face_store,
    foreground_activity,
    foreground_priority,
    image_codecs,
    media_store,
)
from .face_groups import PageFace

log = logging.getLogger(__name__)

#: Pictures per backfill batch: the work between two reads of the CLIP index's next paths.
BATCH = 32
#: Paths read from the CLIP index at a time, and checked against the face index in one query.
_WINDOW = 256

_LOCK = threading.Lock()
_MODELS: face_models.FaceModels | None = None
#: Why the models could not load in this process; the next attempt waits for `unload`.
_UNAVAILABLE: str | None = None


def models(vault_root: Path) -> face_models.FaceModels | None:
    """The face models, loaded once per process, or None when they cannot run here or faces are off.

    The first attempt records the outcome in the face index, which `faces.status` reports. A face step
    that started before the owner turned faces off or deleted them loads nothing and creates no store.
    """
    global _MODELS, _UNAVAILABLE
    with _LOCK:
        if not face_store.active(vault_root):
            return None
        if _MODELS is not None or _UNAVAILABLE is not None:
            return _MODELS
        try:
            _MODELS = face_models.load()
        except face_models.FacesUnavailable as error:
            _UNAVAILABLE = str(error)
            log.warning("faces unavailable: %s", error)
        if not face_store.active(vault_root):
            # Turned off or deleted during the load; `unload` waits on this lock, so drop what it would.
            _MODELS = _UNAVAILABLE = None
            return None
        try:
            with closing(face_store.connect_index(vault_root, create=False)) as index:
                face_store.record_availability(index, _UNAVAILABLE)
        except (sqlite3.Error, OSError):
            log.warning("faces: the face index could not record model availability", exc_info=True)
        return _MODELS


def unload() -> None:
    """Drop the loaded models and a recorded failure; the next face step loads them again."""
    global _MODELS, _UNAVAILABLE
    with _LOCK:
        _MODELS = None
        _UNAVAILABLE = None


def _faces(found: face_models.FaceModels, picture: Any, frame_ts: float | None) -> list[PageFace]:
    width, height = picture.size
    out = []
    for face in found.faces(picture):
        region = face.region(width, height)
        out.append(PageFace(frame_ts, region, min(region[2] * width, region[3] * height), face.score, face.vector))
    return out


def image_faces(found: face_models.FaceModels, path: Path) -> list[PageFace]:
    """The faces of an image, with boxes in the upright picture that `read_media` shows."""
    from PIL import ImageOps

    with image_codecs.open_image(path) as opened:
        picture = ImageOps.exif_transpose(opened).convert("RGB")
    return _faces(found, picture, None)


def keyframe_faces(found: face_models.FaceModels, keyframes: Sequence[tuple[float, Any]]) -> list[PageFace]:
    """The faces of a video's keyframes, each with its keyframe time."""
    out: list[PageFace] = []
    for frame_ts, image in keyframes:
        if image is not None:
            out.extend(_faces(found, image, float(frame_ts)))
    return out


def detect(vault_root: Path, path: Path, keyframes: Sequence[tuple[float, Any]] | None = None) -> list[PageFace] | None:
    """The faces of one picture: the image at `path`, or a video's decoded `keyframes`.

    None when the models cannot run or detection failed; the caller then stores nothing for the picture.
    """
    found = models(vault_root)
    if found is None:
        return None
    try:
        return keyframe_faces(found, keyframes) if keyframes is not None else image_faces(found, path)
    except Exception:  # noqa: BLE001 - a face failure must never stop the picture's other media steps
        log.warning("face detection failed for %s", Path(path).name, exc_info=True)
        return None


def commit(vault_root: Path, page: str, faces: Sequence[PageFace] | None, *, mtime: float | None = None) -> bool:
    """Store a picture's faces and group them, unless faces were turned off meanwhile. Never raises.

    `mtime` is the modification time of the bytes the faces came from. Without faces to store, a picture
    whose stored faces came from other bytes loses them: its original changed.
    """
    try:
        if faces is None or not face_store.active(vault_root):
            if mtime is not None:
                face_groups.forget_changed(vault_root, page, mtime)
            return False
        face_groups.record_page(vault_root, page, faces, model_id=face_models.embedder_model_id(), mtime=mtime)
        if _removed(vault_root, page):
            # A removal that ran between the caller's last check and this commit found no face to drop, and
            # reconcile finds removed pictures by CLIP rows it already dropped. A removal drops the file before
            # its cleanup, so a removal this check misses runs its cleanup after the commit and drops them.
            # Changed bytes are left to their own CLIP job: it replaces the faces and keeps the owner's exclusions.
            face_groups.forget_pages(vault_root, [page])
            return False
    except face_groups.FacesStopped:
        return False  # Turned off or deleted while this picture was in flight.
    except Exception:  # noqa: BLE001 - a face failure must never stop the picture's other media steps
        log.warning("face index write failed for %s", Path(page).name, exc_info=True)
        return False
    return True


def _removed(vault_root: Path, page: str) -> bool:
    """Whether a removal took the picture: no bytes are held, and it is not an evicted original, which keeps rows."""
    from . import media_processing, originals

    try:
        if media_processing.held_path(vault_root, vault_root / page).is_file():
            return False
        return not originals.evicted(vault_root, page)
    except Exception:  # noqa: BLE001 - undecided: dropping costs a rescan and the owner's exclusions on that picture; keeping could leave an erased face
        return True


# --- Backfill on turn-on ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Progress:
    """How far the face index covers the pictures that the CLIP index holds."""

    #: Pages the face index records as scanned with the current embedder.
    processed: int
    #: Pictures the CLIP index holds; None when it cannot be read, which status reports as `unknown`.
    total: int | None


def progress(vault_root: Path) -> Progress:
    model_id = face_models.embedder_model_id()
    try:
        total: int | None = embeddings.get_clip_index(vault_root).picture_count()
    except (FileNotFoundError, sqlite3.Error, RuntimeError):
        total = None
    return Progress(face_groups.scanned_count(vault_root, model_id=model_id), total)


def _scan_picture(vault_root: Path, clip_index: Any, page: str) -> bool:
    """Detect and store the faces of one indexed picture. False when it was not stored."""
    from . import media_processing

    binary = vault_root / page
    is_video = extract.media_type_for(binary) == "video"
    times = clip_index.frame_timestamps(page) if is_video else None
    try:
        before = media_processing.held_path(vault_root, binary).stat()
        with media_store.logical_view(vault_root, binary) as view:
            if is_video:
                keyframes = list(zip(times, embeddings._decode_frames_at(view, times), strict=True)) if times else []
                faces = detect(vault_root, view, keyframes)
            else:
                faces = detect(vault_root, view)
        after = media_processing.held_path(vault_root, binary).stat()
    except Exception:  # noqa: BLE001 - one unreadable picture must not end the backfill
        log.warning("face backfill skipped %s", binary.name, exc_info=True)
        return False
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        return False  # The picture changed; its new CLIP job runs the face step on the new bytes.
    return commit(vault_root, page, faces, mtime=after.st_mtime)


def backfill_batch(vault_root: Path, *, after: str = "", limit: int = BATCH) -> tuple[int, str | None]:
    """Scan up to `limit` pictures the face index has not scanned, in path order after `after`.

    Returns how many were stored and the cursor for the next batch, or None when no picture follows.
    The face index is the record of what is done, so a restarted backfill starts from "" and skips them.
    """
    clip_index = embeddings.get_clip_index(vault_root)
    model_id = face_models.embedder_model_id()
    stored = 0
    cursor = after
    while True:
        try:
            window = clip_index.picture_paths(cursor, _WINDOW)
        except FileNotFoundError:
            return stored, None
        done = face_groups.scanned(vault_root, window, model_id=model_id)
        for page in window:
            cursor = page
            if page in done:
                continue
            foreground_priority.yield_in_bulk()
            if not face_store.active(vault_root):
                return stored, None
            stored += _scan_picture(vault_root, clip_index, page)
            if stored >= limit:
                return stored, cursor
        if len(window) < _WINDOW:
            return stored, None


class _Backfill:
    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="exomem-face-backfill", daemon=True)

    def _run(self) -> None:
        cursor: str | None = ""
        try:
            with foreground_activity.background_scope(self.vault_root), foreground_priority.bulk(stop=self.stop):
                while cursor is not None and not self.stop.is_set() and face_store.active(self.vault_root):
                    if models(self.vault_root) is None:
                        return
                    _, cursor = backfill_batch(self.vault_root, after=cursor)
        except foreground_priority.BulkCancelled:
            log.info("face backfill stopped")
        except Exception:  # noqa: BLE001 - the next start resumes from the face index
            log.warning("face backfill failed; the next start resumes it", exc_info=True)


_RUNNERS: dict[Path, _Backfill] = {}
_RUNNERS_LOCK = threading.Lock()


def start_backfill(vault_root: Path) -> bool:
    """Start scanning the pictures the face index lacks, in the background. False when faces do not run here.

    One backfill runs per vault; a second start while it runs does nothing. It runs at background
    priority: each picture waits for requests in flight first.
    """
    if not face_store.active(vault_root):
        return False
    key = Path(vault_root).resolve()
    with _RUNNERS_LOCK:
        running = _RUNNERS.get(key)
        if running is not None and running.thread.is_alive():
            return True
        runner = _Backfill(key)
        _RUNNERS[key] = runner
        runner.thread.start()
    return True


def stop_backfill(vault_root: Path, *, timeout: float | None = None) -> None:
    """Stop the vault's backfill; it processes no further picture once the current one ends."""
    with _RUNNERS_LOCK:
        runner = _RUNNERS.pop(Path(vault_root).resolve(), None)
    if runner is not None:
        runner.stop.set()
        runner.thread.join(timeout)


def backfill_running(vault_root: Path) -> bool:
    with _RUNNERS_LOCK:
        runner = _RUNNERS.get(Path(vault_root).resolve())
    return runner is not None and runner.thread.is_alive()

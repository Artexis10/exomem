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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from . import embeddings, index_sync, prior_claims, semantic_contract
from .governance import projected_retrieval
from .preserve import _render_sidecar
from .vault import (
    MISSING_CONTENT_HASH,
    DeferredGraphCompletion,
    PathGuard,
    PlannedWrite,
    PreparedBinaryContent,
    batch_atomic_write,
    content_hash,
    read_guarded_bytes,
    read_guarded_text,
)

if TYPE_CHECKING:
    from .governance import catalog_publication

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


def projection_clip_samples(
    frames: list[tuple[float, object]],
) -> tuple[projected_retrieval.ProjectionClipSample, ...]:
    """Canonicalize one scene embedding result for immutable v4 publication."""
    return tuple(
        projected_retrieval.ProjectionClipSample(
            frame_timestamp_ms(timestamp),
            tuple(float(value) for value in vector),
        )
        for timestamp, vector in frames
    )


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


def stored_frames_folder(vault_root: Path, frames: Path) -> str | None:
    """The vault-relative frames folder when a store-mode vault owns it, else None (file mode)."""
    from .vault_store import mode, ownership

    if not mode.store_mode_active(vault_root):
        return None
    try:
        relative = frames.resolve().relative_to(Path(vault_root).resolve()).as_posix()
    except (ValueError, OSError):
        return None
    return relative if ownership.owns_folder(relative) else None


def _clear_in_store(vault_root: Path, frames: Path, frames_rel: str) -> int:
    """Clear a video's frames in a store-mode vault: unlink each JPEG, then one commit.

    A frame's JPEG is a binary, whose bytes are on disk, and its sidecar is a stored page. The
    commit deletes every frame sidecar the store holds and the record of every JPEG it unlinked.
    It runs on the store thread, as a media sidecar commit does; a thread with no writer and no
    service to hop to raises before anything is unlinked. A kill between the unlinks and the commit
    leaves the sidecar pages: the next sweep drops the records of the gone JPEGs, and reconcile's
    frame pass clears the pages. Returns the number of JPEGs and sidecar pages removed.
    """
    from .vault_store import availability, batch, read, write
    from .vault_store import media as store_media

    if store_media.off_store_thread(vault_root):
        return store_media.on_store_thread(
            vault_root,
            lambda: _clear_in_store(vault_root, frames, frames_rel),
            operation="vault_store_clear_scene_frames",
        )
    writer = availability.page_writer(vault_root)
    with read.snapshot(vault_root) as view:
        sidecars, recorded = _stored_frames(view, frames_rel)
    unlinked: list[str] = []
    if frames.is_dir():
        for jpg in sorted(frames.iterdir()):
            if parse_frame_ts(jpg.name) is None:
                continue
            try:
                jpg.unlink()
            except OSError as e:
                log.warning("could not remove stale scene frame %s: %s", jpg.name, e)
                continue
            unlinked.append(jpg.name)
    # A JPEG that stayed keeps its record; one that is gone, by this pass or before it, loses it.
    gone = sorted(recorded - {f"{frames_rel}/{name}" for name in _on_disk(frames)})
    if sidecars or gone:
        batch.commit_batch(
            writer,
            [],
            vault_root=vault_root,
            lifecycle=batch.Lifecycle(
                deletes=tuple(write.PageDelete(rel, absent_ok=True) for rel in sidecars),
                binary_deletes=tuple(write.BinaryDelete(rel, on_disk=True) for rel in gone),
            ),
        )
    return len(unlinked) + len(sidecars)


def _stored_frames(view, frames_rel: str) -> tuple[list[str], set[str]]:
    """The frame sidecar pages that the store holds below ``frames_rel``, and the frame JPEGs that
    it records there, from two range reads of ``view``."""
    prefix = f"{frames_rel}/"
    sidecars = [
        meta.path
        for meta in view.pages(prefix)
        if parse_frame_ts(meta.path[len(prefix):].removesuffix(".md")) is not None
    ]
    recorded = {
        meta.path
        for meta in view.binaries(prefix)
        if parse_frame_ts(meta.path[len(prefix):]) is not None
    }
    return sidecars, recorded


def _on_disk(frames: Path) -> set[str]:
    """The names of the entries still in a frames folder."""
    try:
        return {entry.name for entry in frames.iterdir()}
    except OSError:
        return set()


def clear_scene_frames(vault_root: Path, video_path: Path) -> int:
    """Remove the frames this feature owns (`scene-*.jpg` + sidecars) for a video.

    The delete half of delete-then-insert re-processing (mirrors
    `ClipIndex.upsert_frames`). Removed sidecars are purged from the text
    embedding index so stale rows don't linger. Returns the number of files removed.
    """
    d = frames_dir_for(video_path)
    frames_rel = stored_frames_folder(vault_root, d)
    if frames_rel is not None:
        return _clear_in_store(vault_root, d, frames_rel)
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
    frames_rel = stored_frames_folder(vault_root, d)
    if frames_rel is not None:
        # The store holds each sidecar page and records each JPEG; the folder on disk may trail it.
        from .vault_store import read

        with read.snapshot(vault_root) as view:
            sidecars, recorded = _stored_frames(view, frames_rel)
        return sorted({*sidecars, *recorded})
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
    from . import originals

    shared = originals.pooled(vault_root, vault_root / relative)
    if shared is not None:
        # A shared original has no file at its path. Its identity is the immutable pool object
        # that its filing binds, and the exact bytes of that filing guard the binding.
        raw, guard = read_guarded_bytes(vault_root, shared.sidecar_path)
        if shared.binary_sha256 is None or hashlib.sha256(raw).hexdigest() != shared.sidecar_hash:
            raise ValueError("the filing of a shared original changed while it was read")
        return shared.binary_sha256, guard, guard
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


@dataclass
class PreparedSceneFrames:
    """Scene frames encoded and rendered with no vault boundary held, waiting for their commit.

    Encoding a frame and rendering its sidecar need no vault state, so they run before any
    boundary is taken; `commit_scene_frames` then holds the boundary only to publish them. A
    caller that does not commit must `close()` it to release the spooled JPEG bytes.
    """

    root: Path
    video: Path
    video_rel: str
    parent_identity_guard: PathGuard
    parent_content_guard: PathGuard
    parent_clip_samples: tuple[projected_retrieval.ProjectionClipSample, ...] | None
    writes: list[PlannedWrite]
    written: list[tuple[Path, Path]]
    streams: list[tempfile.SpooledTemporaryFile[bytes]]

    def close(self) -> None:
        for stream in self.streams:
            stream.close()

    def drop_reserved(self) -> None:
        """Drop each frame pair whose sidecar path a moved page still claims; the boundary is held.

        A frame pair that moved away still claims these names, so its old links never reach a
        re-extracted frame; that one frame is skipped. A move can land after preparation and no
        commit guard rechecks a reservation, so this reads the newest claims inside the hold.
        """
        kept: list[tuple[Path, Path]] = []
        for jpg, sidecar in self.written:
            try:
                prior_claims.require_unreserved(self.root, sidecar.relative_to(self.root).as_posix())
            except Exception as error:  # noqa: BLE001 - keep per-frame isolation
                log.warning("scene frame companion failed for %s: %s", jpg.name, error)
                continue
            kept.append((jpg, sidecar))
        if len(kept) == len(self.written):
            return
        paths = {path for pair in kept for path in pair}
        self.writes = [write for write in self.writes if write.path in paths]
        self.written = kept


def prepare_scene_frames(
    vault_root: Path,
    video_path: Path,
    scenes_with_images: list[tuple[embeddings.Scene, object]],
    *,
    parent_clip_samples: tuple[projected_retrieval.ProjectionClipSample, ...] | None = None,
    today: dt.date | None = None,
) -> PreparedSceneFrames | None:
    """Encode one JPEG and render one pending sidecar per scene, holding no vault boundary.

    Returns None when the video cannot be hashed or no frame could be prepared. Encoding failures
    are per-frame soft failures. The parent video's content hash is taken here; the commit's
    guards prove the video unchanged when the frames publish, and the commit drops the frames
    whose names a moved page still claims (`PreparedSceneFrames.drop_reserved`).
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
        return None
    if parent_clip_samples is not None:
        if type(parent_clip_samples) is not tuple or any(
            not isinstance(sample, projected_retrieval.ProjectionClipSample)
            for sample in parent_clip_samples
        ):
            raise ValueError("parent CLIP samples must be a finite immutable tuple")
        expected_timestamps = tuple(
            frame_timestamp_ms(scene.rep_ts) for scene, _image in scenes_with_images
        )
        if (
            tuple(sample.frame_timestamp_ms for sample in parent_clip_samples)
            != expected_timestamps
        ):
            raise ValueError("parent CLIP samples do not match the scene frame set")
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
        return None
    return PreparedSceneFrames(
        root, canonical_video, video_rel, parent_identity_guard, parent_content_guard,
        parent_clip_samples, writes, out, streams,
    )


def commit_scene_frames(
    prepared: PreparedSceneFrames,
    *,
    deferred: bool = False,
    publication_intents_out: list[object] | None = None,
) -> tuple[list[tuple[Path, Path]], DeferredGraphCompletion | None]:
    """Publish prepared frames; the caller holds the vault boundary and closes ``prepared``.

    Returns the committed `[(jpg_path, sidecar_path)]` and, when ``deferred``, the handoff whose
    fan-out the caller completes after releasing the boundary (the commit then fans nothing out
    and registers its watcher self-writes in ``publication_intents_out``). Fresh JPEG bytes and
    their sidecars share one held rollback set. When the caller supplied the scene sampler's
    canonical parent CLIP samples, exact-v4 binds them to the unchanged parent sidecar in that
    same successor. Open/schema-v3 vaults retain legacy delete-then-insert reprocessing, and the
    removal of a previous frame set keeps its fan-out inside the hold so that it precedes the new
    frames' fan-out. Exact-v4 refuses an existing frame set before deletion until removal
    publication has its own atomic successor protocol. It first drops the frames whose names a
    moved page claims (`PreparedSceneFrames.drop_reserved`), and commits nothing, clearing no
    earlier frame set, when none remain. A batch failure soft-fails to `[]`; exact-v4 catalog
    refusal/uncertainty is surfaced to the caller.
    """
    from .governance import catalog_publication, graph_producer
    from .vault_store import mode as store_mode

    root = prepared.root
    prepared.drop_reserved()
    if not prepared.writes:
        return [], None
    writes = prepared.writes
    clip_replacements: tuple[catalog_publication.ClipMeasurementReplacement, ...] = ()
    parent_sidecar_guard: PathGuard | None = None
    if prepared.parent_clip_samples is not None:
        # Read with the boundary held, so the binding names the sidecar bytes that publish with it.
        parent_sidecar_rel = f"{prepared.video_rel}.md"
        parent_source, parent_sidecar_guard = read_guarded_text(root, root / parent_sidecar_rel)
        clip_replacements = (
            catalog_publication.ClipMeasurementReplacement(
                item_identity=parent_sidecar_rel,
                content_hash=content_hash(parent_source),
                samples=prepared.parent_clip_samples,
            ),
        )

    def graph_replacement_provider():
        return graph_producer.replacements_for_planned_markdown(
            root,
            before_corpus=semantic_contract.build_corpus_context(root),
            writes=tuple(writes),
        )

    handoff: DeferredGraphCompletion | None = None
    intent_count = len(publication_intents_out) if publication_intents_out is not None else 0
    try:
        try:
            prepared_catalog = catalog_publication.prepare_planned_markdown_batch(
                root,
                writes=tuple(writes),
                clip_replacements=clip_replacements,
                graph_replacement_provider=graph_replacement_provider,
            )
        except catalog_publication.CatalogPublicationError as error:
            raise catalog_publication.CatalogCommitError(
                "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED",
                str(error),
            ) from error
        if prepared_catalog is not None and _has_owned_scene_frames(prepared.video):
            raise catalog_publication.CatalogCommitError(
                "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED",
                "exact-v4 scene-frame replacement requires a removal successor",
            )
        if prepared_catalog is None:
            clear_scene_frames(root, prepared.video)
        batch_kwargs: dict[str, object] = {}
        if deferred:
            batch_kwargs["post_commit_fanout"] = False
            # A store-mode batch commits its graph rows with its pages, so nothing is deferred.
            if not store_mode.store_mode_active(root):
                batch_kwargs["defer_graph_completion"] = True
                batch_kwargs["publication_intents_out"] = publication_intents_out
        written = batch_atomic_write(
            writes,
            vault_root=root,
            required_guards=(
                prepared.parent_identity_guard,
                *((parent_sidecar_guard,) if parent_sidecar_guard is not None else ()),
            ),
            completion_guards=(
                prepared.parent_content_guard,
                *((parent_sidecar_guard,) if parent_sidecar_guard is not None else ()),
            ),
            **batch_kwargs,
        )
        if deferred:
            handoff = (
                written
                if isinstance(written, DeferredGraphCompletion)
                else DeferredGraphCompletion(tuple(written), None, None)
            )
        try:
            catalog_publication.publish_markdown_batch(prepared_catalog)
        except catalog_publication.CatalogPublicationError as error:
            if publication_intents_out is not None and len(publication_intents_out) > intent_count:
                from . import file_watcher

                # The caller never completes this batch's fan-out, so its self-write suppression ends here.
                file_watcher.abort_publication_intents(
                    publication_intents_out[intent_count:],
                    force_paths=[write.path for write in writes],
                )
            raise catalog_publication.CatalogCommitError(
                "GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN",
                str(error),
            ) from error
    except catalog_publication.CatalogCommitError:
        raise
    except Exception as e:  # noqa: BLE001 — sidecars are the findability layer, not the vectors
        log.warning("scene frame batch write failed for %s: %s", prepared.video.name, e)
        return [], None
    return prepared.written, handoff


def write_scene_frames(
    vault_root: Path,
    video_path: Path,
    scenes_with_images: list[tuple[embeddings.Scene, object]],
    *,
    parent_clip_samples: tuple[projected_retrieval.ProjectionClipSample, ...] | None = None,
    today: dt.date | None = None,
) -> list[tuple[Path, Path]]:
    """Persist one JPEG + pending sidecar per scene → `[(jpg_path, sidecar_path)]`.

    The synchronous form: it prepares, commits and fans out in the caller's own boundary
    context. The media worker prepares before it takes the boundary and completes the fan-out
    after releasing it (`prepare_scene_frames`, `commit_scene_frames`).
    """
    from .vault_store import media as store_media

    if store_media.off_store_thread(vault_root):
        # A store-mode vault commits the frames and their sidecars on the store thread, as the
        # media sidecar commit does, inside the vault boundary that the store thread takes.
        return store_media.on_store_thread(
            vault_root,
            lambda: write_scene_frames(
                vault_root,
                video_path,
                scenes_with_images,
                parent_clip_samples=parent_clip_samples,
                today=today,
            ),
            operation="background_media_scene_frame_commit",
        )
    prepared = prepare_scene_frames(
        vault_root,
        video_path,
        scenes_with_images,
        parent_clip_samples=parent_clip_samples,
        today=today,
    )
    if prepared is None:
        return []
    try:
        written, _handoff = commit_scene_frames(prepared)
    finally:
        prepared.close()
    return written


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

"""Background media-extraction worker — fills pending media sidecars off the request path.

When a media binary is uploaded without text, `preserve()` writes a `pending` stub sidecar
and the `/upload` route enqueues a job here. A single background thread (the GPU is
serialized) runs ASR/OCR/PDF extraction (`extract.extract_text`), writes the transcript into
the sidecar (`preserve.update_sidecar_extraction`) and re-embeds it — so the 201 returns
immediately and the binary becomes searchable shortly after.

In-memory queue (no DB). A startup `scan_pending()` re-enqueues any `extracted_by: pending`
sidecar so a restart doesn't strand jobs — mirroring exomem's reconcile-heals-drift approach.
A genuine extraction error marks the sidecar `extracted_by: failed: …` so it won't loop.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from . import embeddings, extract, index_sync, preserve, scene_frames, semantic_segments
from .backfill import iter_kb_files

log = logging.getLogger(__name__)

_MEDIA_TYPE_RE = re.compile(r"(?m)^media_type:\s*(\S+)\s*$")
_EVIDENCE_FILE_RE = re.compile(r"(?m)^evidence_file:\s*(.+?)\s*$")
_PENDING_MARKER = "extracted_by: pending"
_PARENT_MEDIA_MARKER = "\nparent_media:"


@dataclass
class _Job:
    binary_path: Path
    sidecar_path: Path
    media_type: str
    do_ocr: bool = True    # transcribe/OCR/read → fill the sidecar text
    do_clip: bool = False  # CLIP-embed (images only) → ClipIndex
    do_reembed: bool = False  # re-embed the sidecar (semantic re-segmentation)


class MediaWorker:
    """A single-thread extraction queue. One worker = the GPU is used serially."""

    def __init__(self, vault_root: Path) -> None:
        self._vault_root = vault_root
        self._q: queue.Queue[_Job | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._clip_index = embeddings.get_clip_index(vault_root)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="kb-media-worker", daemon=True
            )
            self._thread.start()
            log.info("media extraction worker started")
            # Warm the ASR model off the request path so the first audio/video upload
            # isn't a multi-minute cold-start (large-v3 ~3 GB loads lazily on first use).
            # Separate daemon thread so image/PDF/CLIP jobs aren't blocked while it loads.
            threading.Thread(
                target=extract.prewarm, name="kb-asr-prewarm", daemon=True
            ).start()
            # Boot-time diarization readiness line (cheap: a path check + small JSON
            # read) — the soft-fail design otherwise hides a broken stack entirely.
            extract.log_diarization_readiness(self._vault_root)

    def enqueue(
        self,
        *,
        binary_path: Path,
        sidecar_path: Path,
        media_type: str,
        do_ocr: bool = True,
        do_clip: bool = False,
        do_reembed: bool = False,
    ) -> None:
        self._q.put(
            _Job(
                binary_path=binary_path,
                sidecar_path=sidecar_path,
                media_type=media_type,
                do_ocr=do_ocr,
                do_clip=do_clip,
                do_reembed=do_reembed,
            )
        )

    def stop(self) -> None:
        self._q.put(None)

    def join(self, timeout: float | None = None) -> None:
        """Block until the queue drains — used in tests."""
        self._q.join() if timeout is None else _join_with_timeout(self._q, timeout)

    def _run(self) -> None:
        while True:
            job = self._q.get()
            try:
                if job is None:
                    return
                self._process(job)
            except Exception:  # noqa: BLE001 — a bad job must never kill the worker
                log.exception("media worker job crashed: %s", getattr(job, "binary_path", "?"))
            finally:
                self._q.task_done()

    def _process(self, job: _Job) -> None:
        if job.do_ocr:
            self._run_extraction(job)
        if job.do_clip:
            self._run_clip(job)
        if job.do_reembed:
            self._run_reembed(job)

    def _run_reembed(self, job: _Job) -> None:
        """Re-embed a sidecar so semantic segmentation re-runs with late signals.

        Enqueued AFTER a video's frame-OCR jobs (FIFO ⇒ runs once they're done),
        so segment boundaries can use visual + OCR events. Soft-fails; the
        earlier embed (transcript+speaker signals only) remains valid.
        """
        try:
            index_sync.upsert_after_write(self._vault_root, [job.sidecar_path])
            log.info("re-embedded %s (post-frame-OCR segmentation)", job.sidecar_path.name)
        except Exception:  # noqa: BLE001 — enrichment, never fatal
            log.exception("re-embed failed for %s", job.sidecar_path.name)

    def _run_extraction(self, job: _Job) -> None:
        try:
            result = extract.extract_text(
                job.binary_path, media_type=job.media_type, vault_root=self._vault_root
            )
        except extract.ExtractionUnavailable as e:
            # Engine not installed on this box right now — leave the sidecar `pending`
            # so a properly-provisioned box picks it up on its next restart scan.
            log.warning("extraction unavailable for %s: %s", job.binary_path.name, e)
            return
        except Exception as e:  # noqa: BLE001 — a corrupt file shouldn't re-loop forever
            log.exception("extraction failed for %s", job.binary_path.name)
            preserve.update_sidecar_extraction(
                self._vault_root, job.sidecar_path, text="", engine=f"failed: {type(e).__name__}"
            )
            return
        text = result.text.strip() or "(no text detected)"
        preserve.update_sidecar_extraction(
            self._vault_root, job.sidecar_path, text=text, engine=result.engine,
            speakers=result.speakers,
        )
        log.info(
            "extracted %s via %s (%d chars)", job.binary_path.name, result.engine, len(result.text)
        )

    def _run_clip(self, job: _Job) -> None:
        """CLIP-embed an image (one vector) or a video (per-keyframe vectors) so it's
        findable by visual content — video at the specific moment, not as one blur."""
        is_video = job.media_type == "video"
        scene_pairs: list | None = None
        try:
            if is_video and scene_frames.scene_frames_enabled():
                # One decode pass yields both the per-scene vectors AND the full-res
                # representative images to persist (EXOMEM_VIDEO_SCENE_FRAMES).
                frames, scene_pairs = embeddings.embed_video_scenes(job.binary_path)
            elif is_video:
                frames = embeddings.embed_video_frames(job.binary_path)
            else:
                vec = embeddings.embed_image(job.binary_path)
        except embeddings.ClipUnavailable as e:
            log.warning("CLIP unavailable for %s: %s", job.binary_path.name, e)
            return
        except Exception:  # noqa: BLE001 — a bad image must not kill the worker
            log.exception("CLIP embedding failed for %s", job.binary_path.name)
            return
        try:
            rel = job.binary_path.resolve().relative_to(self._vault_root.resolve()).as_posix()
            mtime = job.binary_path.stat().st_mtime
        except (ValueError, OSError) as e:
            log.warning("CLIP skip %s: %s", job.binary_path.name, e)
            return
        if is_video:
            self._clip_index.upsert_frames(rel, frames, mtime)
            log.info("CLIP-indexed %s (%d keyframes)", job.binary_path.name, len(frames))
            if scene_pairs:
                self._persist_scene_frames(job, scene_pairs)
        else:
            self._clip_index.upsert(rel, vec, mtime)
            log.info("CLIP-indexed %s", job.binary_path.name)

    def _persist_scene_frames(self, job: _Job, pairs: list) -> None:
        """Write scene JPEGs + pending sidecars, then queue each frame for OCR only.

        Soft-fails entirely: the video's vectors are already upserted, so a frame
        persistence failure only costs the viewable frames, never the search index.
        OCR jobs go through the normal queue (`do_clip=False` — frame children never
        get their own ClipIndex rows; the parent video's vectors own visual search).
        """
        try:
            written = scene_frames.write_scene_frames(self._vault_root, job.binary_path, pairs)
        except Exception:  # noqa: BLE001 — persistence is strictly additive
            log.exception("scene frame persistence failed for %s", job.binary_path.name)
            return
        for jpg, sidecar in written:
            self.enqueue(
                binary_path=jpg,
                sidecar_path=sidecar,
                media_type="image",
                do_ocr=True,
                do_clip=False,
            )
        if written and semantic_segments.semantic_segments_enabled():
            # Trailing re-embed of the PARENT sidecar: the FIFO queue guarantees it
            # runs after every frame OCR above, so semantic segmentation sees the
            # visual + OCR boundary events.
            self.enqueue(
                binary_path=job.binary_path,
                sidecar_path=job.sidecar_path,
                media_type=job.media_type,
                do_ocr=False,
                do_clip=False,
                do_reembed=True,
            )
        if written:
            log.info(
                "scene frames: wrote %d frame(s) for %s", len(written), job.binary_path.name
            )

    def scan_pending(self) -> int:
        """Restart recovery: re-enqueue pending OCR + CLIP-index un-indexed images."""
        return self._scan_pending_ocr() + self._scan_unindexed_images()

    def _scan_pending_ocr(self) -> int:
        """Re-enqueue every `extracted_by: pending` sidecar under the KB. Returns count."""
        kb = self._vault_root / "Knowledge Base"
        if not kb.is_dir():
            return 0
        n = 0
        pending_parents: dict[str, bool] = {}  # insertion-ordered dedup
        for sidecar in iter_kb_files(kb):
            if sidecar.suffix.lower() != ".md":
                continue
            try:
                head = sidecar.read_text(encoding="utf-8")[:800]
            except OSError:
                continue
            if _PENDING_MARKER not in head:
                continue
            ef = _EVIDENCE_FILE_RE.search(head)
            if not ef:
                continue
            binary = self._vault_root / ef.group(1).strip()
            mt_match = _MEDIA_TYPE_RE.search(head)
            media_type = mt_match.group(1) if mt_match else extract.media_type_for(binary)
            if media_type and binary.exists():
                self.enqueue(binary_path=binary, sidecar_path=sidecar, media_type=media_type)
                n += 1
                if _PARENT_MEDIA_MARKER in head:
                    pm = re.search(r"(?m)^parent_media:\s*(.+?)\s*$", head)
                    if pm:
                        pending_parents[pm.group(1).strip()] = True
        # Deduped parent re-embeds AFTER the pending frame children above, so
        # semantic segmentation re-runs once their OCR completes (gate on only).
        if pending_parents and semantic_segments.semantic_segments_enabled():
            for parent_rel in pending_parents:
                parent_sidecar = self._vault_root / (parent_rel + ".md")
                parent_binary = self._vault_root / parent_rel
                if parent_sidecar.exists() and parent_binary.exists():
                    self.enqueue(
                        binary_path=parent_binary,
                        sidecar_path=parent_sidecar,
                        media_type=extract.media_type_for(parent_binary) or "video",
                        do_ocr=False,
                        do_clip=False,
                        do_reembed=True,
                    )
                    n += 1
        if n:
            log.info("media worker: re-enqueued %d pending extraction(s)", n)
        return n

    def _scan_unindexed_images(self) -> int:
        """CLIP-queue KB images that have a sidecar but aren't indexed yet.

        Sidecar-LESS images (pre-feature files) are skipped on purpose: `find()` can't
        surface a CLIP match without a `<image>.md` sidecar, so indexing them here would be
        wasted work. The deliberate `exomem backfill-media` pass writes their sidecars
        (+ OCR + CLIP) — this incremental scan only tops up already-sidecar'd images.
        """
        if not embeddings.clip_enabled():
            return 0
        kb = self._vault_root / "Knowledge Base"
        if not kb.is_dir():
            return 0
        n = 0
        for f in iter_kb_files(kb):
            mt = extract.media_type_for(f)
            if mt not in ("image", "video"):  # video is CLIP-able too (keyframes)
                continue
            sidecar = f.with_name(f.name + ".md")
            if not sidecar.exists():
                continue  # no sidecar → not findable; backfill-media handles these
            try:
                rel = f.resolve().relative_to(self._vault_root.resolve()).as_posix()
            except (ValueError, OSError):
                continue
            # Video keys on per-keyframe rows so a legacy single-vector video
            # (frame_ts NULL) is re-queued for per-keyframe indexing; image = any row.
            if (self._clip_index.has_frames(rel) if mt == "video" else self._clip_index.has(rel)):
                continue
            # Scene-frame children (sidecar carries `parent_media:`) never get their own
            # ClipIndex rows — the parent video's per-scene vectors own visual search.
            try:
                if _PARENT_MEDIA_MARKER in sidecar.read_text(encoding="utf-8")[:800]:
                    continue
            except OSError:
                continue
            self.enqueue(
                binary_path=f,
                sidecar_path=f.with_name(f.name + ".md"),
                media_type=mt,
                do_ocr=False,
                do_clip=True,
            )
            n += 1
        if n:
            log.info("media worker: CLIP-queued %d un-indexed image(s)/video(s)", n)
        return n


def _join_with_timeout(q: queue.Queue, timeout: float) -> None:
    """queue.join() honoring a timeout (queue has no native timed join)."""
    import time

    deadline = time.monotonic() + timeout
    while q.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.02)

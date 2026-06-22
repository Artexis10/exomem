"""Background media-extraction worker — fills pending Evidence sidecars off the request path.

When a media binary is uploaded without text, `preserve()` writes a `pending` stub sidecar
and the `/upload` route enqueues a job here. A single background thread (the GPU is
serialized) runs ASR/OCR/PDF extraction (`extract.extract_text`), writes the transcript into
the sidecar (`preserve.update_sidecar_extraction`) and re-embeds it — so the 201 returns
immediately and the binary becomes searchable shortly after.

In-memory queue (no DB). A startup `scan_pending()` re-enqueues any `extracted_by: pending`
sidecar so a restart doesn't strand jobs — mirroring kb-mcp's reconcile-heals-drift approach.
A genuine extraction error marks the sidecar `extracted_by: failed: …` so it won't loop.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from . import embeddings, extract, preserve

log = logging.getLogger(__name__)

_MEDIA_TYPE_RE = re.compile(r"(?m)^media_type:\s*(\S+)\s*$")
_EVIDENCE_FILE_RE = re.compile(r"(?m)^evidence_file:\s*(.+?)\s*$")
_PENDING_MARKER = "extracted_by: pending"


@dataclass
class _Job:
    binary_path: Path
    sidecar_path: Path
    media_type: str
    do_ocr: bool = True    # transcribe/OCR/read → fill the sidecar text
    do_clip: bool = False  # CLIP-embed (images only) → ClipIndex


class MediaWorker:
    """A single-thread extraction queue. One worker = the GPU is used serially."""

    def __init__(self, vault_root: Path) -> None:
        self._vault_root = vault_root
        self._q: queue.Queue[_Job | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._clip_index = embeddings.ClipIndex(vault_root)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="kb-media-worker", daemon=True
            )
            self._thread.start()
            log.info("media extraction worker started")

    def enqueue(
        self,
        *,
        binary_path: Path,
        sidecar_path: Path,
        media_type: str,
        do_ocr: bool = True,
        do_clip: bool = False,
    ) -> None:
        self._q.put(
            _Job(
                binary_path=binary_path,
                sidecar_path=sidecar_path,
                media_type=media_type,
                do_ocr=do_ocr,
                do_clip=do_clip,
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

    def _run_extraction(self, job: _Job) -> None:
        try:
            result = extract.extract_text(job.binary_path, media_type=job.media_type)
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
            self._vault_root, job.sidecar_path, text=text, engine=result.engine
        )
        log.info(
            "extracted %s via %s (%d chars)", job.binary_path.name, result.engine, len(result.text)
        )

    def _run_clip(self, job: _Job) -> None:
        """CLIP-embed an image into the ClipIndex so it's findable by visual content."""
        try:
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
        self._clip_index.upsert(rel, vec, mtime)
        log.info("CLIP-indexed %s", job.binary_path.name)

    def scan_pending(self) -> int:
        """Restart recovery: re-enqueue pending OCR + CLIP-index un-indexed images."""
        return self._scan_pending_ocr() + self._scan_unindexed_images()

    def _scan_pending_ocr(self) -> int:
        """Re-enqueue every `extracted_by: pending` sidecar under Evidence/. Returns count."""
        evidence = self._vault_root / "Knowledge Base" / "Evidence"
        if not evidence.is_dir():
            return 0
        n = 0
        for sidecar in evidence.rglob("*.md"):
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
        if n:
            log.info("media worker: re-enqueued %d pending extraction(s)", n)
        return n

    def _scan_unindexed_images(self) -> int:
        """CLIP-queue every Evidence image not yet in the index (mirrors the OCR scan)."""
        if not embeddings.clip_enabled():
            return 0
        evidence = self._vault_root / "Knowledge Base" / "Evidence"
        if not evidence.is_dir():
            return 0
        n = 0
        for f in evidence.rglob("*"):
            if not f.is_file() or extract.media_type_for(f) != "image":
                continue
            try:
                rel = f.resolve().relative_to(self._vault_root.resolve()).as_posix()
            except (ValueError, OSError):
                continue
            if self._clip_index.has(rel):
                continue
            self.enqueue(
                binary_path=f,
                sidecar_path=f.with_name(f.name + ".md"),
                media_type="image",
                do_ocr=False,
                do_clip=True,
            )
            n += 1
        if n:
            log.info("media worker: CLIP-queued %d un-indexed image(s)", n)
        return n


def _join_with_timeout(q: queue.Queue, timeout: float) -> None:
    """queue.join() honoring a timeout (queue has no native timed join)."""
    import time

    deadline = time.monotonic() + timeout
    while q.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.02)

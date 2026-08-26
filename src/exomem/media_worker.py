"""Durable, disposable media extraction runtime.

When a media binary is uploaded without text, `preserve()` writes a `pending` stub sidecar
and the `/upload` route enqueues a job here. A single background thread (the GPU is
serialized) runs ASR/OCR/PDF extraction (`extract.extract_text`), writes the transcript into
the sidecar (`preserve.update_sidecar_extraction`) and re-embeds it — so the 201 returns
immediately and the binary becomes searchable shortly after.

The long-lived service is a lightweight supervisor. Product mode records jobs in a
SQLite ledger and starts one child process per vault on demand. The child serializes
heavy work, stays warm for a bounded burst, then exits so RAM and accelerator contexts
are returned to the host. Inline mode preserves a deterministic test/debug path.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import (
    deferred_index,
    embeddings,
    extract,
    graph_sync,
    index_sync,
    preserve,
    recall_policy,
    scene_frames,
    semantic_segments,
)
from .backfill import iter_kb_files
from .cli_ops import OpError
from .governance import projected_retrieval
from .kbdir import kb_dirname
from .media_jobs import (
    BLOCKED,
    FAILED,
    MediaJobStore,
    is_guarded_sidecar_sharing_violation,
    pid_alive,
)
from .media_jobs import (
    MediaJob as _Job,
)
from .vault import (
    MISSING_CONTENT_HASH,
    DeferredGraphCompletion,
    PathGuard,
    PlannedWrite,
    batch_atomic_write,
    content_hash,
    post_commit_batch_fanout,
    read_bounded_guarded_bytes,
)
from .writer_lease import get_manager

log = logging.getLogger(__name__)

_MEDIA_TYPE_RE = re.compile(r"(?m)^media_type:\s*(\S+)\s*$")
_EVIDENCE_FILE_RE = re.compile(r"(?m)^evidence_file:\s*(.+?)\s*$")
_PENDING_MARKER = "extracted_by: pending"
_PARENT_MEDIA_MARKER = "\nparent_media:"
_MAX_IDENTITY_HASH_BYTES = 8 * 1024 * 1024


def _is_canonical_sidecar(sidecar: Path, binary: Path) -> bool:
    """Whether `sidecar` is the one sidecar `binary` owns (`<binary>.md`, beside it).

    Case-insensitive on the directory because a vault-relative `evidence_file:`
    need not match the on-disk casing.
    """
    if sidecar.name != binary.name + ".md":
        return False
    return os.path.normcase(str(sidecar.parent)) == os.path.normcase(str(binary.parent))
_COMPLETE = "complete"
_STALE = "stale"
_BLOCKED_ACTION = "install the required media dependency, then retry"
_RENDERER_ACTION = "check the timestamp renderer, then retry"
_FAILED_ACTION = "repair or replace the media artifact, then retry"
_TRANSIENT_OPERATION_CODES = frozenset(
    {
        "MUTATION_BUSY",
        "MUTATION_LOCK_UNAVAILABLE",
        "WRITER_COORDINATOR_UNAVAILABLE",
        # Deliberately transient too. A misconfigured coordinator URL is not
        # fixed by asking again, but it IS fixed by an operator without
        # restarting anything -- so the caller waits rather than crashing. What
        # changes is only how often it asks; see the recheck cadence below.
        "WRITER_COORDINATOR_CONTRACT_ABSENT",
        "WRITER_FENCED",
        "WRITER_LEASE_REQUIRED",
    }
)
_FOLLOWER_RECHECK_SECONDS = 5.0
#: A coordinator URL that answers but does not serve the lease contract returns
#: the same answer to every request, so the follower cadence buys nothing and
#: costs a request every five seconds per server process, indefinitely. Slow
#: enough that a stuck deployment is quiet; short enough that fixing the URL
#: takes effect without a restart (#550).
_CONTRACT_ABSENT_RECHECK_SECONDS = 300.0
_TRANSIENT_EXIT_CODE = 75
_LOCK_UNAVAILABLE_EXIT_CODE = 76
_TRANSIENT_RECHECK_SECONDS = 5.0
_LOCK_UNAVAILABLE_RECHECK_SECONDS = 30.0


def _writer_authority_available() -> bool:
    """Derived from :func:`_probe_writer_authority`, never a second probe.

    The supervisor needs the refusing code to pick a recheck cadence and other
    callers need only the boolean. Keeping one seam means a test that stubs
    either reaches both; two independent probes meant a stub of this one left
    the real one running, and its assertion fired on the supervisor thread
    where pytest could only report it as a warning.
    """
    return _probe_writer_authority() is None


def _probe_writer_authority() -> str | None:
    """The code refusing write authority, or ``None`` when this replica holds it."""
    try:
        # cause="probe": an availability poll must not count as write activity
        # for the lease idle-release timer.
        get_manager().ensure_writer(cause="probe")
    except OpError as exc:
        if exc.code in _TRANSIENT_OPERATION_CODES:
            return exc.code
        raise
    return None


def _authority_recheck_seconds(code: str) -> float:
    if code == "WRITER_COORDINATOR_CONTRACT_ABSENT":
        return _CONTRACT_ABSENT_RECHECK_SECONDS
    return _FOLLOWER_RECHECK_SECONDS


@dataclass(frozen=True)
class _ProcessOutcome:
    state: str
    error: str | None = None


def _content_digest(path: Path) -> str | None:
    """Small-file optimistic identity used to protect a sidecar commit."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _stat_identity(path: Path) -> tuple[int, int, int, int, int] | None:
    """Cheap parent-media identity that is safe to revalidate while locked."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _binary_identity(
    path: Path,
) -> tuple[tuple[int, int, int, int, int], str | None] | None:
    """Stable media identity: bounded content hash, stat fallback for large files."""
    before = _stat_identity(path)
    if before is None:
        return None
    if before[2] > _MAX_IDENTITY_HASH_BYTES:
        return (before, None)
    digest = _content_digest(path)
    if digest is None or _stat_identity(path) != before:
        return None
    return (before, digest)


class MediaWorker:
    """Media supervisor with process-default and inline execution modes."""

    def __init__(
        self,
        vault_root: Path,
        *,
        execution_mode: str | None = None,
        idle_seconds: float | None = None,
    ) -> None:
        self._vault_root = vault_root
        selected = execution_mode or os.environ.get("EXOMEM_MEDIA_WORKER_MODE", "process")
        if selected not in {"process", "inline"}:
            raise ValueError("media worker mode must be process or inline")
        self._execution_mode = selected
        self._idle_seconds = idle_seconds if idle_seconds is not None else _idle_seconds()
        self._q: queue.Queue[_Job | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._clip_index = embeddings.get_clip_index(vault_root)
        self._store = MediaJobStore(vault_root) if selected == "process" else None
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        self._child: subprocess.Popen | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            if self._execution_mode == "process":
                assert self._store is not None
                recovered = self._store.recover_transient_failures()
                if recovered:
                    log.info(
                        "media worker: recovered %d transient coordination failure(s)",
                        recovered,
                    )
                sharing_recovered = self._store.recover_sharing_failures()
                if sharing_recovered:
                    log.info(
                        "media worker: recovered %d sidecar sharing failure(s)",
                        sharing_recovered,
                    )
                target = self._supervise
                name = "exomem-media-supervisor"
            else:
                target = self._run
                name = "exomem-media-inline"
            self._thread = threading.Thread(
                target=target, name=name, daemon=True
            )
            self._thread.start()
            log.info("media extraction %s runtime started", self._execution_mode)
            # Explicit opt-in is honored only for inline/debug mode. Product process
            # mode starts no child and loads no model until durable work exists.
            if self._execution_mode == "inline" and extract.asr_prewarm_enabled():
                threading.Thread(
                    target=extract.prewarm, name="exomem-asr-prewarm", daemon=True
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
        if not self._is_automatic_media_sidecar(sidecar_path):
            return
        job = _Job(
            id=None,
            binary_path=binary_path,
            sidecar_path=sidecar_path,
            media_type=media_type,
            do_ocr=do_ocr,
            do_clip=do_clip,
            do_reembed=do_reembed,
        )
        if self._execution_mode == "process":
            assert self._store is not None
            self._store.enqueue(job)
            self._wake.set()
        else:
            self._q.put(job)

    def _is_automatic_media_sidecar(self, sidecar: Path) -> bool:
        """Gate automatic work without opening a structured-only sidecar."""
        if not recall_policy.is_structured_only_path(self._vault_root, sidecar):
            return True
        try:
            rel = sidecar.relative_to(self._vault_root).as_posix()
        except ValueError:
            return False
        self._clip_index.purge_markdown_paths_if_present([rel])
        return False

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()
        if self._execution_mode == "inline":
            self._q.put(None)
        child = self._child
        if child is not None and child.poll() is None:
            with contextlib.suppress(OSError):
                child.terminate()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def join(self, timeout: float | None = None) -> None:
        """Block until the queue drains — used in tests."""
        if self._execution_mode == "inline":
            self._q.join() if timeout is None else _join_with_timeout(self._q, timeout)
            return
        assert self._store is not None
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            counts = self._store.counts()
            if counts["pending"] == 0 and counts["running"] == 0:
                return
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("media worker queue did not drain")
            time.sleep(0.02)

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

    def _process(self, job: _Job) -> _ProcessOutcome:
        if not self._is_recall_admitted_media_sidecar(job.sidecar_path):
            if self._store is not None:
                self._store.complete(job)
            return _ProcessOutcome(_COMPLETE)
        outcome = _ProcessOutcome(_COMPLETE)
        if job.do_ocr:
            outcome = self._run_extraction(job)
        if job.do_clip:
            self._run_clip(job)
        if job.do_reembed:
            self._run_reembed(job)
        return outcome

    def _is_recall_admitted_media_sidecar(self, sidecar: Path) -> bool:
        """Recheck semantic admission and purge stale CLIP rows when it is lost."""
        if recall_policy.is_recall_candidate(self._vault_root, sidecar):
            return True
        try:
            rel = sidecar.relative_to(self._vault_root).as_posix()
        except ValueError:
            return False
        self._clip_index.purge_markdown_paths_if_present([rel])
        return False

    def _run_reembed(self, job: _Job) -> None:
        """Re-embed a sidecar so semantic segmentation re-runs with late signals.

        Enqueued AFTER a video's frame-OCR jobs (FIFO ⇒ runs once they're done),
        so segment boundaries can use visual + OCR events. Soft-fails; the
        earlier embed (transcript+speaker signals only) remains valid.
        """
        try:
            with get_manager().mutation_guard(
                self._vault_root,
                operation="background_media_reembed_commit",
                holder_kind="background",
            ):
                if not self._is_recall_admitted_media_sidecar(job.sidecar_path):
                    return
                index_sync.upsert_after_write(self._vault_root, [job.sidecar_path])
            log.info("re-embedded %s (post-frame-OCR segmentation)", job.sidecar_path.name)
        except OpError as exc:
            if exc.code in _TRANSIENT_OPERATION_CODES:
                raise
            log.exception("re-embed failed for %s", job.sidecar_path.name)
        except Exception:  # noqa: BLE001 — enrichment, never fatal
            log.exception("re-embed failed for %s", job.sidecar_path.name)

    def _run_extraction(self, job: _Job) -> _ProcessOutcome:
        # A startup/watcher race may complete the transcript after this job was
        # queued or even claimed. Re-read before ASR; the final commit repeats
        # this check under mutation authority to close the compute-window race.
        from . import media_processing

        if not self._is_recall_admitted_media_sidecar(job.sidecar_path):
            return _ProcessOutcome(_COMPLETE)
        try:
            claimed_content = job.sidecar_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            claimed_content = ""
        if media_processing.has_completed_transcript(
            claimed_content, media_type=job.media_type
        ):
            return _ProcessOutcome(_COMPLETE)
        expected_sidecar = _content_digest(job.sidecar_path)
        expected_sidecar_text_hash = content_hash(claimed_content)
        expected_binary = _binary_identity(job.binary_path)
        if expected_sidecar is None or expected_binary is None:
            log.warning("extraction skip %s: input identity unavailable", job.binary_path.name)
            return _ProcessOutcome(_STALE, "input identity unavailable")
        try:
            kwargs = {"media_type": job.media_type, "vault_root": self._vault_root}
            signature = inspect.signature(extract.extract_text)
            parameters = signature.parameters.values()
            accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters)
            if accepts_kwargs or "timestamps" in signature.parameters:
                kwargs["timestamps"] = job.media_type in {"audio", "video"}
            result = extract.extract_text(job.binary_path, **kwargs)
        except extract.TimestampRenderingUnavailable as e:
            error = f"{type(e).__name__}: {e}"
            log.warning("timestamp rendering unavailable for %s: %s", job.binary_path.name, e)
            committed = self._commit_processing_failure(
                job,
                expected_sidecar=expected_sidecar,
                expected_binary=expected_binary,
                state=BLOCKED,
                error=error,
                next_action=_RENDERER_ACTION,
            )
            return _ProcessOutcome(BLOCKED, error) if committed else _ProcessOutcome(_STALE)
        except extract.ExtractionUnavailable as e:
            error = f"{type(e).__name__}: {e}"
            log.warning("extraction unavailable for %s: %s", job.binary_path.name, e)
            committed = self._commit_processing_failure(
                job,
                expected_sidecar=expected_sidecar,
                expected_binary=expected_binary,
                state=BLOCKED,
                error=error,
                next_action=_BLOCKED_ACTION,
            )
            return _ProcessOutcome(BLOCKED, error) if committed else _ProcessOutcome(_STALE)
        except Exception as e:  # noqa: BLE001 — a corrupt file shouldn't re-loop forever
            error = f"{type(e).__name__}: {e}"
            log.exception("extraction failed for %s", job.binary_path.name)
            committed = self._commit_processing_failure(
                job,
                expected_sidecar=expected_sidecar,
                expected_binary=expected_binary,
                state=FAILED,
                error=error,
                next_action=_FAILED_ACTION,
            )
            return _ProcessOutcome(FAILED, error) if committed else _ProcessOutcome(_STALE)
        text = result.text.strip() or "(no text detected)"
        committed = False
        for commit_attempt in range(3):
            committed = self._commit_sidecar_extraction(
                job,
                expected_sidecar=expected_sidecar,
                expected_binary=expected_binary,
                text=text,
                engine=result.engine,
                speakers=result.speakers,
                speaker_verification=result.speaker_verification or "unavailable",
            )
            if committed:
                break
            if (
                _content_digest(job.sidecar_path) != expected_sidecar
                or _binary_identity(job.binary_path) != expected_binary
            ):
                break
            if commit_attempt < 2:
                time.sleep(0.02)
        if not committed:
            stale_parts: list[str] = []
            sidecar_changed = _content_digest(job.sidecar_path) != expected_sidecar
            binary_changed = _binary_identity(job.binary_path) != expected_binary
            if sidecar_changed:
                stale_parts.append("sidecar content changed")
            if binary_changed:
                stale_parts.append("media identity changed")
            if binary_changed and not sidecar_changed:
                preserve.update_sidecar_processing_pending(
                    self._vault_root,
                    job.sidecar_path,
                    attempts=max(1, job.attempts),
                    expected_hash=expected_sidecar_text_hash,
                )
            detail = ", ".join(stale_parts) or "commit precondition changed"
            return _ProcessOutcome(_STALE, f"stale extraction: {detail}")
        log.info(
            "extracted %s via %s (%d chars)", job.binary_path.name, result.engine, len(result.text)
        )
        return _ProcessOutcome(_COMPLETE)

    def _commit_sidecar_extraction(
        self,
        job: _Job,
        *,
        expected_sidecar: str,
        expected_binary: tuple[tuple[int, int, int, int, int], str | None],
        text: str,
        engine: str,
        speakers: list[dict] | None = None,
        speaker_verification: str | None = None,
    ) -> bool:
        token: DeferredGraphCompletion | None = None
        receipts: list[deferred_index.DeferredReceipt] = []
        with get_manager().mutation_guard(
            self._vault_root,
            operation="background_media_extraction_commit",
            holder_kind="background",
        ):
            if not self._is_recall_admitted_media_sidecar(job.sidecar_path):
                return False
            try:
                current_content = job.sidecar_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                current_content = ""
            from . import media_processing

            current_sidecar = _content_digest(job.sidecar_path)
            current_binary = _binary_identity(job.binary_path)
            if current_sidecar != expected_sidecar or current_binary != expected_binary:
                log.warning(
                    "extraction result stale for %s; newer canonical input preserved",
                    job.sidecar_path.name,
                )
                return False
            if media_processing.has_completed_transcript(
                current_content, media_type=job.media_type
            ):
                log.info(
                    "completed transcript appeared before commit for %s; preserving it",
                    job.sidecar_path.name,
                )
                return False
            receipts = deferred_index.add_full_receipts(
                self._vault_root,
                [job.sidecar_path.relative_to(self._vault_root).as_posix()],
            )
            handoff = preserve.update_sidecar_extraction(
                self._vault_root,
                job.sidecar_path,
                text=text,
                engine=engine,
                speakers=speakers,
                speaker_verification=speaker_verification,
                attempts=max(1, job.attempts),
                defer_index_fanout=True,
                defer_graph_completion=True,
            )
            assert isinstance(handoff, DeferredGraphCompletion)
            token = handoff
        assert token is not None
        self._complete_deferred_graph_completion(token, receipts)
        return True

    def _commit_processing_failure(
        self,
        job: _Job,
        *,
        expected_sidecar: str,
        expected_binary: tuple[tuple[int, int, int, int, int], str | None],
        state: str,
        error: str,
        next_action: str,
    ) -> bool:
        token: DeferredGraphCompletion | None = None
        receipts: list[deferred_index.DeferredReceipt] = []
        with get_manager().mutation_guard(
            self._vault_root,
            operation="background_media_failure_commit",
            holder_kind="background",
        ):
            if not self._is_recall_admitted_media_sidecar(job.sidecar_path):
                return False
            current_sidecar = _content_digest(job.sidecar_path)
            current_binary = _binary_identity(job.binary_path)
            if current_sidecar != expected_sidecar or current_binary != expected_binary:
                log.warning(
                    "processing failure stale for %s; newer canonical input preserved",
                    job.sidecar_path.name,
                )
                return False
            try:
                current_content = job.sidecar_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                current_content = ""
            from . import media_processing

            if media_processing.has_completed_transcript(
                current_content, media_type=job.media_type
            ):
                log.info(
                    "completed transcript appeared before failure commit for %s; preserving it",
                    job.sidecar_path.name,
                )
                return False
            receipts = deferred_index.add_full_receipts(
                self._vault_root,
                [job.sidecar_path.relative_to(self._vault_root).as_posix()],
            )
            handoff = preserve.update_sidecar_processing_failure(
                self._vault_root,
                job.sidecar_path,
                state=state,
                attempts=max(1, job.attempts),
                error=error,
                retryable=True,
                next_action=next_action,
                defer_index_fanout=True,
                defer_graph_completion=True,
            )
            assert isinstance(handoff, DeferredGraphCompletion)
            token = handoff
        assert token is not None
        self._complete_deferred_graph_completion(token, receipts)
        return True

    def _complete_deferred_graph_completion(
        self,
        token: DeferredGraphCompletion,
        receipts: list[deferred_index.DeferredReceipt],
    ) -> None:
        try:
            with get_manager().mutation_guard(
                self._vault_root,
                operation="background_media_graph_completion",
                holder_kind="background",
            ):
                floor_path = graph_sync.floor_path(self._vault_root)
                checkpoint_path = graph_sync.checkpoint_path(self._vault_root)
                floor_relative = floor_path.relative_to(self._vault_root).as_posix()
                checkpoint_relative = checkpoint_path.relative_to(self._vault_root).as_posix()
                try:
                    floor_raw, floor_guard = read_bounded_guarded_bytes(
                        self._vault_root,
                        floor_relative,
                        limit=graph_sync._FLOOR_READ_LIMIT,
                    )
                except FileNotFoundError:
                    return
                floor = graph_sync.GraphSyncGenerationFloor.parse(floor_raw)
                if floor is None or floor.generation != token.checkpoint.generation:
                    return
                try:
                    checkpoint_raw, checkpoint_guard = read_bounded_guarded_bytes(
                        self._vault_root,
                        checkpoint_relative,
                        limit=graph_sync._CHECKPOINT_READ_LIMIT,
                    )
                except FileNotFoundError:
                    predecessor = None
                    checkpoint_guard = PathGuard.capture(
                        self._vault_root,
                        checkpoint_relative,
                        leaf_policy="absent",
                    )
                    checkpoint_hash = MISSING_CONTENT_HASH
                else:
                    predecessor = graph_sync.GraphSyncCheckpoint.parse(checkpoint_raw)
                    if predecessor is None:
                        return
                    checkpoint_hash = checkpoint_guard.expected_content_hash
                if predecessor != token.predecessor or checkpoint_hash is None:
                    return
                batch_atomic_write(
                    [
                        PlannedWrite(
                            checkpoint_path,
                            token.checkpoint.render(),
                            guard=checkpoint_guard,
                            expected_hash=checkpoint_hash,
                        )
                    ],
                    vault_root=self._vault_root,
                    required_guards=(floor_guard,),
                    post_commit_fanout=False,
                )
            completed = post_commit_batch_fanout(
                self._vault_root, list(token.replaced), None, None
            )
            if completed is True:
                deferred_index.clear_full_receipts(self._vault_root, receipts)
        except Exception:  # noqa: BLE001 - canonical media is already committed
            log.exception("media deferred graph completion failed")

    def _run_clip(self, job: _Job) -> None:
        """CLIP-embed an image (one vector) or a video (per-keyframe vectors) so it's
        findable by visual content — video at the specific moment, not as one blur."""
        if not self._is_recall_admitted_media_sidecar(job.sidecar_path):
            return
        is_video = job.media_type == "video"
        scene_pairs: list | None = None
        parent_clip_samples: tuple[projected_retrieval.ProjectionClipSample, ...] | None = None
        expected_binary = _binary_identity(job.binary_path)
        if expected_binary is None:
            log.warning("CLIP skip %s: media identity unavailable", job.binary_path.name)
            return
        try:
            if is_video and scene_frames.scene_frames_enabled():
                # One decode pass yields both the per-scene vectors AND the full-res
                # representative images to persist (EXOMEM_VIDEO_SCENE_FRAMES).
                frames, scene_pairs = embeddings.embed_video_scenes(job.binary_path)
                parent_clip_samples = scene_frames.projection_clip_samples(frames)
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
        with get_manager().mutation_guard(
            self._vault_root,
            operation="background_media_clip_commit",
            holder_kind="background",
        ):
            if not self._is_recall_admitted_media_sidecar(job.sidecar_path):
                return
            if _binary_identity(job.binary_path) != expected_binary:
                log.warning(
                    "CLIP result stale for %s; changed media was not indexed",
                    job.binary_path.name,
                )
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
                    assert parent_clip_samples is not None
                    self._persist_scene_frames(
                        job,
                        scene_pairs,
                        parent_clip_samples=parent_clip_samples,
                        expected_binary=expected_binary,
                    )
            else:
                self._clip_index.upsert(rel, vec, mtime)
                log.info("CLIP-indexed %s", job.binary_path.name)

    def _persist_scene_frames(
        self,
        job: _Job,
        pairs: list,
        *,
        parent_clip_samples: tuple[projected_retrieval.ProjectionClipSample, ...],
        expected_binary: tuple[tuple[int, int, int, int, int], str | None] | None = None,
    ) -> None:
        """Write scene JPEGs + pending sidecars, then queue each frame for OCR only.

        Soft-fails entirely: the video's vectors are already upserted, so a frame
        persistence failure only costs the viewable frames, never the search index.
        OCR jobs go through the normal queue (`do_clip=False` — frame children never
        get their own ClipIndex rows; the parent video's vectors own visual search).
        """
        with get_manager().mutation_guard(
            self._vault_root,
            operation="background_media_scene_frame_commit",
            holder_kind="background",
        ):
            if not self._is_recall_admitted_media_sidecar(job.sidecar_path):
                return
            if expected_binary is not None and _binary_identity(job.binary_path) != expected_binary:
                log.warning(
                    "scene-frame result stale for %s; changed media was not persisted",
                    job.binary_path.name,
                )
                return
            try:
                written = scene_frames.write_scene_frames(
                    self._vault_root,
                    job.binary_path,
                    pairs,
                    parent_clip_samples=parent_clip_samples,
                )
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

    def _launch_child(self) -> subprocess.Popen:
        args = [
            sys.executable,
            "-m",
            "exomem.media_worker_child",
            "--vault",
            str(self._vault_root),
            "--parent-pid",
            str(os.getpid()),
            "--idle-seconds",
            str(self._idle_seconds),
        ]
        log.info("media worker: starting disposable child")
        return subprocess.Popen(args)  # noqa: S603 - fixed interpreter/module command

    def _supervise(self) -> None:
        assert self._store is not None
        relaunch_after = 0.0
        try:
            while not self._stop_event.is_set():
                child = self._child
                if child is not None and child.poll() is not None:
                    returncode = child.returncode
                    child_pid = child.pid
                    self._child = None
                    owns_runtime = self._store.worker_pid() == child_pid
                    recovered = self._store.recover_interrupted() if owns_runtime else 0
                    if owns_runtime:
                        self._store.clear_worker(child_pid)
                    if returncode in {_TRANSIENT_EXIT_CODE, _LOCK_UNAVAILABLE_EXIT_CODE}:
                        delay = (
                            _LOCK_UNAVAILABLE_RECHECK_SECONDS
                            if returncode == _LOCK_UNAVAILABLE_EXIT_CODE
                            else _TRANSIENT_RECHECK_SECONDS
                        )
                        log.info(
                            "media child deferred work; retrying after %.1fs",
                            delay,
                        )
                    elif returncode:
                        delay = 2.0
                        log.warning(
                            "media child exited %s; recovered %d job(s)",
                            returncode,
                            recovered,
                        )
                    else:
                        delay = 0.5
                    relaunch_after = time.monotonic() + delay
                if (
                    self._child is None
                    and time.monotonic() >= relaunch_after
                    and self._store.needs_worker()
                ):
                    refusal = _probe_writer_authority()
                    if refusal is not None:
                        relaunch_after = time.monotonic() + _authority_recheck_seconds(refusal)
                    else:
                        try:
                            self._child = self._launch_child()
                        except OSError:
                            log.exception("media worker: could not start child")
                            relaunch_after = time.monotonic() + 5.0
                self._wake.wait(0.5)
                self._wake.clear()
        finally:
            child = self._child
            if child is not None and child.poll() is None:
                with contextlib.suppress(OSError):
                    child.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=5)
            self._child = None

    def scan_pending(self) -> int:
        """Restart recovery: re-enqueue pending OCR + CLIP-index un-indexed images."""
        return self._scan_pending_ocr() + self._scan_unindexed_images()

    def _scan_pending_ocr(self) -> int:
        """Re-enqueue every `extracted_by: pending` sidecar under the KB. Returns count."""
        kb = self._vault_root / kb_dirname()
        if not kb.is_dir():
            return 0
        n = 0
        skipped_strays = 0
        pending_parents: dict[str, bool] = {}  # insertion-ordered dedup
        for sidecar in iter_kb_files(kb):
            if sidecar.suffix.lower() != ".md":
                continue
            if not self._is_automatic_media_sidecar(sidecar):
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
            # Enqueue only the sidecar this binary actually owns. A stray copy —
            # a Syncthing `.sync-conflict-*.md`, a manual duplicate — still carries
            # `evidence_file:` pointing back here, and queueing it minted a SECOND
            # job for the same binary (the job key includes the sidecar path), so
            # two workers extracted one binary into two different sidecars.
            if not _is_canonical_sidecar(sidecar, binary):
                skipped_strays += 1
                continue
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
        if skipped_strays:
            log.warning(
                "media worker: skipped %d non-canonical sidecar copy/copies "
                "(duplicate .md alongside a governed binary)",
                skipped_strays,
            )
        return n

    def _scan_unindexed_images(self) -> int:
        """CLIP-queue KB images that have a sidecar but aren't indexed yet.

        Sidecar-LESS images (pre-feature files) are skipped on purpose: `find()` can't
        surface a CLIP match without a `<image>.md` sidecar, so indexing them here would be
        wasted work. The deliberate `exomem backfill-media` pass writes their sidecars
        (+ OCR + CLIP) — this incremental scan only tops up already-sidecar'd images.
        """
        kb = self._vault_root / kb_dirname()
        if not kb.is_dir():
            return 0
        clip_enabled = embeddings.clip_enabled()
        n = 0
        for f in iter_kb_files(kb):
            mt = extract.media_type_for(f)
            if mt not in ("image", "video"):  # video is CLIP-able too (keyframes)
                continue
            sidecar = f.with_name(f.name + ".md")
            if not self._is_automatic_media_sidecar(sidecar):
                continue
            if not clip_enabled:
                continue
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


def _idle_seconds() -> float:
    try:
        return max(0.1, float(os.environ.get("EXOMEM_MEDIA_IDLE_SECONDS") or "300"))
    except ValueError:
        return 300.0


def run_child(vault_root: Path, *, parent_pid: int, idle_seconds: float) -> int:
    """Claim and process durable jobs until idle or the parent disappears."""
    store = MediaJobStore(vault_root)
    store.recover_interrupted()
    if not _writer_authority_available():
        log.info("media worker: writer authority unavailable; leaving work queued")
        return _TRANSIENT_EXIT_CODE
    store.set_worker(os.getpid(), idle_seconds)
    # Process mode makes scene-frame follow-up enqueue durable in this same ledger;
    # the nested supervisor is never started because the child calls _process directly.
    worker = MediaWorker(vault_root, execution_mode="process", idle_seconds=idle_seconds)
    last_work = time.monotonic()
    try:
        if extract.asr_prewarm_enabled():
            extract.prewarm()
        extract.log_diarization_readiness(vault_root)
        while _parent_alive(parent_pid):
            job = store.claim_next()
            if job is None:
                if time.monotonic() - last_work >= idle_seconds:
                    log.info("media worker: idle deadline reached; child exiting")
                    return 0
                time.sleep(min(0.25, idle_seconds))
                continue
            last_work = time.monotonic()
            try:
                outcome = worker._process(job)
            except OpError as exc:
                assert job.id is not None
                if exc.code in _TRANSIENT_OPERATION_CODES:
                    store.defer(job.id)
                    log.info(
                        "media worker: deferred %s after transient %s",
                        job.binary_path,
                        exc.code,
                    )
                    if exc.code == "MUTATION_LOCK_UNAVAILABLE":
                        return _LOCK_UNAVAILABLE_EXIT_CODE
                    return _TRANSIENT_EXIT_CODE
                log.exception("media child job crashed: %s", job.binary_path)
                store.mark(job.id, FAILED, f"{type(exc).__name__}: {exc}")
            except PermissionError as exc:
                assert job.id is not None
                error = f"{type(exc).__name__}: {exc}"
                if is_guarded_sidecar_sharing_violation(exc, job.sidecar_path):
                    if store.defer_sharing_violation(job.id, error):
                        log.info(
                            "media worker: deferred %s after Windows sidecar sharing denial",
                            job.binary_path,
                        )
                        return _LOCK_UNAVAILABLE_EXIT_CODE
                    log.error(
                        "media worker: sidecar sharing retry limit exhausted for %s",
                        job.binary_path,
                    )
                else:
                    log.exception("media child job crashed: %s", job.binary_path)
                store.mark(job.id, FAILED, error)
            except Exception as exc:  # noqa: BLE001 - preserve worker availability
                log.exception("media child job crashed: %s", job.binary_path)
                assert job.id is not None
                store.mark(job.id, FAILED, f"{type(exc).__name__}: {exc}")
            else:
                assert job.id is not None
                if outcome.state == BLOCKED:
                    store.mark(job.id, BLOCKED, outcome.error)
                elif outcome.state == FAILED:
                    store.mark(job.id, FAILED, outcome.error)
                elif outcome.state == _STALE:
                    try:
                        current_content = job.sidecar_path.read_text(encoding="utf-8")
                    except (OSError, UnicodeError):
                        current_content = ""
                    from . import media_processing

                    binary_only_stale = (
                        "media identity changed" in (outcome.error or "")
                        and "sidecar content changed" not in (outcome.error or "")
                    )
                    if media_processing.has_completed_transcript(
                        current_content, media_type=job.media_type
                    ) or binary_only_stale:
                        store.discard(job)
                    else:
                        store.mark(
                            job.id,
                            FAILED,
                            outcome.error or "stale extraction commit",
                        )
                else:
                    store.complete(job)
        log.info("media worker: parent exited; child stopping")
        return 0
    finally:
        store.clear_worker(os.getpid())


def _parent_alive(parent_pid: int) -> bool:
    if parent_pid <= 0:
        return True
    return pid_alive(parent_pid)

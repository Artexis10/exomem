"""Canonical, import-light orchestration for governed media artifacts.

This leaf classifies a binary, converges its Markdown sidecar, and records one
durable media job.  Model-backed extraction remains the worker's responsibility.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from . import (
    access,
    held_fs,
    media_jobs,
    media_types,
    memory_refs,
    recall_policy,
    reserved_paths,
)
from .cli_ops import OpError
from .kbdir import kb_dirname
from .vault import (
    MISSING_CONTENT_HASH,
    VAULT_SCAN_SKIP_DIRS,
    PlannedWrite,
    batch_atomic_write,
    content_hash,
    parse_frontmatter,
    post_commit_batch_fanout,
    yaml_scalar,
)

log = logging.getLogger(__name__)
DEFAULT_RECONCILE_LIMIT = 100
_RUNTIME_LOCK = threading.Lock()
_RUNTIME_UNAVAILABLE: dict[str, tuple[str, str]] = {}
_TRANSIENT_COORDINATION_CODES = frozenset(
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

# A read-only replica correctly declining the writer lease
# (WRITER_LEASE_REQUIRED) is an EXPECTED operational state, not a crash. The
# background reconcile loop retries the whole pass every cycle, so logging a
# full multi-frame traceback per artifact floods exomem.log and evicts the
# tool-call trace history operators rely on. Record it instead as one concise
# WARNING line with no traceback, throttled down to one line per holder change
# or interval while the condition persists.
_READONLY_REPLICA_CODE = "WRITER_LEASE_REQUIRED"
_READONLY_LOG_LOCK = threading.Lock()
# resolved vault root -> (holder note, monotonic deadline) of the last notice.
_READONLY_LOG_STATE: dict[str, tuple[str, float]] = {}
_READONLY_LOG_INTERVAL_SECONDS = 300.0


def _should_log_readonly_replica(vault: Path, holder_note: str) -> bool:
    """True only when this read-only notice is new, its holder changed, or its
    throttle window lapsed — so a bounded pass over many artifacts, and a
    persistent read-only state across cycles, both collapse to a single line."""
    key = str(Path(vault).resolve())
    now = time.monotonic()
    with _READONLY_LOG_LOCK:
        previous = _READONLY_LOG_STATE.get(key)
        if previous is not None and previous[0] == holder_note and now < previous[1]:
            return False
        _READONLY_LOG_STATE[key] = (holder_note, now + _READONLY_LOG_INTERVAL_SECONDS)
        return True


def _log_reconcile_op_error(
    error: OpError, binary: Path, vault: Path, *, activity: str
) -> None:
    """Record a soft-failed background reconcile at a severity that fits its cause.

    The read-only-replica case (``WRITER_LEASE_REQUIRED``) is expected and gets a
    single throttled WARNING with no traceback; every other OpError keeps its
    full traceback so genuinely unexpected failures stay diagnosable.
    """
    if error.code == _READONLY_REPLICA_CODE:
        if _should_log_readonly_replica(vault, error.message):
            log.warning("%s skipped for read-only replica: %s", activity, error)
        return
    log.warning("%s failed for %s", activity, binary, exc_info=True)


class MediaProcessingError(Exception):
    """Stable orchestration failure exposed by later product surfaces."""

    def __init__(self, code: str, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ReconcileResult:
    media_type: str
    state: str
    sidecar_path: Path
    job_id: int | None
    requeued: int = 0


@dataclass(frozen=True)
class _BinaryProvenance:
    relative_path: str
    original_filename: str
    sha256: str
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int


_EXTRACTED_SECTION_RE = re.compile(
    r"(?ms)^## Extracted text\s*\n(.*?)(?=^## |\Z)"
)
_PRESERVED_HEADING = "## Preserved notes"
# The title + locator lines _render_sidecar emits. Re-emitted on every render, so
# they are regenerated rather than preserved.
_SIDECAR_BOILERPLATE_RE = re.compile(
    r"(?m)^# (?:Evidence|Source): .*$\n?|^Preserved under `[^`]*`\.[ \t]*$\n?"
)
_INCOMPLETE_ENGINES = {"", "none", "pending"}
_PROVENANCE_FIELDS = (
    "evidence_file",
    "original_filename",
    "binary_sha256",
    "binary_size",
    "binary_mtime_ns",
    "binary_ctime_ns",
)
# Content identity: a mismatch here means genuinely different bytes, so the
# recorded transcript no longer describes this artifact.
_IDENTITY_FIELDS = (
    "evidence_file",
    "original_filename",
    "binary_sha256",
    "binary_size",
)
# Cache hints only: filesystem timestamps move without the bytes changing.
_TIMESTAMP_FIELDS = ("binary_mtime_ns", "binary_ctime_ns")


def classify_media(path: str | Path) -> str | None:
    """Return the canonical extraction kind for ``path``, case-insensitively."""
    return media_types.media_type_for(path)


def _preserve_module():
    from . import preserve

    return preserve


def set_media_runtime_available(vault_root: Path) -> None:
    """Mark this process's worker available without creating vault state."""
    with _RUNTIME_LOCK:
        _RUNTIME_UNAVAILABLE.pop(str(Path(vault_root).resolve()), None)


def set_media_runtime_unavailable(
    vault_root: Path, *, reason: str, next_action: str
) -> None:
    """Keep later automatic discoveries actionable for this server lifetime."""
    with _RUNTIME_LOCK:
        _RUNTIME_UNAVAILABLE[str(Path(vault_root).resolve())] = (reason, next_action)


def _runtime_unavailable(vault_root: Path) -> tuple[str, str] | None:
    with _RUNTIME_LOCK:
        return _RUNTIME_UNAVAILABLE.get(str(Path(vault_root).resolve()))


def _is_automatic_media_candidate(vault: Path, binary: Path) -> bool:
    """Reject raw Records media before automatic discovery reads its inputs."""
    sidecar = binary.with_name(binary.name + ".md")
    if not recall_policy.is_structured_only_path(vault, sidecar):
        return True
    try:
        rel_sidecar = sidecar.relative_to(vault).as_posix()
    except ValueError:
        return False
    from .clip_index import ClipIndex

    ClipIndex(vault).purge_markdown_paths_if_present([rel_sidecar])
    return False


def reconcile_media(
    vault_root: Path,
    binary_path: str | Path,
    *,
    explicit: bool = True,
    commit_guard: Callable[[], AbstractContextManager[object]] | None = None,
) -> ReconcileResult | None:
    """Converge one governed media artifact to a sidecar and durable job.

    The binary is only read for provenance.  Sidecar work is atomic and repeated
    calls preserve already-converged bytes while the ledger's media key deduplicates
    enqueue requests.
    """
    vault = Path(vault_root).resolve()
    binary = Path(binary_path)
    if not binary.is_absolute():
        binary = vault / binary
    binary = Path(os.path.abspath(binary))
    if not explicit and not _is_automatic_media_candidate(vault, binary):
        return None

    media_type = classify_media(binary)
    if media_type is None:
        if not explicit:
            return None
        raise MediaProcessingError(
            "UNSUPPORTED_MEDIA",
            f"unsupported media type for {binary.name!r}",
        )

    try:
        lexical_relative = binary.relative_to(vault).as_posix()
    except ValueError as error:
        raise MediaProcessingError(
            "MEDIA_PATH_OUTSIDE_KB",
            f"media path must resolve inside {kb_dirname()}: {binary}",
        ) from error
    if (
        reserved_paths.classify_logical(lexical_relative).disposition
        is reserved_paths.PathDisposition.RESERVED
    ):
        raise MediaProcessingError(
            "MEDIA_NOT_FOUND", "media artifact does not exist"
        )
    rel_binary = lexical_relative
    try:
        retained_identity = reserved_paths.inspect_generic_file(vault, rel_binary)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code not in {"MISSING", "RESERVED_PATH"}:
            _confine_to_knowledge_base(vault, binary)
        raise MediaProcessingError(
            "MEDIA_NOT_FOUND", "media artifact does not exist"
        ) from None
    resolved_binary = _confine_to_knowledge_base(vault, binary)
    tier = access.access_tier(vault, rel_binary)
    if tier in {access.TIER_EXCLUDED, access.TIER_READONLY}:
        if not explicit:
            return None
        raise MediaProcessingError(
            "MEDIA_PATH_ACCESS_DENIED",
            f"media path is {tier} under _access.yaml: {rel_binary}",
        )

    sidecar = binary.with_name(binary.name + ".md")
    _confine_sidecar(vault, sidecar)
    original = _read_sidecar_text(vault, sidecar)
    provenance = _read_provenance(
        vault,
        binary,
        resolved_binary,
        retained_identity,
    )

    completed = original is not None and _completed_provenance_state(
        original, media_type=media_type, provenance=provenance
    )
    repaired = (
        _backfill_completed_provenance(original, provenance)
        if completed == "repairable" and original is not None
        else None
    )
    pending = (
        None
        if completed in {"valid", "repairable"}
        else _render_pending_sidecar(
            binary=binary,
            media_type=media_type,
            provenance=provenance,
            original=original,
        )
    )

    deferred_fanout: list[Path] = []
    deferred_created: list[Path] = []

    def _write_sidecar(write: PlannedWrite) -> None:
        from .governance import catalog_publication

        try:
            written = _preserve_module().commit_media_sidecar_writes(
                vault,
                (write,),
                post_commit_fanout=commit_guard is None,
                batch_writer=batch_atomic_write,
            )
        except catalog_publication.CatalogCommitError as error:
            raise MediaProcessingError(error.code, error.reason) from error
        if commit_guard is not None:
            assert isinstance(written, list)
            deferred_fanout.extend(written)
            if write.create_only or write.expected_hash == MISSING_CONTENT_HASH:
                deferred_created.extend(written)

    result: ReconcileResult | None = None
    boundary = commit_guard() if commit_guard is not None else nullcontext()

    @contextmanager
    def _commit_scope():
        try:
            with boundary:
                yield
        finally:
            if deferred_fanout:
                post_commit_batch_fanout(
                    vault,
                    list(dict.fromkeys(deferred_fanout)),
                    None,
                    None,
                    created_paths=list(dict.fromkeys(deferred_created)),
                )

    with _commit_scope():
        commit_tier = access.access_tier(vault, rel_binary)
        if commit_tier in {access.TIER_EXCLUDED, access.TIER_READONLY}:
            if not explicit:
                return None
            raise MediaProcessingError(
                "MEDIA_PATH_ACCESS_DENIED",
                f"media path is {commit_tier} under _access.yaml: {rel_binary}",
            )
        _confine_sidecar(vault, sidecar)
        _verify_binary_identity(binary, resolved_binary, provenance)
        current = _read_sidecar_text(vault, sidecar)
        if current != original:
            raise MediaProcessingError(
                "MEDIA_CHANGED_DURING_RECONCILIATION",
                f"media sidecar changed while reconciliation was being planned: {sidecar}",
            )

        if completed in {"valid", "repairable"}:
            if repaired is not None:
                assert original is not None
                _write_sidecar(
                    PlannedWrite(
                        path=sidecar,
                        content=repaired,
                        expected_hash=content_hash(original),
                    )
                )
            _verify_binary_identity(binary, resolved_binary, provenance)
            _discard_stale_job(vault, binary, sidecar, media_type)
            result = ReconcileResult(media_type, "completed", sidecar, None)
        else:
            assert pending is not None
            if original != pending:
                expected = (
                    content_hash(original)
                    if original is not None
                    else MISSING_CONTENT_HASH
                )
                _write_sidecar(
                    PlannedWrite(
                        path=sidecar,
                        content=pending,
                        expected_hash=expected,
                    )
                )

            _verify_binary_identity(binary, resolved_binary, provenance)
            store = media_jobs.MediaJobStore(vault)
            job_id = store.enqueue(
                media_jobs.MediaJob(
                    binary_path=binary,
                    sidecar_path=sidecar,
                    media_type=media_type,
                    do_ocr=True,
                    do_clip=media_type in {"image", "video"}
                    and not os.environ.get("EXOMEM_DISABLE_CLIP"),
                )
            )
            durable_job = store.get(job_id)
            state = durable_job.state if durable_job is not None else media_jobs.PENDING
            unavailable = _runtime_unavailable(vault)
            if unavailable is not None:
                reason, next_action = unavailable
                store.mark(job_id, media_jobs.BLOCKED, reason)
                current_sidecar = _read_sidecar_text(vault, sidecar)
                if current_sidecar is None:
                    raise MediaProcessingError(
                        "MEDIA_CHANGED_DURING_RECONCILIATION",
                        "media sidecar disappeared after publication",
                    )
                if not _has_runtime_unavailable_state(
                    current_sidecar, reason=reason, next_action=next_action
                ):
                    blocked_sidecar = _preserve_module().render_sidecar_processing_failure(
                        current_sidecar,
                        state=media_jobs.BLOCKED,
                        attempts=(
                            durable_job.attempts if durable_job is not None else 0
                        ),
                        error=reason,
                        retryable=True,
                        next_action=next_action,
                    )
                    _write_sidecar(
                        PlannedWrite(
                            path=sidecar,
                            content=blocked_sidecar,
                            expected_hash=content_hash(current_sidecar),
                        )
                    )
                state = media_jobs.BLOCKED
            result = ReconcileResult(media_type, state, sidecar, job_id)

    assert result is not None
    return result


def reconcile_all_media(
    vault_root: Path,
    *,
    limit: int = DEFAULT_RECONCILE_LIMIT,
    reconcile_one: Callable[[Path], object] | None = None,
    propagate_transient_errors: bool = False,
) -> int:
    """Reconcile a bounded, pruned pass of supported governed binaries.

    Each artifact is independent: one unreadable or racing file is logged and
    does not prevent later candidates from converging.
    """
    if isinstance(limit, bool) or limit <= 0:
        raise ValueError("media reconciliation limit must be a positive integer")
    vault = Path(vault_root).resolve()
    reconcile_one = reconcile_one or (
        lambda binary: reconcile_media(vault, binary, explicit=False)
    )
    kb = vault / kb_dirname()
    if not kb.is_dir():
        return 0

    store: media_jobs.MediaJobStore | None = None
    if media_jobs.job_store_path(vault).exists():
        store = media_jobs.MediaJobStore(vault, create=False)

    examined = 0
    attempted = 0
    last_examined: Path | None = None
    for binary in _iter_rotating_governed_media(
        vault,
        kb,
        after=store.discovery_cursor() if store is not None else None,
    ):
        if store is None:
            store = media_jobs.MediaJobStore(vault)
        if examined >= limit:
            break
        examined += 1
        last_examined = binary
        if _needs_reconciliation(vault, binary, store):
            attempted += 1
            try:
                reconcile_one(binary)
            except OpError as error:
                if (
                    propagate_transient_errors
                    and error.code in _TRANSIENT_COORDINATION_CODES
                ):
                    raise
                _log_reconcile_op_error(
                    error, binary, vault, activity="media reconciliation"
                )
            except Exception:  # noqa: BLE001 - one artifact must not abort discovery
                log.warning("media reconciliation failed for %s", binary, exc_info=True)
    if last_examined is not None and store is not None:
        store.set_discovery_cursor(last_examined)
    return attempted


def _iter_rotating_governed_media(
    vault: Path,
    kb_root: Path,
    *,
    after: str | None,
):
    if after is None:
        yield from _iter_governed_media(vault, kb_root)
        return

    found_cursor = False
    for binary in _iter_governed_media(vault, kb_root):
        relative = binary.relative_to(vault).as_posix()
        if found_cursor:
            yield binary
        elif relative == after:
            found_cursor = True
    if not found_cursor:
        yield from _iter_governed_media(vault, kb_root)
        return

    for binary in _iter_governed_media(vault, kb_root):
        relative = binary.relative_to(vault).as_posix()
        if relative == after:
            break
        yield binary


def _iter_governed_media(vault: Path, kb_root: Path):
    """Yield supported binaries from a deterministic, hidden/pruned KB walk."""
    stack = [kb_root]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            continue
        directories: list[Path] = []
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                rel = child.relative_to(vault).as_posix()
                if reserved_paths.classify_logical(rel).blocked:
                    continue
                info = child.lstat()
                if stat.S_ISLNK(info.st_mode) or bool(
                    getattr(os.path, "isjunction", lambda _path: False)(child)
                ):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if child.name not in VAULT_SCAN_SKIP_DIRS:
                        directories.append(child)
                elif stat.S_ISREG(info.st_mode) and classify_media(child):
                    try:
                        if info.st_nlink != 1:
                            continue
                        reserved_paths.inspect_generic_file(vault, rel)
                    except (OSError, reserved_paths.ReservedPathLeafError):
                        continue
                    if (
                        _is_automatic_media_candidate(vault, child)
                        and access.access_tier(vault, rel) not in {
                        access.TIER_EXCLUDED,
                        access.TIER_READONLY,
                        }
                    ):
                        yield child
            except OSError:
                continue
        stack.extend(reversed(directories))


def _needs_reconciliation(
    vault: Path,
    binary: Path,
    store: media_jobs.MediaJobStore | None,
) -> bool:
    sidecar = binary.with_name(binary.name + ".md")
    try:
        original = _read_sidecar_text(vault, sidecar)
    except MediaProcessingError:
        return True
    if original is None:
        return True

    media_type = classify_media(binary)
    if media_type is None:
        return False

    frontmatter, body, raw_frontmatter = parse_frontmatter(original)
    if raw_frontmatter is None:
        return True
    if _is_completed_sidecar_shape(vault, binary, frontmatter, body, media_type):
        return store is not None and store.has_binary(binary)
    if _is_pending_sidecar_shape(vault, binary, frontmatter, media_type):
        return store is None or not store.has_binary(binary)
    return True


def _capture_owned_shape_is_valid(frontmatter: dict[str, object]) -> bool:
    """Whether the fields the *capture* owns are present and well-formed.

    The media pipeline owns `media_type`, `evidence_file`, `extracted_by`,
    `processing_state`, and the binary provenance fields. Everything else on the
    page — its identity, its classification, its title, its tags, its body —
    belongs to whoever captured the artifact, so this predicate checks those for
    validity rather than for particular values.

    That distinction used to be absent. Both shape checks required
    `source_type: other`, which is the Evidence sidecar's hard-coded value and
    which no real Source has, so a media page captured as a Source failed every
    check and was rebuilt from scratch — losing its title, kind, domain,
    projects and tags, and having its body demoted under `## Preserved notes`.
    A non-empty `tags` list was required for the same reason, and did the same
    to a capture that legitimately has none.
    """
    captured = frontmatter.get("captured")
    try:
        if isinstance(captured, dt.date):
            captured.isoformat()
        else:
            dt.date.fromisoformat(str(captured))
    except (TypeError, ValueError):
        return False
    title = frontmatter.get("title")
    source_type = frontmatter.get("source_type")
    tags = frontmatter.get("tags")
    return (
        frontmatter.get("type") == "source"
        and memory_refs.normalize_id(frontmatter.get("exomem_id")) is not None
        and isinstance(title, str)
        and bool(title.strip())
        and isinstance(source_type, str)
        and bool(source_type.strip())
        and isinstance(tags, list)
        and all(isinstance(tag, str) and tag.strip() for tag in tags)
        and isinstance(frontmatter.get("ingested_into"), list)
    )


def _is_pending_sidecar_shape(
    vault: Path,
    binary: Path,
    frontmatter: dict[str, object],
    media_type: str,
) -> bool:
    digest = str(frontmatter.get("binary_sha256", ""))
    expected_path = binary.relative_to(vault).as_posix()
    return (
        _capture_owned_shape_is_valid(frontmatter)
        and frontmatter.get("media_type") == media_type
        and frontmatter.get("evidence_file") == expected_path
        and frontmatter.get("original_filename") == binary.name
        and str(frontmatter.get("extracted_by", "")).strip().lower() == "pending"
        and frontmatter.get("processing_state") == "pending"
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and _is_nonnegative_int(frontmatter.get("binary_size"))
        and _is_nonnegative_int(frontmatter.get("binary_mtime_ns"))
        and _is_nonnegative_int(frontmatter.get("binary_ctime_ns"))
    )


def _is_completed_sidecar_shape(
    vault: Path,
    binary: Path,
    frontmatter: dict[str, object],
    body: str,
    media_type: str,
) -> bool:
    if not _is_completed_transcript_shape(frontmatter, body, media_type):
        return False
    present = tuple(field in frontmatter for field in _PROVENANCE_FIELDS)
    if not all(present):
        return False
    try:
        current = binary.stat()
    except OSError:
        return False
    # Deliberately NOT comparing binary_mtime_ns/binary_ctime_ns. They are cache
    # hints, not identity: `binary.stat()` here and the `os.fstat()` that recorded
    # them in _read_provenance can disagree for an unchanged file (see the note in
    # _verify_binary_identity), and on POSIX any metadata touch moves st_ctime.
    # Treating that drift as "not completed" made every pass re-render the sidecar.
    expected = {
        "evidence_file": binary.relative_to(vault).as_posix(),
        "original_filename": binary.name,
        "binary_size": current.st_size,
    }
    digest = str(frontmatter.get("binary_sha256", ""))
    return (
        re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and all(frontmatter.get(field) == value for field, value in expected.items())
    )


def _is_completed_transcript_shape(
    frontmatter: dict[str, object],
    body: str,
    media_type: str,
) -> bool:
    engine = str(frontmatter.get("extracted_by", "")).strip()
    return (
        frontmatter.get("type") == "source"
        and frontmatter.get("media_type") == media_type
        and frontmatter.get("processing_state") in (None, "completed")
        and engine.lower() not in _INCOMPLETE_ENGINES
        and not engine.lower().startswith("failed")
        and any(
            bool(match.group(1).strip())
            for match in _EXTRACTED_SECTION_RE.finditer(body)
        )
    )


def has_completed_transcript(content: str, *, media_type: str) -> bool:
    """Final-commit guard for a transcript completed out of band."""
    frontmatter, body, raw_frontmatter = parse_frontmatter(content)
    return raw_frontmatter is not None and _is_completed_transcript_shape(
        frontmatter, body, media_type
    )


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _require_media_access(vault: Path, binary: Path) -> str:
    rel_binary = binary.relative_to(vault).as_posix()
    tier = access.access_tier(vault, rel_binary)
    if tier in {access.TIER_EXCLUDED, access.TIER_READONLY}:
        raise MediaProcessingError(
            "MEDIA_PATH_ACCESS_DENIED",
            f"media path is {tier} under _access.yaml: {rel_binary}",
        )
    return rel_binary


def _pending_sidecar_matches_current_provenance(
    content: str,
    *,
    vault: Path,
    binary: Path,
    media_type: str,
    provenance: _BinaryProvenance,
) -> bool:
    frontmatter, _body, raw_frontmatter = parse_frontmatter(content)
    if raw_frontmatter is None or not _is_pending_sidecar_shape(
        vault, binary, frontmatter, media_type
    ):
        return False
    values = _provenance_values(provenance)
    return all(frontmatter.get(field) == values[field] for field in _PROVENANCE_FIELDS)


def _block_ambiguous_retry(
    store: media_jobs.MediaJobStore,
    job: media_jobs.MediaJob,
    sidecar: Path,
) -> ReconcileResult:
    assert job.id is not None
    store.mark(
        job.id,
        media_jobs.BLOCKED,
        job.last_error or "BatchWriteError: reconciliation required",
    )
    return ReconcileResult(job.media_type, media_jobs.BLOCKED, sidecar, job.id)


def _retry_ambiguous_batch_failure(
    vault: Path,
    binary: Path,
    job: media_jobs.MediaJob,
    *,
    commit_guard: Callable[[], AbstractContextManager[object]] | None,
) -> ReconcileResult:
    """Resolve only current canonical provenance; retained batches remain inspect-only."""
    sidecar = binary.with_name(binary.name + ".md")
    resolved_binary = _confine_to_knowledge_base(vault, binary)
    _require_media_access(vault, binary)
    boundary = commit_guard() if commit_guard is not None else nullcontext()
    with boundary:
        store = media_jobs.MediaJobStore(vault)
        durable = store.get_by_binary(binary)
        if durable is None:
            return ReconcileResult(job.media_type, media_jobs.BLOCKED, sidecar, job.id)
        if media_jobs._classify_batch_write_failure(durable.last_error) is None:
            return ReconcileResult(
                durable.media_type,
                durable.state,
                durable.sidecar_path,
                durable.id,
            )
        _require_media_access(vault, binary)
        try:
            _confine_sidecar(vault, sidecar)
            provenance = _read_provenance(vault, binary, resolved_binary)
            original = sidecar.read_text(encoding="utf-8")
        except (OSError, UnicodeError, MediaProcessingError):
            return _block_ambiguous_retry(store, durable, sidecar)

        try:
            _verify_binary_identity(binary, resolved_binary, provenance)
            current = sidecar.read_text(encoding="utf-8") if sidecar.exists() else None
        except (OSError, UnicodeError, MediaProcessingError):
            return _block_ambiguous_retry(store, durable, sidecar)
        if current != original:
            return _block_ambiguous_retry(store, durable, sidecar)

        completed = _completed_provenance_state(
            original, media_type=durable.media_type, provenance=provenance
        )
        if completed == "valid":
            store.discard(durable)
            return ReconcileResult(durable.media_type, "completed", sidecar, None)
        if not _pending_sidecar_matches_current_provenance(
            original,
            vault=vault,
            binary=binary,
            media_type=durable.media_type,
            provenance=provenance,
        ):
            return _block_ambiguous_retry(store, durable, sidecar)

        requeued = store.retry(
            binary_path=binary,
            include_failed=True,
            allow_reconciliation_required=True,
        )
        if requeued == 0:
            return _block_ambiguous_retry(store, durable, sidecar)
        return ReconcileResult(
            durable.media_type,
            media_jobs.PENDING,
            sidecar,
            durable.id,
            requeued=requeued,
        )


def retry_media(
    vault_root: Path,
    binary_path: str | Path,
    *,
    commit_guard: Callable[[], AbstractContextManager[object]] | None = None,
) -> ReconcileResult:
    """Explicitly retry one blocked/failed artifact without replacing valid output."""
    vault = Path(vault_root).resolve()
    binary = Path(binary_path)
    if not binary.is_absolute():
        binary = vault / binary
    binary = Path(os.path.abspath(binary))

    if media_jobs.job_store_path(vault).exists():
        store = media_jobs.MediaJobStore(vault, create=False)
        job = store.get_by_binary(binary)
        if job is not None and media_jobs._classify_batch_write_failure(job.last_error) is not None:
            return _retry_ambiguous_batch_failure(
                vault,
                binary,
                job,
                commit_guard=commit_guard,
            )

    result = reconcile_media(vault, binary, commit_guard=commit_guard)
    if result.state == "completed" or result.job_id is None:
        return result
    if _runtime_unavailable(vault) is not None:
        return result

    boundary = commit_guard() if commit_guard is not None else nullcontext()
    with boundary:
        store = media_jobs.MediaJobStore(vault)
        requeued = store.retry(binary_path=binary, include_failed=True)
        if requeued == 0:
            return result
        durable_job = store.get(result.job_id)
        state = durable_job.state if durable_job is not None else media_jobs.PENDING
    return ReconcileResult(
        result.media_type,
        state,
        result.sidecar_path,
        result.job_id,
        requeued=requeued,
    )


def retry_all_media(
    vault_root: Path,
    *,
    limit: int = media_jobs.STATUS_JOB_LIMIT,
    commit_guard: Callable[[], AbstractContextManager[object]] | None = None,
    propagate_transient_errors: bool = False,
) -> int:
    """Reconcile then retry a bounded snapshot of actionable terminal work."""
    if isinstance(limit, bool) or limit <= 0:
        raise ValueError("media retry limit must be a positive integer")
    vault = Path(vault_root).resolve()
    if not media_jobs.job_store_path(vault).exists():
        return 0
    store = media_jobs.MediaJobStore(vault, create=False)
    requeued = 0
    for job in store.retryable_jobs(limit=limit):
        try:
            result = retry_media(
                vault,
                job.binary_path,
                commit_guard=commit_guard,
            )
        except OpError as error:
            if (
                propagate_transient_errors
                and error.code in _TRANSIENT_COORDINATION_CODES
            ):
                raise
            _log_reconcile_op_error(
                error, job.binary_path, vault, activity="media retry reconciliation"
            )
            continue
        except Exception:  # noqa: BLE001 - one stale artifact must not abort the pass
            log.warning("media retry reconciliation failed for %s", job.binary_path, exc_info=True)
            continue
        requeued += result.requeued
    return requeued


def _discard_stale_job(
    vault: Path, binary: Path, sidecar: Path, media_type: str
) -> None:
    if not media_jobs.job_store_path(vault).exists():
        return
    media_jobs.MediaJobStore(vault, create=False).discard(
        media_jobs.MediaJob(
            binary_path=binary,
            sidecar_path=sidecar,
            media_type=media_type,
        )
    )


def _confine_to_knowledge_base(vault: Path, binary: Path) -> Path:
    try:
        binary.relative_to(vault / kb_dirname())
        resolved = binary.resolve(strict=True)
        resolved.relative_to((vault / kb_dirname()).resolve(strict=True))
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError) as exc:
        raise MediaProcessingError(
            "MEDIA_PATH_OUTSIDE_KB",
            f"media path must resolve inside {kb_dirname()}: {binary}",
        ) from exc
    if not resolved.is_file():
        raise MediaProcessingError(
            "MEDIA_PATH_OUTSIDE_KB",
            f"media path is not a regular file: {binary}",
        )
    return resolved


def _confine_sidecar(vault: Path, sidecar: Path) -> None:
    """Reject an existing sidecar symlink that escapes the governed tree."""
    try:
        sidecar.relative_to(vault / kb_dirname())
        resolved = sidecar.resolve(strict=False)
        resolved.relative_to((vault / kb_dirname()).resolve(strict=True))
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError) as exc:
        raise MediaProcessingError(
            "MEDIA_PATH_OUTSIDE_KB",
            f"media sidecar must resolve inside {kb_dirname()}: {sidecar}",
        ) from exc


def _read_sidecar_text(vault: Path, sidecar: Path) -> str | None:
    """Read one ordinary sidecar without turning private aliases into an oracle."""

    try:
        relative = sidecar.relative_to(vault).as_posix()
    except ValueError as error:
        raise MediaProcessingError(
            "MEDIA_PATH_OUTSIDE_KB",
            "media sidecar is outside the vault",
        ) from error
    try:
        snapshot = reserved_paths.read_generic_bytes(vault, relative)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "MISSING":
            return None
        raise MediaProcessingError(
            "MEDIA_NOT_FOUND",
            "media sidecar does not exist",
        ) from None
    try:
        return snapshot.data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as error:
        raise MediaProcessingError(
            "MEDIA_SIDECAR_INVALID",
            "media sidecar is not UTF-8 text",
        ) from error


def _read_provenance(
    vault: Path,
    binary: Path,
    resolved_binary: Path,
    expected_identity: held_fs.StableIdentity | None = None,
) -> _BinaryProvenance:
    if expected_identity is None:
        try:
            relative = binary.relative_to(vault).as_posix()
            expected_identity = reserved_paths.inspect_generic_file(vault, relative)
        except (ValueError, reserved_paths.ReservedPathLeafError):
            raise MediaProcessingError(
                "MEDIA_NOT_FOUND", "media artifact does not exist"
            ) from None
    digest = hashlib.sha256()
    with resolved_binary.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            before.st_dev != expected_identity.device
            or before.st_ino != expected_identity.inode
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
        ):
            raise MediaProcessingError(
                "MEDIA_CHANGED_DURING_RECONCILIATION",
                "media changed while provenance was being recorded",
            )
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    identity_before = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_after = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if identity_after != identity_before or after.st_nlink != 1:
        raise MediaProcessingError(
            "MEDIA_CHANGED_DURING_RECONCILIATION",
            f"media changed while provenance was being recorded: {binary}",
        )
    return _BinaryProvenance(
        relative_path=binary.relative_to(vault).as_posix(),
        original_filename=binary.name,
        sha256=digest.hexdigest(),
        size=before.st_size,
        mtime_ns=before.st_mtime_ns,
        ctime_ns=before.st_ctime_ns,
        device=before.st_dev,
        inode=before.st_ino,
    )


def _verify_binary_identity(
    binary: Path, resolved_binary: Path, provenance: _BinaryProvenance
) -> None:
    try:
        if binary.resolve(strict=True) != resolved_binary:
            raise OSError("media path target changed")
        # Keep the identity source consistent with _read_provenance. On Windows,
        # stat(path) and fstat(open_handle) can report different st_ctime_ns
        # precision for the same unchanged file.
        with resolved_binary.open("rb") as stream:
            current = os.fstat(stream.fileno())
    except OSError as exc:
        raise MediaProcessingError(
            "MEDIA_CHANGED_DURING_RECONCILIATION",
            f"media changed while provenance was being recorded: {binary}",
        ) from exc
    current_identity = (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )
    expected_identity = (
        provenance.device,
        provenance.inode,
        provenance.size,
        provenance.mtime_ns,
        provenance.ctime_ns,
    )
    if current_identity != expected_identity or current.st_nlink != 1:
        raise MediaProcessingError(
            "MEDIA_CHANGED_DURING_RECONCILIATION",
            f"media changed while provenance was being recorded: {binary}",
        )


def _render_pending_sidecar(
    *,
    binary: Path,
    media_type: str,
    provenance: _BinaryProvenance,
    original: str | None,
) -> str:
    preserve = _preserve_module()
    existing_id: str | None = None
    preserved_notes: str | None = None
    if original is not None:
        frontmatter, body, raw_frontmatter = parse_frontmatter(original)
        existing_id = memory_refs.normalize_id(frontmatter.get("exomem_id"))
        if _is_canonical_pending_shape(frontmatter, media_type, provenance):
            rendered = original
            pending_fields = _pending_fields(provenance)
            if frontmatter.get("processing_state") == media_jobs.BLOCKED:
                pending_fields = pending_fields[1:]
            for field, value in pending_fields:
                rendered = preserve._set_frontmatter_field(rendered, field, str(value))
            return rendered
        preserved_notes = _preservable_notes(
            body if raw_frontmatter is not None else original
        )

    tree, scope, category = preserve.artifact_location(provenance.relative_path)
    rendered = preserve._render_sidecar(
        artifact_name=binary.name,
        scope=scope,
        category=category,
        date_iso=dt.date.today().isoformat(),
        media_type=media_type,
        evidence_file=provenance.relative_path,
        extracted_by="pending",
        tree=tree,
    )
    if existing_id is not None:
        rendered = preserve._set_frontmatter_field(rendered, "exomem_id", existing_id)
    for field, value in _pending_fields(provenance):
        rendered = preserve._set_frontmatter_field(rendered, field, str(value))
    if preserved_notes:
        rendered = rendered.rstrip("\n") + "\n\n## Preserved notes\n\n" + preserved_notes
    return rendered


def _preservable_notes(body: str) -> str | None:
    """The content worth carrying into a re-rendered sidecar, or None.

    A prior ``## Preserved notes`` section is UNWRAPPED rather than re-nested,
    and identical segments collapse to one. That is what bounds this: re-rendering
    an already-preserved sidecar reproduces it byte for byte instead of wrapping
    another copy around it, so a sidecar can no longer accumulate a copy of itself
    per reconciliation pass.

    A real transcript is still preserved — on a genuine provenance conflict the
    binary may not re-extract to the same thing, so the recorded text is the only
    record. Only regenerated scaffolding is dropped: the title/locator lines and
    an empty ``## Extracted text`` anchor.
    """
    kept: list[str] = []
    for segment in body.split(_PRESERVED_HEADING):
        prose = _strip_empty_extracted_sections(segment)
        prose = _SIDECAR_BOILERPLATE_RE.sub("", prose).strip()
        if prose and prose not in kept:
            kept.append(prose)
    return "\n\n".join(kept) + "\n" if kept else None


def _strip_empty_extracted_sections(text: str) -> str:
    """Drop `## Extracted text` anchors that carry no transcript."""
    return _EXTRACTED_SECTION_RE.sub(
        lambda match: "" if not match.group(1).strip() else match.group(0), text
    )


def _pending_fields(provenance: _BinaryProvenance) -> tuple[tuple[str, object], ...]:
    return (
        ("processing_state", "pending"),
        ("evidence_file", yaml_scalar(provenance.relative_path)),
        ("original_filename", yaml_scalar(provenance.original_filename)),
        ("binary_sha256", provenance.sha256),
        ("binary_size", provenance.size),
        ("binary_mtime_ns", provenance.mtime_ns),
        ("binary_ctime_ns", provenance.ctime_ns),
    )


def _has_runtime_unavailable_state(
    content: str, *, reason: str, next_action: str
) -> bool:
    frontmatter, _body, raw_frontmatter = parse_frontmatter(content)
    return (
        raw_frontmatter is not None
        and frontmatter.get("processing_state") == media_jobs.BLOCKED
        and frontmatter.get("processing_error") == reason
        and frontmatter.get("processing_retryable") is True
        and frontmatter.get("processing_next_action") == next_action
    )


def _is_canonical_pending_shape(
    frontmatter: dict[str, object],
    media_type: str,
    provenance: _BinaryProvenance,
) -> bool:
    return (
        _capture_owned_shape_is_valid(frontmatter)
        and frontmatter.get("media_type") == media_type
        and frontmatter.get("evidence_file") == provenance.relative_path
        and str(frontmatter.get("extracted_by", "")).strip().lower() == "pending"
    )


def _is_valid_completed_sidecar(
    content: str,
    *,
    media_type: str,
    provenance: _BinaryProvenance,
) -> bool:
    return _completed_provenance_state(
        content, media_type=media_type, provenance=provenance
    ) == "valid"


def _completed_provenance_state(
    content: str,
    *,
    media_type: str,
    provenance: _BinaryProvenance,
) -> str:
    frontmatter, body, raw_frontmatter = parse_frontmatter(content)
    if raw_frontmatter is None:
        return "not-completed"
    if not _is_completed_transcript_shape(frontmatter, body, media_type):
        return "not-completed"
    values = _provenance_values(provenance)
    for field in _IDENTITY_FIELDS:
        if field in frontmatter and frontmatter.get(field) != values[field]:
            return "conflict"
    # Timestamps are NOT identity. Same bytes under the same engine produce the
    # same transcript, so a moved mtime/ctime is drift to heal in place, never a
    # reason to discard a completed transcript and re-extract. Reporting it as a
    # conflict is what drove the pending re-render that nested each sidecar
    # inside itself once per pass.
    drifted = any(
        field in frontmatter and frontmatter.get(field) != values[field]
        for field in _TIMESTAMP_FIELDS
    )
    if drifted or not all(field in frontmatter for field in _PROVENANCE_FIELDS):
        return "repairable"
    return "valid"


def _backfill_completed_provenance(
    content: str, provenance: _BinaryProvenance
) -> str:
    """Heal a completed sidecar's provenance in place, keeping its transcript.

    Fills fields a legacy sidecar never recorded AND refreshes drifted
    mtime/ctime, so the next pass sees "valid" instead of re-reporting drift.
    """
    preserve = _preserve_module()
    rendered = content
    existing, _body, _raw = parse_frontmatter(content)
    # _pending_fields carries the SERIALIZED form (yaml_scalar-quoted for strings);
    # compare against the parsed semantic value so an already-correct quoted field
    # is not rewritten on every pass.
    semantic = _provenance_values(provenance)
    for field, serialized in _pending_fields(provenance)[1:]:
        if field not in existing or existing.get(field) != semantic[field]:
            rendered = preserve._set_frontmatter_field(rendered, field, str(serialized))
    return rendered


def _provenance_values(provenance: _BinaryProvenance) -> dict[str, object]:
    """Provenance as parsed frontmatter would carry it, for comparison."""
    return {
        "evidence_file": provenance.relative_path,
        "original_filename": provenance.original_filename,
        "binary_sha256": provenance.sha256,
        "binary_size": provenance.size,
        "binary_mtime_ns": provenance.mtime_ns,
        "binary_ctime_ns": provenance.ctime_ns,
    }


def mark_processing_unavailable(
    vault_root: Path,
    *,
    reason: str,
    next_action: str,
    commit_guard: Callable[[], AbstractContextManager[object]] | None = None,
) -> int:
    """Make queued automatic work actionable when no runtime can consume it."""
    vault = Path(vault_root).resolve()
    set_media_runtime_unavailable(vault, reason=reason, next_action=next_action)
    if not media_jobs.job_store_path(vault).exists():
        return 0
    store = media_jobs.MediaJobStore(vault, create=False)
    jobs = store.pending_jobs()
    preserve = _preserve_module()
    changed = 0
    for job in jobs:
        if job.id is None:
            continue
        written: list[Path] = []
        boundary = commit_guard() if commit_guard is not None else nullcontext()
        try:
            with boundary:
                current_job = store.get(job.id)
                if current_job is None or current_job.state != media_jobs.PENDING:
                    continue
                if job.sidecar_path.exists():
                    _confine_sidecar(vault, job.sidecar_path)
                    rel_binary = job.binary_path.relative_to(vault).as_posix()
                    tier = access.access_tier(vault, rel_binary)
                    if tier in {access.TIER_EXCLUDED, access.TIER_READONLY}:
                        continue
                    content = job.sidecar_path.read_text(encoding="utf-8")
                    blocked = preserve.render_sidecar_processing_failure(
                        content,
                        state=media_jobs.BLOCKED,
                        attempts=current_job.attempts,
                        error=reason,
                        retryable=True,
                        next_action=next_action,
                    )
                    written_result = preserve.commit_media_sidecar_writes(
                        vault,
                        (
                            PlannedWrite(
                                path=job.sidecar_path,
                                content=blocked,
                                expected_hash=content_hash(content),
                            ),
                        ),
                        post_commit_fanout=commit_guard is None,
                        batch_writer=batch_atomic_write,
                    )
                    assert isinstance(written_result, list)
                    written = written_result
                store.mark(job.id, media_jobs.BLOCKED, reason)
                changed += 1
        except Exception:  # noqa: BLE001 - one stale job must not abort startup
            log.warning(
                "media unavailable-state commit failed for %s",
                job.binary_path,
                exc_info=True,
            )
            continue
        if written and commit_guard is not None:
            post_commit_batch_fanout(vault, written, None, None)
    return changed

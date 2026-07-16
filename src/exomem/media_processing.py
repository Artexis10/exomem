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
from dataclasses import dataclass
from pathlib import Path

from . import access, media_jobs, media_types, memory_refs
from .kbdir import kb_dirname
from .vault import (
    MISSING_CONTENT_HASH,
    VAULT_SCAN_SKIP_DIRS,
    PlannedWrite,
    batch_atomic_write,
    content_hash,
    parse_frontmatter,
    yaml_scalar,
)

log = logging.getLogger(__name__)
DEFAULT_RECONCILE_LIMIT = 100


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
_INCOMPLETE_ENGINES = {"", "none", "pending"}
_PROVENANCE_FIELDS = (
    "evidence_file",
    "original_filename",
    "binary_sha256",
    "binary_size",
    "binary_mtime_ns",
    "binary_ctime_ns",
)


def classify_media(path: str | Path) -> str | None:
    """Return the canonical extraction kind for ``path``, case-insensitively."""
    return media_types.media_type_for(path)


def _preserve_module():
    from . import preserve

    return preserve


def reconcile_media(
    vault_root: Path,
    binary_path: str | Path,
    *,
    explicit: bool = True,
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
    resolved_binary = _confine_to_knowledge_base(vault, binary)

    media_type = classify_media(binary)
    if media_type is None:
        if not explicit:
            return None
        raise MediaProcessingError(
            "UNSUPPORTED_MEDIA",
            f"unsupported media type for {binary.name!r}",
        )

    rel_binary = binary.relative_to(vault).as_posix()
    tier = access.access_tier(vault, rel_binary)
    if tier in {access.TIER_EXCLUDED, access.TIER_READONLY}:
        if not explicit:
            return None
        raise MediaProcessingError(
            "MEDIA_PATH_ACCESS_DENIED",
            f"media path is {tier} under _access.yaml: {rel_binary}",
        )

    provenance = _read_provenance(vault, binary, resolved_binary)
    sidecar = binary.with_name(binary.name + ".md")
    _confine_sidecar(vault, sidecar)
    original = sidecar.read_text(encoding="utf-8") if sidecar.exists() else None

    completed = original is not None and _completed_provenance_state(
        original, media_type=media_type, provenance=provenance
    )
    if completed in {"valid", "repairable"}:
        if completed == "repairable":
            repaired = _backfill_completed_provenance(original, provenance)
            _verify_binary_identity(binary, resolved_binary, provenance)
            batch_atomic_write(
                [
                    PlannedWrite(
                        path=sidecar,
                        content=repaired,
                        expected_hash=content_hash(original),
                    )
                ],
                vault_root=vault,
            )
        _verify_binary_identity(binary, resolved_binary, provenance)
        _discard_stale_job(vault, binary, sidecar, media_type)
        return ReconcileResult(media_type, "completed", sidecar, None)

    pending = _render_pending_sidecar(
        binary=binary,
        media_type=media_type,
        provenance=provenance,
        original=original,
    )
    if original != pending:
        _verify_binary_identity(binary, resolved_binary, provenance)
        expected = content_hash(original) if original is not None else MISSING_CONTENT_HASH
        batch_atomic_write(
            [PlannedWrite(path=sidecar, content=pending, expected_hash=expected)],
            vault_root=vault,
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
    return ReconcileResult(media_type, state, sidecar, job_id)


def reconcile_all_media(
    vault_root: Path,
    *,
    limit: int = DEFAULT_RECONCILE_LIMIT,
) -> int:
    """Reconcile a bounded, pruned pass of supported governed binaries.

    Each artifact is independent: one unreadable or racing file is logged and
    does not prevent later candidates from converging.
    """
    if isinstance(limit, bool) or limit <= 0:
        raise ValueError("media reconciliation limit must be a positive integer")
    vault = Path(vault_root).resolve()
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
                reconcile_media(vault, binary, explicit=False)
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
                if child.is_dir():
                    if not child.is_symlink() and child.name not in VAULT_SCAN_SKIP_DIRS:
                        directories.append(child)
                elif child.is_file() and not child.is_symlink() and classify_media(child):
                    rel = child.relative_to(vault).as_posix()
                    if access.access_tier(vault, rel) not in {
                        access.TIER_EXCLUDED,
                        access.TIER_READONLY,
                    }:
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
        original = sidecar.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
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


def _is_pending_sidecar_shape(
    vault: Path,
    binary: Path,
    frontmatter: dict[str, object],
    media_type: str,
) -> bool:
    captured = frontmatter.get("captured")
    try:
        if isinstance(captured, dt.date):
            captured.isoformat()
        else:
            dt.date.fromisoformat(str(captured))
    except (TypeError, ValueError):
        return False
    tags = frontmatter.get("tags")
    ingested_into = frontmatter.get("ingested_into")
    digest = str(frontmatter.get("binary_sha256", ""))
    expected_path = binary.relative_to(vault).as_posix()
    return (
        frontmatter.get("type") == "source"
        and memory_refs.normalize_id(frontmatter.get("exomem_id")) is not None
        and isinstance(frontmatter.get("title"), str)
        and bool(str(frontmatter["title"]).strip())
        and frontmatter.get("source_type") == "other"
        and frontmatter.get("media_type") == media_type
        and frontmatter.get("evidence_file") == expected_path
        and frontmatter.get("original_filename") == binary.name
        and str(frontmatter.get("extracted_by", "")).strip().lower() == "pending"
        and frontmatter.get("processing_state") == "pending"
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and _is_nonnegative_int(frontmatter.get("binary_size"))
        and _is_nonnegative_int(frontmatter.get("binary_mtime_ns"))
        and _is_nonnegative_int(frontmatter.get("binary_ctime_ns"))
        and isinstance(tags, list)
        and bool(tags)
        and all(isinstance(tag, str) and tag.strip() for tag in tags)
        and isinstance(ingested_into, list)
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
    expected = {
        "evidence_file": binary.relative_to(vault).as_posix(),
        "original_filename": binary.name,
        "binary_size": current.st_size,
        "binary_mtime_ns": current.st_mtime_ns,
        "binary_ctime_ns": current.st_ctime_ns,
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


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def retry_media(vault_root: Path, binary_path: str | Path) -> ReconcileResult:
    """Explicitly retry one blocked/failed artifact without replacing valid output."""
    vault = Path(vault_root).resolve()
    binary = Path(binary_path)
    if not binary.is_absolute():
        binary = vault / binary
    binary = Path(os.path.abspath(binary))

    result = reconcile_media(vault, binary)
    if result.state == "completed" or result.job_id is None:
        return result

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
            result = retry_media(vault, job.binary_path)
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


def _read_provenance(
    vault: Path, binary: Path, resolved_binary: Path
) -> _BinaryProvenance:
    digest = hashlib.sha256()
    with resolved_binary.open("rb") as stream:
        before = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    identity_before = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_after = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if identity_after != identity_before:
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
        current = resolved_binary.stat()
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
    if current_identity != expected_identity:
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
            for field, value in _pending_fields(provenance):
                rendered = preserve._set_frontmatter_field(rendered, field, str(value))
            return rendered
        preserved_notes = body if raw_frontmatter is not None else original

    parts = Path(provenance.relative_path).parts
    evidence_index = next(
        (i for i, part in enumerate(parts) if part.casefold() == "evidence"), None
    )
    folders = parts[evidence_index + 1 : -1] if evidence_index is not None else ()
    scope = folders[0] if folders else "evidence"
    category = folders[1] if len(folders) > 1 else "uncategorized"
    rendered = preserve._render_sidecar(
        artifact_name=binary.name,
        scope=scope,
        category=category,
        date_iso=dt.date.today().isoformat(),
        media_type=media_type,
        evidence_file=provenance.relative_path,
        extracted_by="pending",
    )
    if existing_id is not None:
        rendered = preserve._set_frontmatter_field(rendered, "exomem_id", existing_id)
    for field, value in _pending_fields(provenance):
        rendered = preserve._set_frontmatter_field(rendered, field, str(value))
    if preserved_notes:
        rendered = rendered.rstrip("\n") + "\n\n## Preserved notes\n\n" + preserved_notes
    return rendered


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


def _is_canonical_pending_shape(
    frontmatter: dict[str, object],
    media_type: str,
    provenance: _BinaryProvenance,
) -> bool:
    captured = frontmatter.get("captured")
    try:
        if isinstance(captured, dt.date):
            captured.isoformat()
        else:
            dt.date.fromisoformat(str(captured))
    except (TypeError, ValueError):
        return False
    tags = frontmatter.get("tags")
    ingested_into = frontmatter.get("ingested_into")
    return (
        frontmatter.get("type") == "source"
        and memory_refs.normalize_id(frontmatter.get("exomem_id")) is not None
        and isinstance(frontmatter.get("title"), str)
        and bool(str(frontmatter["title"]).strip())
        and frontmatter.get("source_type") == "other"
        and frontmatter.get("media_type") == media_type
        and frontmatter.get("evidence_file") == provenance.relative_path
        and str(frontmatter.get("extracted_by", "")).strip().lower() == "pending"
        and isinstance(tags, list)
        and bool(tags)
        and all(isinstance(tag, str) and tag.strip() for tag in tags)
        and isinstance(ingested_into, list)
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
    expected = {
        "evidence_file": provenance.relative_path,
        "original_filename": provenance.original_filename,
        "binary_sha256": provenance.sha256,
        "binary_size": provenance.size,
        "binary_mtime_ns": provenance.mtime_ns,
        "binary_ctime_ns": provenance.ctime_ns,
    }
    for field, value in expected.items():
        if field in frontmatter and frontmatter.get(field) != value:
            return "conflict"
    if all(field in frontmatter for field in _PROVENANCE_FIELDS):
        return "valid"
    return "repairable"


def _backfill_completed_provenance(
    content: str, provenance: _BinaryProvenance
) -> str:
    preserve = _preserve_module()
    rendered = content
    existing, _body, _raw = parse_frontmatter(content)
    for field, value in _pending_fields(provenance)[1:]:
        if field not in existing:
            rendered = preserve._set_frontmatter_field(rendered, field, str(value))
    return rendered


def mark_processing_unavailable(
    vault_root: Path,
    *,
    reason: str,
    next_action: str,
) -> int:
    """Make queued automatic work actionable when no runtime can consume it."""
    vault = Path(vault_root).resolve()
    if not media_jobs.job_store_path(vault).exists():
        return 0
    store = media_jobs.MediaJobStore(vault, create=False)
    jobs = store.pending_jobs()
    preserve = _preserve_module()
    changed = 0
    for job in jobs:
        if job.id is None:
            continue
        store.mark(job.id, media_jobs.BLOCKED, reason)
        if job.sidecar_path.exists():
            preserve.update_sidecar_processing_failure(
                vault,
                job.sidecar_path,
                state=media_jobs.BLOCKED,
                attempts=job.attempts,
                error=reason,
                retryable=True,
                next_action=next_action,
            )
        changed += 1
    return changed

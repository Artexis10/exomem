"""Canonical, import-light orchestration for governed media artifacts.

This leaf classifies a binary, converges its Markdown sidecar, and records one
durable media job.  Model-backed extraction remains the worker's responsibility.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import extract, media_jobs, memory_refs, preserve
from .kbdir import kb_dirname
from .vault import (
    MISSING_CONTENT_HASH,
    PlannedWrite,
    batch_atomic_write,
    content_hash,
    parse_frontmatter,
    yaml_scalar,
)


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


def classify_media(path: str | Path) -> str | None:
    """Return the canonical extraction kind for ``path``, case-insensitively."""
    return extract.media_type_for(path)


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

    provenance = _read_provenance(vault, binary, resolved_binary)
    sidecar = binary.with_name(binary.name + ".md")
    _confine_sidecar(vault, sidecar)
    original = sidecar.read_text(encoding="utf-8") if sidecar.exists() else None

    completed = original is not None and _is_valid_completed_sidecar(
        original, media_type=media_type, provenance=provenance
    )
    if completed:
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
    if store.retry(binary_path=binary, include_failed=True) == 0:
        return result
    durable_job = store.get(result.job_id)
    state = durable_job.state if durable_job is not None else media_jobs.PENDING
    return ReconcileResult(result.media_type, state, result.sidecar_path, result.job_id)


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
    frontmatter, body, raw_frontmatter = parse_frontmatter(content)
    if raw_frontmatter is None:
        return False
    engine = str(frontmatter.get("extracted_by", "")).strip()
    if (
        frontmatter.get("type") != "source"
        or frontmatter.get("media_type") != media_type
        or frontmatter.get("processing_state") not in (None, "completed")
        or engine.lower() in _INCOMPLETE_ENGINES
        or engine.lower().startswith("failed")
    ):
        return False
    recorded_hash = frontmatter.get("binary_sha256")
    if recorded_hash is not None and str(recorded_hash) != provenance.sha256:
        return False
    return any(
        bool(match.group(1).strip())
        for match in _EXTRACTED_SECTION_RE.finditer(body)
    )

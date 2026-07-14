"""Private hosted-cell helpers for deterministic vault export and restore.

The public product and storage lifecycle live in the control plane.  This module
owns only the cell-local facts Exomem can prove: a vault is quiesced, an export
matches its manifest, a restore is safe to publish, or a lifecycle checkpoint
has been recorded without performing external deletion.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__

MANIFEST_NAME = "exomem-manifest.json"
MANIFEST_SCHEMA_VERSION = 1
CLASSIFICATION_VERSION = 1
ARCHIVE_FORMAT = "zip"

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_EXPORT_REF_RE = re.compile(r"^exomem-export://sha256/[0-9a-f]{64}$")


class PortabilityError(RuntimeError):
    """Stable, content-free portability failure."""

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


class ArtifactClass(StrEnum):
    """Durability class written into export manifests."""

    CANONICAL = "canonical"
    PORTABLE_DERIVED = "portable-derived"
    DISPOSABLE_RUNTIME = "disposable-runtime"


@dataclass(frozen=True, slots=True)
class ArtifactClassification:
    artifact_class: ArtifactClass
    rule_id: str
    rationale: str


@dataclass(frozen=True, slots=True)
class _ClassificationRule:
    rule_id: str
    artifact_class: ArtifactClass
    rationale: str
    matcher: Callable[[str, tuple[str, ...]], bool]


def _is_review_state(path: str, _parts: tuple[str, ...]) -> bool:
    return path == "Knowledge Base/.review-state.json"


def _is_provider_log(_path: str, parts: tuple[str, ...]) -> bool:
    return bool(parts) and parts[0].casefold() in {"logs", ".logs", "runtime-logs"}


def _is_secret_or_credential(path: str, parts: tuple[str, ...]) -> bool:
    names = {part.casefold() for part in parts}
    basename = parts[-1].casefold() if parts else ""
    if basename == ".env" or basename.startswith(".env."):
        return True
    if basename.endswith((".pem", ".key", ".p12", ".pfx", ".jwk")):
        return True
    if names & {"credentials", ".credentials", "secrets", ".secrets"}:
        return True
    # Treat exact conventional secret filenames as runtime state, but never use
    # a loose substring across the whole path. Authored notes routinely discuss
    # keys and credentials; e.g. ``master-key-rotation.md`` is canonical memory,
    # not a secret merely because its title contains those words.
    secret_stems = {
        "service-credential",
        "oauth-token",
        "session-token",
        "encryption-key",
        "master-key",
    }
    return any(
        basename == stem
        or basename in {f"{stem}.json", f"{stem}.yaml", f"{stem}.yml", f"{stem}.txt"}
        for stem in secret_stems
    )


def _is_temporary_or_lock(_path: str, parts: tuple[str, ...]) -> bool:
    basename = parts[-1].casefold() if parts else ""
    return (
        basename.endswith((".tmp", ".partial", ".lock", ".lck", ".swp", ".bak", "~"))
        or basename.startswith(".~")
        or basename in {"lock", "mutation.guard", ".ds_store", "thumbs.db"}
    )


def _is_generated_media_or_model(_path: str, parts: tuple[str, ...]) -> bool:
    lowered = tuple(part.casefold() for part in parts)
    return (
        any(part.endswith(".frames") for part in lowered)
        or bool(
            set(lowered)
            & {
                ".models",
                "models",
                ".model-cache",
                ".voice-models",
                ".voice-profiles",
            }
        )
        or (bool(lowered) and lowered[-1] in {".voice_profiles.json", ".voice-profiles.json"})
    )


_REBUILDABLE_SQLITE_NAMES = {
    ".embeddings.sqlite",
    ".clip.sqlite",
    ".lexical.sqlite",
    ".graph.sqlite",
    ".claims.sqlite",
    ".references.sqlite",
    ".refs.sqlite",
    ".freshness.sqlite",
    ".deferred-index.sqlite",
    ".deferred_index.sqlite",
    ".media-jobs.sqlite",
    ".media_jobs.sqlite",
    ".idempotency.sqlite",
}


def _is_rebuildable_sidecar(_path: str, parts: tuple[str, ...]) -> bool:
    basename = parts[-1].casefold() if parts else ""
    for name in _REBUILDABLE_SQLITE_NAMES:
        if basename == name or basename in {f"{name}-wal", f"{name}-shm"}:
            return True
    return basename in {
        ".idempotency.json",
        ".idempotency.jsonl",
        ".media-jobs.json",
        ".deferred-index.json",
    }


def _is_hosted_runtime_state(_path: str, parts: tuple[str, ...]) -> bool:
    basename = parts[-1].casefold() if parts else ""
    if basename in {
        ".exomem-hosted-cell.json",
        "hosted-lifecycle-state.json",
        "hosted-security.sqlite",
        "hosted-security.sqlite-wal",
        "hosted-security.sqlite-shm",
        "writer-leases.sqlite",
        "writer-leases.sqlite-wal",
        "writer-leases.sqlite-shm",
    }:
        return True
    if parts and parts[0].casefold() in {
        "hosted-init-operations",
        "restore-journal",
        "tmp",
    }:
        return True
    return basename.startswith("idempotency-") and basename.endswith(
        (".sqlite", ".sqlite-wal", ".sqlite-shm")
    )


def _is_unregistered_hidden_state(_path: str, parts: tuple[str, ...]) -> bool:
    # New machine-local sidecars are conventionally hidden.  Defaulting them to
    # disposable prevents a new cache/database from silently entering exports;
    # portable hidden state must be explicitly registered above.
    return any(part.startswith(".") for part in parts)


def _always_canonical(_path: str, _parts: tuple[str, ...]) -> bool:
    return True


_CLASSIFICATION_RULES = (
    _ClassificationRule(
        "portable-review-state",
        ArtifactClass.PORTABLE_DERIVED,
        "User-visible epistemic review decisions are portable derived state.",
        _is_review_state,
    ),
    _ClassificationRule(
        "provider-operational-logs",
        ArtifactClass.DISPOSABLE_RUNTIME,
        "Provider logs and query records are runtime-private and not vault history.",
        _is_provider_log,
    ),
    _ClassificationRule(
        "secrets-and-credentials",
        ArtifactClass.DISPOSABLE_RUNTIME,
        "Credentials, keys, and session material are never portable vault content.",
        _is_secret_or_credential,
    ),
    _ClassificationRule(
        "temporary-and-lock-files",
        ArtifactClass.DISPOSABLE_RUNTIME,
        "Incomplete writes, backups, and process coordination state are disposable.",
        _is_temporary_or_lock,
    ),
    _ClassificationRule(
        "generated-media-and-model-state",
        ArtifactClass.DISPOSABLE_RUNTIME,
        "Generated frames, models, and voice profiles are machine-local state.",
        _is_generated_media_or_model,
    ),
    _ClassificationRule(
        "rebuildable-index-sidecars",
        ArtifactClass.DISPOSABLE_RUNTIME,
        "Search, graph, media-job, and idempotency indexes rebuild from canonical files.",
        _is_rebuildable_sidecar,
    ),
    _ClassificationRule(
        "hosted-cell-runtime-state",
        ArtifactClass.DISPOSABLE_RUNTIME,
        "Cell binding, writer leases, and request idempotency are runtime control state.",
        _is_hosted_runtime_state,
    ),
    _ClassificationRule(
        "unregistered-hidden-state",
        ArtifactClass.DISPOSABLE_RUNTIME,
        "Unregistered hidden sidecars fail closed until durability is declared.",
        _is_unregistered_hidden_state,
    ),
    _ClassificationRule(
        "owned-vault-payload",
        ArtifactClass.CANONICAL,
        "Authored Markdown, governed history, schema, trash, and original media are canonical.",
        _always_canonical,
    ),
)


def classification_registry() -> dict[str, Any]:
    """Return the stable, versioned durability registry without executable matchers."""

    return {
        "version": CLASSIFICATION_VERSION,
        "rules": [
            {
                "id": rule.rule_id,
                "artifact_class": rule.artifact_class.value,
                "rationale": rule.rationale,
            }
            for rule in _CLASSIFICATION_RULES
        ],
    }


def classify_artifact(path: str | PurePosixPath) -> ArtifactClassification:
    """Classify a normalized vault-relative path using the first matching rule."""

    normalized = _normalized_relative_path(str(path))
    parts = PurePosixPath(normalized).parts
    for rule in _CLASSIFICATION_RULES:
        if rule.matcher(normalized, parts):
            return ArtifactClassification(rule.artifact_class, rule.rule_id, rule.rationale)
    raise AssertionError("classification registry must end with a catch-all rule")


@dataclass(frozen=True, slots=True)
class PortabilityContext:
    cell_id: str
    vault_id: str
    operation_id: str
    created_at: str
    operator_authorized: bool
    lifecycle_state: str
    routing_stopped: bool
    active_mutations: int
    background_writers_stopped: bool
    reads_allowed: bool


@dataclass(frozen=True, slots=True)
class PortabilityLimits:
    """Resource bounds enforced before and while reading an archive."""

    max_files: int = 100_000
    max_total_bytes: int = 10 * 1024 * 1024 * 1024
    max_file_bytes: int = 2 * 1024 * 1024 * 1024
    max_manifest_bytes: int = 16 * 1024 * 1024
    max_path_bytes: int = 4096
    max_compression_ratio: int = 1_000


def _validate_limits(limits: PortabilityLimits) -> None:
    if not isinstance(limits, PortabilityLimits):
        _fail("INVALID_RESOURCE_LIMITS", "portability limits have the wrong type")
    values = (
        limits.max_files,
        limits.max_total_bytes,
        limits.max_file_bytes,
        limits.max_manifest_bytes,
        limits.max_path_bytes,
        limits.max_compression_ratio,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        _fail("INVALID_RESOURCE_LIMITS", "portability limits must be positive integers")


@dataclass(frozen=True, slots=True)
class ExportResult:
    archive_path: Path
    archive_sha256: str
    archive_size: int
    archive_format: str
    artifact_reference: str
    manifest: dict[str, Any]

    @property
    def manifest_sha256(self) -> str:
        return str(self.manifest["overall_digest"]["value"])


@dataclass(frozen=True, slots=True)
class VerifiedArchive:
    archive_path: Path
    archive_sha256: str
    archive_size: int
    archive_format: str
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreparedRestore:
    staging_root: Path
    source_archive: Path
    archive_sha256: str
    manifest: dict[str, Any]
    context: PortabilityContext
    state: str = "prepared"


@dataclass(frozen=True, slots=True)
class PublishedRestore:
    live_root: Path
    state: str
    lexical_ready: bool
    derived_state: str
    derived_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class LifecycleCheckpoint:
    operation: str
    operation_id: str
    cell_id: str
    vault_id: str
    created_at: str
    state: str
    reason_code: str
    checkpoint_digest: str
    replayed: bool
    artifact_reference: str | None = None
    external_deletion_performed: bool = False
    external_deletion_owner: str = "control-plane"

    def audit_record(self) -> dict[str, Any]:
        """Return a deliberately content-minimal lifecycle event."""

        return {
            "event": "hosted-portability-checkpoint",
            "operation": self.operation,
            "operation_id": self.operation_id,
            "cell_id": self.cell_id,
            "state": self.state,
            "timestamp": self.created_at,
            "checkpoint_digest": self.checkpoint_digest,
            "replayed": self.replayed,
            "outcome": "replayed" if self.replayed else "committed",
        }


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    path: str
    source_path: Path
    size: int
    sha256: str
    classification: str
    source_signature: tuple[int, int, int, int, int]


def _fail(code: str, reason: str) -> None:
    raise PortabilityError(code, reason)


def _validate_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        _fail("INVALID_PORTABILITY_CONTEXT", f"{field} is not a valid opaque identifier")


def _validate_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        _fail("INVALID_PORTABILITY_CONTEXT", "created_at must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        _fail("INVALID_PORTABILITY_CONTEXT", "created_at must include a timezone")


def _validate_context(
    context: PortabilityContext,
    *,
    allowed_states: set[str],
    require_routing_stopped: bool = True,
) -> None:
    if not context.operator_authorized:
        _fail("UNAUTHORIZED_PORTABILITY", "private operator authorization is required")
    _validate_identifier(context.cell_id, "cell_id")
    _validate_identifier(context.vault_id, "vault_id")
    _validate_identifier(context.operation_id, "operation_id")
    _validate_timestamp(context.created_at)
    if context.lifecycle_state not in allowed_states:
        _fail("CELL_NOT_QUIESCED", "cell lifecycle does not permit this portability operation")
    if require_routing_stopped and not context.routing_stopped:
        _fail("ROUTING_NOT_STOPPED", "routing must stop before the portability operation")
    if (
        isinstance(context.active_mutations, bool)
        or not isinstance(context.active_mutations, int)
        or context.active_mutations != 0
        or not context.background_writers_stopped
    ):
        _fail("QUIESCENCE_INCOMPLETE", "in-flight or background mutation work remains")
    if not isinstance(context.reads_allowed, bool):
        _fail("INVALID_PORTABILITY_CONTEXT", "read admission must be explicitly reported")


def _normalized_relative_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        _fail("UNSAFE_ARCHIVE_PATH", "path is empty or not a portable POSIX path")
    normalized = unicodedata.normalize("NFC", path)
    candidate = PurePosixPath(normalized)
    if (
        candidate.is_absolute()
        or path.startswith("/")
        or _WINDOWS_DRIVE_RE.match(path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        _fail("UNSAFE_ARCHIVE_PATH", "archive paths must remain beneath the vault root")
    return candidate.as_posix()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _manifest_digest(manifest_without_digest: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(manifest_without_digest))


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError:
        _fail("SOURCE_CHANGED_DURING_EXPORT", "a source entry changed during export")


def _source_signature(source_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
        source_stat.st_ctime_ns,
    )


def _open_regular_source(
    path: Path,
    *,
    expected_signature: tuple[int, int, int, int, int] | None = None,
) -> tuple[int, tuple[int, int, int, int, int]]:
    before = _safe_lstat(path)
    if stat.S_ISLNK(before.st_mode):
        _fail("UNSAFE_SYMLINK", "vault exports never follow symbolic links")
    if not stat.S_ISREG(before.st_mode):
        _fail("UNSAFE_SOURCE_ENTRY", "vault exports accept regular files only")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail("SOURCE_CHANGED_DURING_EXPORT", "a source file changed during export")
    try:
        opened = os.fstat(descriptor)
        if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
            _fail("UNSAFE_SOURCE_ENTRY", "vault exports accept regular files only")
        before_signature = _source_signature(before)
        opened_signature = _source_signature(opened)
        if before_signature != opened_signature or (
            expected_signature is not None and opened_signature != expected_signature
        ):
            _fail("SOURCE_CHANGED_DURING_EXPORT", "a source file changed during export")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened_signature


def _digest_regular_source(
    path: Path,
) -> tuple[int, str, tuple[int, int, int, int, int]]:
    descriptor, signature = _open_regular_source(path)
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        after_signature = _source_signature(os.fstat(descriptor))
    finally:
        os.close(descriptor)
    if signature != after_signature or size != signature[2]:
        _fail("SOURCE_CHANGED_DURING_EXPORT", "a source file changed during export")
    return size, digest.hexdigest(), signature


def _enumerate_source(vault_root: Path, limits: PortabilityLimits) -> list[_SourceSnapshot]:
    snapshots: list[_SourceSnapshot] = []
    total_bytes = 0
    try:
        root_stat = vault_root.lstat()
    except OSError:
        _fail("VAULT_NOT_FOUND", "vault root is unavailable")
    if stat.S_ISLNK(root_stat.st_mode):
        _fail("UNSAFE_SYMLINK", "vault root cannot be a symbolic link")
    if not stat.S_ISDIR(root_stat.st_mode):
        _fail("VAULT_NOT_FOUND", "vault root is not a directory")

    for current, directory_names, file_names in os.walk(
        vault_root, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        directory_names.sort()
        file_names.sort()
        for name in directory_names:
            child = current_path / name
            child_stat = _safe_lstat(child)
            if stat.S_ISLNK(child_stat.st_mode):
                _fail("UNSAFE_SYMLINK", "vault exports never follow symbolic links")
            if not stat.S_ISDIR(child_stat.st_mode):
                _fail("UNSAFE_SOURCE_ENTRY", "vault directories must be ordinary directories")
        for name in file_names:
            source = current_path / name
            relative = source.relative_to(vault_root).as_posix()
            normalized = _normalized_relative_path(relative)
            if normalized != relative:
                _fail("UNSAFE_SOURCE_PATH", "source paths must already be normalized")
            classification = classify_artifact(normalized)
            if classification.artifact_class is ArtifactClass.DISPOSABLE_RUNTIME:
                # Still reject links and unsupported entries in excluded locations.
                source_stat = _safe_lstat(source)
                if stat.S_ISLNK(source_stat.st_mode):
                    _fail("UNSAFE_SYMLINK", "vault exports never follow symbolic links")
                if not stat.S_ISREG(source_stat.st_mode):
                    _fail("UNSAFE_SOURCE_ENTRY", "vault exports accept regular files only")
                continue
            size, digest, source_signature = _digest_regular_source(source)
            if size > limits.max_file_bytes:
                _fail("RESOURCE_LIMIT_EXCEEDED", "a source file exceeds the export limit")
            snapshots.append(
                _SourceSnapshot(
                    path=normalized,
                    source_path=source,
                    size=size,
                    sha256=digest,
                    classification=classification.artifact_class.value,
                    source_signature=source_signature,
                )
            )
            total_bytes += size
            if len(snapshots) > limits.max_files:
                _fail("RESOURCE_LIMIT_EXCEEDED", "the vault exceeds the export file limit")
            if total_bytes > limits.max_total_bytes:
                _fail("RESOURCE_LIMIT_EXCEEDED", "the vault exceeds the export byte limit")
    snapshots.sort(key=lambda snapshot: snapshot.path)
    folded: set[str] = set()
    directory_spellings: dict[str, str] = {}
    for snapshot in snapshots:
        collision_key = unicodedata.normalize("NFC", snapshot.path).casefold()
        if collision_key in folded:
            _fail("CASE_COLLISION", "vault paths collide on a case-insensitive filesystem")
        folded.add(collision_key)
        parts = PurePosixPath(snapshot.path).parts
        for index in range(1, len(parts)):
            spelling = "/".join(parts[:index])
            directory_key = unicodedata.normalize("NFC", spelling).casefold()
            prior = directory_spellings.setdefault(directory_key, spelling)
            if prior != spelling:
                _fail("CASE_COLLISION", "vault directories collide by case")
    return snapshots


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=_ZIP_TIMESTAMP)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.flag_bits |= 0x800
    return info


def _build_manifest(
    snapshots: Iterable[_SourceSnapshot],
    context: PortabilityContext,
    exomem_release: str,
) -> dict[str, Any]:
    records = [
        {
            "path": snapshot.path,
            "size": snapshot.size,
            "sha256": snapshot.sha256,
            "classification": snapshot.classification,
        }
        for snapshot in snapshots
    ]
    base: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "classification_version": CLASSIFICATION_VERSION,
        "archive_format": ARCHIVE_FORMAT,
        "cell_id": context.cell_id,
        "vault_id": context.vault_id,
        "operation_id": context.operation_id,
        "created_at": context.created_at,
        "exomem_release": exomem_release,
        "files": records,
        "signature": {"algorithm": None, "value": None},
    }
    return {
        **base,
        "overall_digest": {"algorithm": "sha256", "value": _manifest_digest(base)},
    }


def _write_export_archive(
    destination: Path,
    snapshots: Iterable[_SourceSnapshot],
    manifest: Mapping[str, Any],
) -> None:
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_STORED, allowZip64=True
    ) as archive:
        archive.writestr(_zip_info(MANIFEST_NAME), _canonical_json(manifest))
        for snapshot in snapshots:
            descriptor, opened_signature = _open_regular_source(
                snapshot.source_path,
                expected_signature=snapshot.source_signature,
            )
            digest = hashlib.sha256()
            size = 0
            info = _zip_info(snapshot.path)
            info.file_size = snapshot.size
            try:
                with archive.open(
                    info,
                    "w",
                    force_zip64=snapshot.size >= 2 * 1024 * 1024 * 1024,
                ) as output:
                    while True:
                        chunk = os.read(descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                after_signature = _source_signature(os.fstat(descriptor))
            finally:
                os.close(descriptor)
            if (
                opened_signature != after_signature
                or size != snapshot.size
                or digest.hexdigest() != snapshot.sha256
            ):
                _fail("SOURCE_CHANGED_DURING_EXPORT", "a source file changed during export")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def export_quiesced_vault(
    vault_root: Path | str,
    artifact_root: Path | str,
    *,
    context: PortabilityContext,
    exomem_release: str | None = None,
    limits: PortabilityLimits | None = None,
    mutation_guard: AbstractContextManager[Any] | None = None,
) -> ExportResult:
    """Create, self-verify, and publish a deterministic canonical vault archive."""

    _validate_context(context, allowed_states={"quiesced"})
    release = __version__ if exomem_release is None else exomem_release
    if not isinstance(release, str) or not release or len(release) > 128:
        _fail("INVALID_RELEASE", "Exomem release must be a short non-empty string")
    effective_limits = limits or PortabilityLimits()
    _validate_limits(effective_limits)
    source_root = Path(vault_root).absolute()
    output_root = Path(artifact_root).absolute()
    source_resolved = source_root.resolve(strict=False)
    output_resolved = output_root.resolve(strict=False)
    if _is_within(output_resolved, source_resolved):
        _fail("ARTIFACT_ROOT_INSIDE_VAULT", "export artifacts must be staged outside the vault")
    if os.path.lexists(output_root):
        output_stat = output_root.lstat()
        if stat.S_ISLNK(output_stat.st_mode) or not stat.S_ISDIR(output_stat.st_mode):
            _fail("UNSAFE_ARTIFACT_ROOT", "artifact root must be a regular directory")

    guard = mutation_guard if mutation_guard is not None else nullcontext()
    with guard:
        snapshots = _enumerate_source(source_root, effective_limits)
        manifest = _build_manifest(snapshots, context, release)
        output_root.mkdir(parents=True, exist_ok=True)
        descriptor, pending_name = tempfile.mkstemp(
            prefix=f".{context.operation_id}.",
            suffix=".zip.partial",
            dir=output_root,
        )
        os.close(descriptor)
        pending = Path(pending_name)
        artifact_ready = False
        try:
            _write_export_archive(pending, snapshots, manifest)
            _fsync_regular_file(pending)

            # Re-read the source after archive construction.  A caller claiming
            # quiescence while bytes or membership change never receives success.
            repeated = _enumerate_source(source_root, effective_limits)
            original_records = [(item.path, item.size, item.sha256) for item in snapshots]
            repeated_records = [(item.path, item.size, item.sha256) for item in repeated]
            if original_records != repeated_records:
                _fail("SOURCE_CHANGED_DURING_EXPORT", "vault contents changed during export")

            verified = verify_export_archive(
                pending,
                expected_cell_id=context.cell_id,
                expected_vault_id=context.vault_id,
                limits=effective_limits,
            )
            final_path = output_root / f"exomem-export-{verified.archive_sha256}.zip"
            try:
                os.link(pending, final_path)
                artifact_ready = True
            except FileExistsError:
                if _hash_file(final_path) != verified.archive_sha256:
                    _fail("ARTIFACT_CONFLICT", "an existing artifact conflicts with this export")
                artifact_ready = True
            except OSError:
                _fail("ARTIFACT_PUBLICATION_FAILED", "verified export could not be published")
        finally:
            pending.unlink(missing_ok=True)
        if artifact_ready:
            _fsync_directory(output_root)

    return ExportResult(
        archive_path=final_path,
        archive_sha256=verified.archive_sha256,
        archive_size=verified.archive_size,
        archive_format=ARCHIVE_FORMAT,
        artifact_reference=f"exomem-export://sha256/{verified.archive_sha256}",
        manifest=verified.manifest,
    )


def _json_no_duplicates(raw: bytes) -> Any:
    def build_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail("INVALID_MANIFEST", "manifest objects cannot contain duplicate keys")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=build_object)
    except PortabilityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("INVALID_MANIFEST", "manifest is not valid UTF-8 JSON")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            _fail("ARCHIVE_UNAVAILABLE", "archive path is not a regular file")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        _fail("ARCHIVE_UNAVAILABLE", "archive cannot be read")
    return digest.hexdigest()


def _fsync_regular_file(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError:
        _fail("ARTIFACT_PUBLICATION_FAILED", "completed artifact could not be made durable")


def _fsync_directory(path: Path) -> None:
    """Best-effort directory-entry durability on platforms that support it."""

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _entry_type_is_safe(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    return kind in {0, stat.S_IFREG}


def _entry_extra_fields_are_safe(info: zipfile.ZipInfo) -> bool:
    """Accept only ZIP64 sizing metadata; reject Unix link/device extensions."""

    offset = 0
    while offset < len(info.extra):
        if offset + 4 > len(info.extra):
            return False
        field_id, field_size = struct.unpack_from("<HH", info.extra, offset)
        offset += 4
        if offset + field_size > len(info.extra) or field_id != 0x0001:
            return False
        offset += field_size
    return True


def _preflight_entries(
    infos: list[zipfile.ZipInfo], limits: PortabilityLimits
) -> dict[str, zipfile.ZipInfo]:
    by_path: dict[str, zipfile.ZipInfo] = {}
    folded: set[str] = set()
    directory_spellings: dict[str, str] = {}
    payload_files = 0
    total_bytes = 0
    for info in infos:
        raw_path = info.filename
        normalized = _normalized_relative_path(raw_path)
        if normalized != raw_path:
            _fail("UNSAFE_ARCHIVE_PATH", "archive entries must use normalized paths")
        if normalized in by_path:
            _fail("DUPLICATE_ARCHIVE_PATH", "archive contains a duplicate path")
        collision_key = unicodedata.normalize("NFC", normalized).casefold()
        parent_parts = PurePosixPath(collision_key).parts
        parent_keys = {"/".join(parent_parts[:index]) for index in range(1, len(parent_parts))}
        if parent_keys & folded or any(
            existing.startswith(f"{collision_key}/") for existing in folded
        ):
            _fail("PREFIX_PATH_COLLISION", "archive file paths collide with a directory prefix")
        if collision_key in folded:
            _fail("CASE_COLLISION", "archive paths collide on a case-insensitive filesystem")
        parts = PurePosixPath(normalized).parts
        for index in range(1, len(parts)):
            spelling = "/".join(parts[:index])
            directory_key = unicodedata.normalize("NFC", spelling).casefold()
            prior = directory_spellings.setdefault(directory_key, spelling)
            if prior != spelling:
                _fail("CASE_COLLISION", "archive directories collide by case")
        folded.add(collision_key)
        by_path[normalized] = info
        if (
            info.is_dir()
            or not _entry_type_is_safe(info)
            or not _entry_extra_fields_are_safe(info)
            or info.flag_bits & 0x1
        ):
            _fail("UNSAFE_ARCHIVE_ENTRY", "archive contains a link or unsupported entry type")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            _fail("UNSAFE_ARCHIVE_ENTRY", "archive uses an unsupported compression method")
        if len(normalized.encode("utf-8")) > limits.max_path_bytes:
            _fail("RESOURCE_LIMIT_EXCEEDED", "an archive path exceeds the configured limit")
        if info.file_size < 0 or info.file_size > limits.max_file_bytes:
            _fail("RESOURCE_LIMIT_EXCEEDED", "an archive entry exceeds the configured limit")
        if info.compress_size == 0:
            ratio = info.file_size if info.file_size else 1
        else:
            ratio = info.file_size / info.compress_size
        if ratio > limits.max_compression_ratio:
            _fail("RESOURCE_LIMIT_EXCEEDED", "an archive entry exceeds the compression limit")
        if normalized == MANIFEST_NAME:
            if info.file_size > limits.max_manifest_bytes:
                _fail("RESOURCE_LIMIT_EXCEEDED", "manifest exceeds the configured limit")
        else:
            payload_files += 1
            total_bytes += info.file_size
            if payload_files > limits.max_files or total_bytes > limits.max_total_bytes:
                _fail("RESOURCE_LIMIT_EXCEEDED", "archive exceeds the configured limits")
    return by_path


def _require_manifest_shape(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        _fail("INVALID_MANIFEST", "manifest root must be an object")
    schema_version = manifest.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != MANIFEST_SCHEMA_VERSION
    ):
        _fail("UNSUPPORTED_MANIFEST_VERSION", "manifest schema version is not supported")
    classification_version = manifest.get("classification_version")
    if (
        isinstance(classification_version, bool)
        or not isinstance(classification_version, int)
        or classification_version != CLASSIFICATION_VERSION
    ):
        _fail(
            "UNSUPPORTED_CLASSIFICATION_VERSION", "classification registry version is not supported"
        )
    required = {
        "archive_format",
        "cell_id",
        "vault_id",
        "operation_id",
        "created_at",
        "exomem_release",
        "files",
        "signature",
        "overall_digest",
    }
    if not required.issubset(manifest):
        _fail("INVALID_MANIFEST", "manifest is missing required fields")
    if manifest["archive_format"] != ARCHIVE_FORMAT:
        _fail("UNSUPPORTED_ARCHIVE_FORMAT", "archive format is not supported")
    for field in ("cell_id", "vault_id", "operation_id"):
        _validate_identifier(manifest[field], field)
    _validate_timestamp(manifest["created_at"])
    if not isinstance(manifest["exomem_release"], str) or not manifest["exomem_release"]:
        _fail("INVALID_MANIFEST", "manifest release is invalid")
    signature = manifest["signature"]
    if not isinstance(signature, dict) or set(signature) != {"algorithm", "value"}:
        _fail("INVALID_MANIFEST", "manifest signature metadata is invalid")
    if signature["algorithm"] is not None or signature["value"] is not None:
        _fail(
            "UNSUPPORTED_MANIFEST_SIGNATURE", "signed manifests are not supported by this version"
        )
    overall = manifest["overall_digest"]
    if (
        not isinstance(overall, dict)
        or overall.get("algorithm") != "sha256"
        or not isinstance(overall.get("value"), str)
        or not _SHA256_RE.fullmatch(overall["value"])
    ):
        _fail("INVALID_MANIFEST", "manifest overall digest is invalid")
    digest_payload = dict(manifest)
    digest_payload.pop("overall_digest")
    if _manifest_digest(digest_payload) != overall["value"]:
        _fail("MANIFEST_DIGEST_MISMATCH", "manifest integrity digest does not match")
    if not isinstance(manifest["files"], list):
        _fail("INVALID_MANIFEST", "manifest files must be a list")
    return manifest


def _validate_manifest_records(
    manifest: Mapping[str, Any], entries: Mapping[str, zipfile.ZipInfo]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    folded: set[str] = set()
    for raw_record in manifest["files"]:
        if not isinstance(raw_record, dict) or set(raw_record) != {
            "path",
            "size",
            "sha256",
            "classification",
        }:
            _fail("INVALID_MANIFEST", "manifest file record is invalid")
        raw_path = raw_record["path"]
        if not isinstance(raw_path, str):
            _fail("INVALID_MANIFEST", "manifest file path is invalid")
        path = _normalized_relative_path(raw_path)
        if path != raw_path or path == MANIFEST_NAME:
            _fail("UNSAFE_ARCHIVE_PATH", "manifest contains an unsafe file path")
        if path in seen:
            _fail("DUPLICATE_ARCHIVE_PATH", "manifest contains a duplicate file path")
        collision_key = unicodedata.normalize("NFC", path).casefold()
        if collision_key in folded:
            _fail("CASE_COLLISION", "manifest paths collide on a case-insensitive filesystem")
        seen.add(path)
        folded.add(collision_key)
        size = raw_record["size"]
        digest = raw_record["sha256"]
        classification = raw_record["classification"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            _fail("INVALID_MANIFEST", "manifest file size is invalid")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            _fail("INVALID_MANIFEST", "manifest file digest is invalid")
        expected_class = classify_artifact(path).artifact_class.value
        if expected_class == ArtifactClass.DISPOSABLE_RUNTIME.value:
            _fail("DISALLOWED_ARTIFACT", "archive contains disposable runtime state")
        if classification != expected_class:
            _fail("ARTIFACT_CLASS_MISMATCH", "manifest artifact class does not match policy")
        info = entries.get(path)
        if info is None:
            _fail("ARCHIVE_ENTRY_MISSING", "a manifest file is missing from the archive")
        if info.file_size != size:
            _fail("ARCHIVE_SIZE_MISMATCH", "an archive entry has the wrong byte size")
        records.append(dict(raw_record))
    if [record["path"] for record in records] != sorted(record["path"] for record in records):
        _fail("INVALID_MANIFEST", "manifest file records must be sorted")
    archive_payload_paths = set(entries) - {MANIFEST_NAME}
    if archive_payload_paths != seen:
        _fail("UNMANIFESTED_ARCHIVE_ENTRY", "archive contains a file absent from its manifest")
    return records


def _digest_zip_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with archive.open(info, "r") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except (OSError, RuntimeError, zipfile.BadZipFile):
        _fail("INVALID_ARCHIVE", "archive entry cannot be read safely")
    return size, digest.hexdigest()


def verify_export_archive(
    archive_path: Path | str,
    *,
    expected_cell_id: str | None = None,
    expected_vault_id: str | None = None,
    limits: PortabilityLimits | None = None,
) -> VerifiedArchive:
    """Verify archive structure, manifest policy, and every payload byte."""

    path = Path(archive_path).absolute()
    effective_limits = limits or PortabilityLimits()
    _validate_limits(effective_limits)
    try:
        archive_stat = path.lstat()
        if stat.S_ISLNK(archive_stat.st_mode) or not stat.S_ISREG(archive_stat.st_mode):
            _fail("INVALID_ARCHIVE", "archive path must be a regular file")
        archive_size = archive_stat.st_size
        with zipfile.ZipFile(path, "r") as archive:
            if archive.comment:
                _fail("UNSAFE_ARCHIVE_ENTRY", "archive comments are not part of the export format")
            entries = _preflight_entries(archive.infolist(), effective_limits)
            manifest_info = entries.get(MANIFEST_NAME)
            if manifest_info is None:
                _fail("MANIFEST_MISSING", "archive does not contain a manifest")
            try:
                manifest_raw = archive.read(manifest_info)
            except (OSError, RuntimeError, zipfile.BadZipFile):
                _fail("INVALID_ARCHIVE", "manifest cannot be read safely")
            manifest = _require_manifest_shape(_json_no_duplicates(manifest_raw))
            records = _validate_manifest_records(manifest, entries)
            if expected_cell_id is not None and manifest["cell_id"] != expected_cell_id:
                _fail("CELL_BINDING_MISMATCH", "archive does not belong to the expected cell")
            if expected_vault_id is not None and manifest["vault_id"] != expected_vault_id:
                _fail("VAULT_BINDING_MISMATCH", "archive does not belong to the expected vault")
            for record in records:
                actual_size, actual_digest = _digest_zip_entry(archive, entries[record["path"]])
                if actual_size != record["size"]:
                    _fail("ARCHIVE_SIZE_MISMATCH", "an archive entry has the wrong byte size")
                if actual_digest != record["sha256"]:
                    _fail("ARCHIVE_DIGEST_MISMATCH", "an archive entry has the wrong digest")
    except PortabilityError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        _fail("INVALID_ARCHIVE", "archive is unavailable or malformed")

    return VerifiedArchive(
        archive_path=path,
        archive_sha256=_hash_file(path),
        archive_size=archive_size,
        archive_format=ARCHIVE_FORMAT,
        manifest=manifest,
    )


def _verify_required_scaffold(records: Iterable[Mapping[str, Any]]) -> None:
    paths = {str(record["path"]) for record in records}
    if not any(path.startswith("Knowledge Base/") for path in paths) or not any(
        path.startswith("Knowledge Base/_Schema/") for path in paths
    ):
        _fail("INVALID_VAULT_STRUCTURE", "archive does not contain the required vault scaffold")


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _extract_verified_archive(
    archive_path: Path, records: Iterable[Mapping[str, Any]], destination: Path
) -> None:
    with zipfile.ZipFile(archive_path, "r") as archive:
        for record in records:
            relative = str(record["path"])
            target = destination.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            try:
                with archive.open(relative, "r") as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            except (OSError, RuntimeError, zipfile.BadZipFile):
                _fail("RESTORE_EXTRACTION_FAILED", "verified archive could not be extracted")
            if size != record["size"]:
                _fail("ARCHIVE_SIZE_MISMATCH", "restored file has the wrong byte size")
            if digest.hexdigest() != record["sha256"]:
                _fail("ARCHIVE_DIGEST_MISMATCH", "restored file has the wrong digest")


def _walk_regular_files(root: Path) -> set[str]:
    found: set[str] = set()
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directory_names:
            child = current_path / name
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISDIR(child_stat.st_mode):
                _fail("UNSAFE_STAGING_ENTRY", "staging root contains an unsafe entry")
        for name in file_names:
            child = current_path / name
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISREG(child_stat.st_mode):
                _fail("UNSAFE_STAGING_ENTRY", "staging root contains an unsafe entry")
            found.add(child.relative_to(root).as_posix())
    return found


def _verify_staged_files(
    root: Path, manifest: Mapping[str, Any], *, allow_derived_extras: bool = False
) -> None:
    try:
        root_stat = root.lstat()
    except OSError:
        _fail("STAGING_ROOT_UNAVAILABLE", "staging root is unavailable")
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        _fail("UNSAFE_STAGING_ENTRY", "staging root is not a regular directory")
    expected = {str(record["path"]): record for record in manifest["files"]}
    found = _walk_regular_files(root)
    missing = set(expected) - found
    if missing:
        _fail("STAGING_DIGEST_MISMATCH", "staging root is missing canonical files")
    extras = found - set(expected)
    if extras and not allow_derived_extras:
        _fail("UNMANIFESTED_STAGING_ENTRY", "staging root contains an unmanifested file")
    if allow_derived_extras:
        for extra in extras:
            if classify_artifact(extra).artifact_class is not ArtifactClass.DISPOSABLE_RUNTIME:
                _fail(
                    "UNMANIFESTED_STAGING_ENTRY", "published vault contains unknown canonical data"
                )
    for path, record in expected.items():
        target = root.joinpath(*PurePosixPath(path).parts)
        if target.stat().st_size != record["size"] or _hash_file(target) != record["sha256"]:
            _fail("STAGING_DIGEST_MISMATCH", "canonical staged bytes do not match the manifest")


def prepare_restore(
    archive_path: Path | str,
    staging_root: Path | str,
    *,
    context: PortabilityContext,
    limits: PortabilityLimits | None = None,
    expected_source_cell_id: str | None = None,
    expected_source_vault_id: str | None = None,
) -> PreparedRestore:
    """Fully verify and extract an export, then atomically mark it publishable."""

    _validate_context(context, allowed_states={"restore-staging"})
    if expected_source_cell_id is not None:
        _validate_identifier(expected_source_cell_id, "expected_source_cell_id")
    if expected_source_vault_id is not None:
        _validate_identifier(expected_source_vault_id, "expected_source_vault_id")
    verified = verify_export_archive(
        archive_path,
        expected_cell_id=expected_source_cell_id,
        expected_vault_id=expected_source_vault_id or context.vault_id,
        limits=limits,
    )
    destination = Path(staging_root).absolute()
    if os.path.lexists(destination):
        _fail("STAGING_ROOT_EXISTS", "restore staging root must be new")
    records = verified.manifest["files"]
    _verify_required_scaffold(records)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        _extract_verified_archive(verified.archive_path, records, temporary)
        _verify_staged_files(temporary, verified.manifest)
        if os.path.lexists(destination):
            _fail("STAGING_ROOT_EXISTS", "restore staging root was claimed concurrently")
        os.rename(temporary, destination)
    except BaseException:
        _remove_tree(temporary)
        raise
    return PreparedRestore(
        staging_root=destination,
        source_archive=verified.archive_path,
        archive_sha256=verified.archive_sha256,
        manifest=verified.manifest,
        context=context,
    )


def _rollback_failed_publication(staging: Path, live: Path) -> None:
    if os.path.lexists(live):
        if not os.path.lexists(staging):
            try:
                os.replace(live, staging)
                return
            except OSError:
                pass
        _remove_tree(live)


def _safe_restore_parent(root: Path, relative: str) -> Path:
    current = root
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        if os.path.lexists(current):
            current_stat = current.lstat()
            if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
                _fail("CANONICAL_INTEGRITY_VIOLATION", "canonical parent path is unsafe")
        else:
            current.mkdir()
    return current


def _repair_canonical_from_archive(prepared: PreparedRestore, live: Path) -> None:
    """Restore manifest-owned bytes after an unsafe derived-index callback."""

    verified = verify_export_archive(
        prepared.source_archive,
        expected_vault_id=prepared.manifest["vault_id"],
    )
    if (
        verified.archive_sha256 != prepared.archive_sha256
        or verified.manifest["overall_digest"] != prepared.manifest["overall_digest"]
    ):
        _fail("CANONICAL_INTEGRITY_VIOLATION", "restore source changed before repair")
    expected = {str(record["path"]): record for record in prepared.manifest["files"]}
    found = _walk_regular_files(live)
    for extra in found - set(expected):
        if classify_artifact(extra).artifact_class is not ArtifactClass.DISPOSABLE_RUNTIME:
            live.joinpath(*PurePosixPath(extra).parts).unlink()

    with zipfile.ZipFile(prepared.source_archive, "r") as archive:
        for relative, record in expected.items():
            parent = _safe_restore_parent(live, relative)
            target = parent / PurePosixPath(relative).name
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".repair",
                dir=parent,
            )
            temporary = Path(temporary_name)
            digest = hashlib.sha256()
            size = 0
            try:
                with os.fdopen(descriptor, "wb") as output, archive.open(relative, "r") as source:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if size != record["size"] or digest.hexdigest() != record["sha256"]:
                    _fail("CANONICAL_INTEGRITY_VIOLATION", "repair source bytes are invalid")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    _verify_staged_files(live, prepared.manifest, allow_derived_extras=True)


def publish_prepared_restore(
    prepared: PreparedRestore,
    live_root: Path | str,
    *,
    publish: Callable[[Path, Path], None] | None = None,
    rebuild_derived: Callable[[Path], None] | None = None,
) -> PublishedRestore:
    """Atomically publish a prepared vault, then soft-fail optional index rebuilds."""

    _validate_context(prepared.context, allowed_states={"restore-staging"})
    staging = prepared.staging_root
    live = Path(live_root).absolute()
    if os.path.lexists(live):
        _fail("LIVE_VAULT_EXISTS", "restore publication never overlays a live vault")
    if not staging.is_dir() or staging.is_symlink():
        _fail("STAGING_ROOT_UNAVAILABLE", "prepared restore root is unavailable")
    _verify_staged_files(staging, prepared.manifest)
    live.parent.mkdir(parents=True, exist_ok=True)
    publisher = publish or os.replace
    try:
        publisher(staging, live)
        if not live.is_dir() or live.is_symlink():
            _fail("PUBLICATION_FAILED", "publication did not produce a live vault")
        _verify_staged_files(live, prepared.manifest)
        if os.path.lexists(staging):
            _remove_tree(staging)
    except BaseException as exc:
        _rollback_failed_publication(staging, live)
        if isinstance(exc, PortabilityError) and exc.code == "PUBLICATION_FAILED":
            raise
        _fail("PUBLICATION_FAILED", "prepared restore could not be published atomically")

    if rebuild_derived is None:
        return PublishedRestore(live, "published", True, "pending")
    rebuild_failed = False
    try:
        rebuild_derived(live)
    except Exception:  # noqa: BLE001 - optional rebuild failures degrade after integrity checks
        rebuild_failed = True
    try:
        _verify_staged_files(live, prepared.manifest, allow_derived_extras=True)
    except PortabilityError:
        try:
            _repair_canonical_from_archive(prepared, live)
        except Exception as repair_error:
            raise PortabilityError(
                "CANONICAL_INTEGRITY_VIOLATION",
                "derived rebuild changed canonical bytes and repair could not be verified",
            ) from repair_error
        _fail(
            "CANONICAL_INTEGRITY_VIOLATION",
            "derived rebuild changed canonical bytes; original bytes were restored",
        )
    if rebuild_failed:
        return PublishedRestore(
            live,
            "published",
            True,
            "degraded",
            derived_error_code="DERIVED_REBUILD_FAILED",
        )
    return PublishedRestore(live, "published", True, "ready")


class LifecycleCheckpointStore:
    """Filesystem-backed idempotent cell-side lifecycle checkpoints."""

    def __init__(self, state_root: Path | str) -> None:
        self.state_root = Path(state_root)

    def pending_path(self, operation: str, cell_id: str, operation_id: str) -> Path:
        _validate_identifier(cell_id, "cell_id")
        _validate_identifier(operation_id, "operation_id")
        if operation not in {"release-export", "seal-deletion"}:
            _fail("INVALID_CHECKPOINT_OPERATION", "checkpoint operation is not supported")
        return self.state_root / operation / cell_id / f"{operation_id}.json.partial"

    def _final_path(self, operation: str, cell_id: str, operation_id: str) -> Path:
        pending = self.pending_path(operation, cell_id, operation_id)
        return pending.with_suffix("")

    def _adopt_existing(
        self,
        final: Path,
        payload: Mapping[str, Any],
        digest: str,
    ) -> LifecycleCheckpoint:
        existing = self._read_checkpoint(final)
        existing_payload = dict(existing)
        existing_digest = existing_payload.pop("checkpoint_digest", None)
        if existing_digest != _sha256_bytes(_canonical_json(existing_payload)):
            _fail("CHECKPOINT_CORRUPT", "stored lifecycle checkpoint is invalid")
        if existing_payload != payload or existing_digest != digest:
            _fail("CHECKPOINT_CONFLICT", "operation identity is bound to another checkpoint")
        return self._checkpoint_from_record(existing, replayed=True)

    def _commit(self, payload: dict[str, Any]) -> LifecycleCheckpoint:
        operation = str(payload["operation"])
        cell_id = str(payload["cell_id"])
        operation_id = str(payload["operation_id"])
        final = self._final_path(operation, cell_id, operation_id)
        digest = _sha256_bytes(_canonical_json(payload))
        persisted = {**payload, "checkpoint_digest": digest}
        if os.path.lexists(final):
            replay = self._adopt_existing(final, payload, digest)
            _fsync_directory(final.parent)
            return replay
        final.parent.mkdir(parents=True, exist_ok=True)
        descriptor: int | None = None
        pending: Path | None = None
        replay: LifecycleCheckpoint | None = None
        try:
            descriptor, pending_name = tempfile.mkstemp(
                prefix=f".{operation_id}.",
                suffix=".json.partial",
                dir=final.parent,
            )
            pending = Path(pending_name)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(_canonical_json(persisted))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(pending, final)
            except FileExistsError:
                replay = self._adopt_existing(final, payload, digest)
        except PortabilityError:
            raise
        except OSError:
            _fail("CHECKPOINT_WRITE_FAILED", "lifecycle checkpoint could not be persisted")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if pending is not None:
                pending.unlink(missing_ok=True)
        _fsync_directory(final.parent)
        if replay is not None:
            return replay
        return self._checkpoint_from_record(persisted, replayed=False)

    @staticmethod
    def _read_checkpoint(path: Path) -> dict[str, Any]:
        try:
            stored_stat = path.lstat()
            if not stat.S_ISREG(stored_stat.st_mode) or stat.S_ISLNK(stored_stat.st_mode):
                _fail("CHECKPOINT_CORRUPT", "stored lifecycle checkpoint is not a regular file")
            value = _json_no_duplicates(path.read_bytes())
        except OSError:
            _fail("CHECKPOINT_CORRUPT", "stored lifecycle checkpoint cannot be read")
        if not isinstance(value, dict):
            _fail("CHECKPOINT_CORRUPT", "stored lifecycle checkpoint is invalid")
        return value

    @staticmethod
    def _checkpoint_from_record(
        record: Mapping[str, Any], *, replayed: bool
    ) -> LifecycleCheckpoint:
        return LifecycleCheckpoint(
            operation=str(record["operation"]),
            operation_id=str(record["operation_id"]),
            cell_id=str(record["cell_id"]),
            vault_id=str(record["vault_id"]),
            created_at=str(record["created_at"]),
            state=str(record["state"]),
            reason_code=str(record["reason_code"]),
            checkpoint_digest=str(record["checkpoint_digest"]),
            replayed=replayed,
            artifact_reference=record.get("artifact_reference"),
            external_deletion_performed=bool(record.get("external_deletion_performed", False)),
            external_deletion_owner=str(record.get("external_deletion_owner", "control-plane")),
        )

    def release_export(
        self,
        *,
        context: PortabilityContext,
        artifact_reference: str,
        reason_code: str,
        export_root: Path | str,
    ) -> LifecycleCheckpoint:
        """Checkpoint release, then remove the corresponding cell-local artifact."""

        if not isinstance(artifact_reference, str) or not _EXPORT_REF_RE.fullmatch(
            artifact_reference
        ):
            _fail("INVALID_ARTIFACT_REFERENCE", "artifact reference must be opaque and internal")
        if not isinstance(reason_code, str) or not _IDENTIFIER_RE.fullmatch(reason_code):
            _fail("INVALID_REASON_CODE", "lifecycle reason code is invalid")
        if not context.operator_authorized:
            _fail("UNAUTHORIZED_PORTABILITY", "private operator authorization is required")
        _validate_identifier(context.cell_id, "cell_id")
        _validate_identifier(context.vault_id, "vault_id")
        _validate_identifier(context.operation_id, "operation_id")
        _validate_timestamp(context.created_at)
        payload = {
            "operation": "release-export",
            "operation_id": context.operation_id,
            "cell_id": context.cell_id,
            "vault_id": context.vault_id,
            "created_at": context.created_at,
            "state": "export-released",
            "reason_code": reason_code,
            "artifact_reference": artifact_reference,
        }
        # The checkpoint is persisted before the route resumes the cell. A lost
        # HTTP acknowledgement therefore replays after background writers have
        # restarted. Adopt the exact existing checkpoint before enforcing the
        # first-write quiescence proof; conflicts still fail closed in _commit.
        final = self._final_path("release-export", context.cell_id, context.operation_id)
        if os.path.lexists(final):
            checkpoint = self._commit(payload)
        else:
            _validate_context(
                context,
                allowed_states={"quiesced", "export-prepared", "export-failed"},
            )
            checkpoint = self._commit(payload)

        digest = artifact_reference.rsplit("/", 1)[-1]
        root = Path(export_root).absolute()
        artifact = root / f"exomem-export-{digest}.zip"
        try:
            try:
                artifact_stat = artifact.lstat()
            except FileNotFoundError:
                # Release is idempotent. Another replay may remove the exact
                # artifact between our existence check and lstat.
                artifact_stat = None
            if artifact_stat is not None:
                if stat.S_ISLNK(artifact_stat.st_mode) or not stat.S_ISREG(artifact_stat.st_mode):
                    _fail(
                        "EXPORT_RELEASE_CLEANUP_FAILED",
                        "released export artifact is not a regular file",
                    )
            artifact.unlink(missing_ok=True)
            _fsync_directory(root)
        except PortabilityError:
            raise
        except OSError:
            _fail(
                "EXPORT_RELEASE_CLEANUP_FAILED",
                "released export artifact could not be removed",
            )
        return checkpoint

    def seal_for_deletion(
        self,
        *,
        context: PortabilityContext,
        reason_code: str,
    ) -> LifecycleCheckpoint:
        """Seal the cell locally without claiming any external destruction."""

        _validate_context(context, allowed_states={"quiesced", "deletion-quiesced"})
        if not isinstance(reason_code, str) or not _IDENTIFIER_RE.fullmatch(reason_code):
            _fail("INVALID_REASON_CODE", "lifecycle reason code is invalid")
        return self._commit(
            {
                "operation": "seal-deletion",
                "operation_id": context.operation_id,
                "cell_id": context.cell_id,
                "vault_id": context.vault_id,
                "created_at": context.created_at,
                "state": "deletion-sealed",
                "reason_code": reason_code,
                "external_deletion_performed": False,
                "external_deletion_owner": "control-plane",
            }
        )


__all__ = [
    "ARCHIVE_FORMAT",
    "CLASSIFICATION_VERSION",
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "ArtifactClass",
    "ArtifactClassification",
    "ExportResult",
    "LifecycleCheckpoint",
    "LifecycleCheckpointStore",
    "PortabilityContext",
    "PortabilityError",
    "PortabilityLimits",
    "PreparedRestore",
    "PublishedRestore",
    "VerifiedArchive",
    "classification_registry",
    "classify_artifact",
    "export_quiesced_vault",
    "prepare_restore",
    "publish_prepared_restore",
    "verify_export_archive",
]

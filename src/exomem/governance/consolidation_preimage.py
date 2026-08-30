"""Complete private destination preimages for governed vault consolidation.

The manifest is published only after every approved canonical byte has been
copied into independent content-addressed storage and the live destination has
revalidated against the plan-bound snapshot.  Run, receipt, seal, and journal
state remain outside the byte census; their semantic bindings are explicit
manifest fields instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

from .. import hosted_portability
from ..kbdir import kb_dirname
from . import consolidation_fingerprints, consolidation_intake
from .projections import ProjectionCanonicalizationError, canonical_jcs

DESTINATION_PREIMAGE_SCHEMA = "exomem.consolidation-destination-preimage/v1"

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_ID = re.compile(r"[0-9a-f]{64}:committed\Z")
_OBJECT_REF = re.compile(r"exomem-consolidation-object://sha256/([0-9a-f]{64})\Z")
_PREIMAGE_REF = re.compile(r"exomem-consolidation-preimage://sha256/([0-9a-f]{64})\Z")
_MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "operation_id",
        "plan_digest",
        "control_basis_digest",
        "semantic_predecessor_event_id",
        "semantic_predecessor_digest",
        "destination_snapshot_fingerprint",
        "destination_census_digest",
        "entry_count",
        "total_bytes",
        "entries",
    }
)
_ENTRY_FIELDS = frozenset({"path", "size", "sha256", "artifact_ref"})

__all__ = [
    "DESTINATION_PREIMAGE_SCHEMA",
    "ConsolidationPreimageUnavailable",
    "DestinationPreimage",
    "DestinationPreimageBinding",
    "DestinationPreimageEntry",
    "DestinationPreimageLimits",
    "materialize_local_destination_preimage",
    "verify_destination_preimage",
]


class ConsolidationPreimageUnavailable(RuntimeError):
    """Stable, content-free refusal for incomplete or stale preimage state."""

    code = "CONSOLIDATION_PREIMAGE_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("consolidation preimage is unavailable")


@dataclass(frozen=True, slots=True)
class DestinationPreimageBinding:
    run_id: str
    operation_id: str
    plan_digest: str
    control_basis_digest: str
    semantic_predecessor_event_id: str
    semantic_predecessor_digest: str
    destination_snapshot_fingerprint: str
    destination_census_digest: str


@dataclass(frozen=True, slots=True)
class DestinationPreimageLimits:
    max_files: int = 100_000
    max_total_bytes: int = _MAX_SAFE_INTEGER
    minimum_free_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DestinationPreimageEntry:
    path: str
    size: int
    sha256: str
    artifact_ref: str


@dataclass(frozen=True, slots=True)
class DestinationPreimage:
    schema: str
    binding: DestinationPreimageBinding
    destination_census_digest: str
    entry_count: int
    total_bytes: int
    entries: tuple[DestinationPreimageEntry, ...]
    manifest_digest: str
    manifest_ref: str


@dataclass(frozen=True, slots=True)
class _MaterialSource:
    entry: consolidation_fingerprints.CanonicalCensusEntry
    source_path: Path | None
    source_signature: tuple[int, int, int, int, int, int] | None
    content: bytes | None


def _fail() -> NoReturn:
    raise ConsolidationPreimageUnavailable from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _uuid4(value: object) -> str:
    if not isinstance(value, str) or value != value.lower():
        _fail()
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        _fail()
    if parsed.version != 4 or str(parsed) != value:
        _fail()
    return value


def _integer(value: object, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > _MAX_SAFE_INTEGER
    ):
        _fail()
    return value


def _path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        _fail()
    normalized = unicodedata.normalize("NFC", value)
    parsed = PurePosixPath(value)
    if (
        normalized != value
        or parsed.is_absolute()
        or parsed.as_posix() != value
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        _fail()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail()
    return value


def _binding(value: object) -> DestinationPreimageBinding:
    if not isinstance(value, DestinationPreimageBinding):
        _fail()
    return DestinationPreimageBinding(
        run_id=_uuid4(value.run_id),
        operation_id=_uuid4(value.operation_id),
        plan_digest=_digest(value.plan_digest),
        control_basis_digest=_digest(value.control_basis_digest),
        semantic_predecessor_event_id=(
            value.semantic_predecessor_event_id
            if isinstance(value.semantic_predecessor_event_id, str)
            and _EVENT_ID.fullmatch(value.semantic_predecessor_event_id) is not None
            else _fail()
        ),
        semantic_predecessor_digest=_digest(value.semantic_predecessor_digest),
        destination_snapshot_fingerprint=_digest(
            value.destination_snapshot_fingerprint
        ),
        destination_census_digest=_digest(value.destination_census_digest),
    )


def _limits(value: object) -> DestinationPreimageLimits:
    if not isinstance(value, DestinationPreimageLimits):
        _fail()
    return DestinationPreimageLimits(
        max_files=_integer(value.max_files, minimum=1),
        max_total_bytes=_integer(value.max_total_bytes),
        minimum_free_bytes=_integer(value.minimum_free_bytes),
    )


def _binding_dict(value: DestinationPreimageBinding) -> dict[str, str]:
    return {
        "run_id": value.run_id,
        "operation_id": value.operation_id,
        "plan_digest": value.plan_digest,
        "control_basis_digest": value.control_basis_digest,
        "semantic_predecessor_event_id": value.semantic_predecessor_event_id,
        "semantic_predecessor_digest": value.semantic_predecessor_digest,
        "destination_snapshot_fingerprint": value.destination_snapshot_fingerprint,
        "destination_census_digest": value.destination_census_digest,
    }


def _entry_dict(value: DestinationPreimageEntry) -> dict[str, str | int]:
    return {
        "path": value.path,
        "size": value.size,
        "sha256": value.sha256,
        "artifact_ref": value.artifact_ref,
    }


def _canonical(value: object) -> bytes:
    try:
        return canonical_jcs(value)
    except ProjectionCanonicalizationError:
        _fail()


def _manifest_value(
    binding: DestinationPreimageBinding,
    entries: tuple[DestinationPreimageEntry, ...],
) -> dict[str, object]:
    total = sum(item.size for item in entries)
    return {
        "schema": DESTINATION_PREIMAGE_SCHEMA,
        **_binding_dict(binding),
        "entry_count": len(entries),
        "total_bytes": total,
        "entries": [_entry_dict(item) for item in entries],
    }


def _available_bytes(path: Path) -> int:
    candidate = Path(path).absolute()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        return shutil.disk_usage(candidate).free
    except OSError:
        _fail()


def _review_state_path() -> str:
    return f"{kb_dirname()}/.review-state.json"


def _policy_path(relative: str) -> str:
    return _path(f"{kb_dirname()}/_Governance/{relative}")


def _sample_material(
    vault_root: Path,
    *,
    now: int,
    portability_limits: hosted_portability.PortabilityLimits | None,
) -> tuple[
    consolidation_fingerprints.DestinationSnapshot,
    consolidation_fingerprints.CanonicalCensus,
    tuple[_MaterialSource, ...],
]:
    effective = portability_limits or hosted_portability.PortabilityLimits()
    hosted_portability._validate_limits(effective)  # noqa: SLF001
    snapshot = consolidation_fingerprints.load_local_destination_snapshot(
        vault_root,
        now=now,
        limits=effective,
    )
    active = consolidation_fingerprints._load_active_policy_snapshot(  # noqa: SLF001
        vault_root,
        now=now,
    )
    snapshots = hosted_portability._enumerate_source(vault_root, effective)  # noqa: SLF001
    materials: list[_MaterialSource] = []
    for item in snapshots:
        if not (
            item.classification == hosted_portability.ArtifactClass.CANONICAL.value
            or item.path == _review_state_path()
        ):
            continue
        materials.append(
            _MaterialSource(
                entry=consolidation_fingerprints.CanonicalCensusEntry(
                    path=item.path,
                    entry_type="file",
                    size=item.size,
                    sha256=item.sha256,
                ),
                source_path=item.source_path,
                source_signature=item.source_signature,
                content=None,
            )
        )
    for relative, content in active.source_documents:
        if not isinstance(relative, str) or not isinstance(content, bytes):
            _fail()
        materials.append(
            _MaterialSource(
                entry=consolidation_fingerprints.CanonicalCensusEntry(
                    path=_policy_path(relative),
                    entry_type="file",
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                ),
                source_path=None,
                source_signature=None,
                content=content,
            )
        )
    census = consolidation_fingerprints.canonical_content_census(
        tuple(item.entry for item in materials)
    )
    by_path = {item.entry.path: item for item in materials}
    if len(by_path) != len(materials) or census.digest != snapshot.canonical_census_digest:
        _fail()
    return snapshot, census, tuple(by_path[item.path] for item in census.entries)


def _copy_live_source(source: _MaterialSource, staged: Path) -> None:
    if source.source_path is None or source.source_signature is None:
        _fail()
    descriptor: int | None = None
    try:
        descriptor, signature = hosted_portability._open_regular_source(  # noqa: SLF001
            source.source_path,
            expected_signature=source.source_signature,
        )
        digest = hashlib.sha256()
        size = 0
        with staged.open("xb") as output:
            while chunk := os.read(descriptor, 1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = hosted_portability._source_signature(os.fstat(descriptor))  # noqa: SLF001
        if (
            after != signature
            or size != source.entry.size
            or digest.hexdigest() != source.entry.sha256
        ):
            _fail()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_material(source: _MaterialSource, staged: Path) -> None:
    if source.content is None:
        _copy_live_source(source, staged)
        return
    try:
        with staged.open("xb") as output:
            output.write(source.content)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        _fail()
    if (
        len(source.content) != source.entry.size
        or hashlib.sha256(source.content).hexdigest() != source.entry.sha256
    ):
        _fail()


def _result_from_value(
    value: Mapping[str, object],
    *,
    manifest_digest: str,
    manifest_ref: str,
) -> DestinationPreimage:
    if set(value) != _MANIFEST_FIELDS or value.get("schema") != DESTINATION_PREIMAGE_SCHEMA:
        _fail()
    binding = _binding(
        DestinationPreimageBinding(
            run_id=value.get("run_id"),  # type: ignore[arg-type]
            operation_id=value.get("operation_id"),  # type: ignore[arg-type]
            plan_digest=value.get("plan_digest"),  # type: ignore[arg-type]
            control_basis_digest=value.get("control_basis_digest"),  # type: ignore[arg-type]
            semantic_predecessor_event_id=value.get("semantic_predecessor_event_id"),  # type: ignore[arg-type]
            semantic_predecessor_digest=value.get("semantic_predecessor_digest"),  # type: ignore[arg-type]
            destination_snapshot_fingerprint=value.get("destination_snapshot_fingerprint"),  # type: ignore[arg-type]
            destination_census_digest=value.get("destination_census_digest"),  # type: ignore[arg-type]
        )
    )
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list):
        _fail()
    entries: list[DestinationPreimageEntry] = []
    seen: set[str] = set()
    folded: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) != _ENTRY_FIELDS:
            _fail()
        path = _path(raw["path"])
        digest = _digest(raw["sha256"])
        size = _integer(raw["size"])
        artifact_ref = raw["artifact_ref"]
        match = _OBJECT_REF.fullmatch(artifact_ref) if isinstance(artifact_ref, str) else None
        if match is None or match.group(1) != digest:
            _fail()
        collision = path.casefold()
        if path in seen or collision in folded:
            _fail()
        seen.add(path)
        folded.add(collision)
        entries.append(
            DestinationPreimageEntry(
                path=path,
                size=size,
                sha256=digest,
                artifact_ref=artifact_ref,
            )
        )
    ordered = tuple(sorted(entries, key=lambda item: item.path))
    if tuple(entries) != ordered:
        _fail()
    count = _integer(value.get("entry_count"))
    total = _integer(value.get("total_bytes"))
    if count != len(ordered) or total != sum(item.size for item in ordered):
        _fail()
    census = consolidation_fingerprints.canonical_content_census(
        tuple(
            consolidation_fingerprints.CanonicalCensusEntry(
                path=item.path,
                entry_type="file",
                size=item.size,
                sha256=item.sha256,
            )
            for item in ordered
        )
    )
    if census.digest != binding.destination_census_digest:
        _fail()
    return DestinationPreimage(
        schema=DESTINATION_PREIMAGE_SCHEMA,
        binding=binding,
        destination_census_digest=binding.destination_census_digest,
        entry_count=count,
        total_bytes=total,
        entries=ordered,
        manifest_digest=_digest(manifest_digest),
        manifest_ref=manifest_ref,
    )


def materialize_local_destination_preimage(
    vault_root: Path | str,
    *,
    binding: DestinationPreimageBinding,
    artifact_store: consolidation_intake.PrivateConsolidationArtifactStore,
    now: int,
    portability_limits: hosted_portability.PortabilityLimits | None = None,
    resource_limits: DestinationPreimageLimits | None = None,
) -> DestinationPreimage:
    """Copy and publish one complete plan-bound destination preimage."""

    checked_binding = _binding(binding)
    checked_limits = _limits(resource_limits or DestinationPreimageLimits())
    if not isinstance(artifact_store, consolidation_intake.PrivateConsolidationArtifactStore):
        _fail()
    root = Path(vault_root)
    try:
        first_snapshot, census, materials = _sample_material(
            root,
            now=now,
            portability_limits=portability_limits,
        )
        if (
            first_snapshot.digest != checked_binding.destination_snapshot_fingerprint
            or census.digest != checked_binding.destination_census_digest
            or len(materials) > checked_limits.max_files
        ):
            _fail()
        total_bytes = sum(item.entry.size for item in materials)
        if total_bytes > checked_limits.max_total_bytes:
            _fail()
        planned_entries = tuple(
            DestinationPreimageEntry(
                path=item.entry.path,
                size=item.entry.size,
                sha256=item.entry.sha256,
                artifact_ref=(
                    "exomem-consolidation-object://sha256/"
                    f"{item.entry.sha256}"
                ),
            )
            for item in materials
        )
        manifest_bytes = _canonical(_manifest_value(checked_binding, planned_entries))
        if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
            _fail()
        required = total_bytes + len(manifest_bytes) + checked_limits.minimum_free_bytes
        if required > _MAX_SAFE_INTEGER or _available_bytes(artifact_store.root) < required:
            _fail()

        artifact_store._ensure()  # noqa: SLF001 - shared private-store boundary
        transaction = Path(tempfile.mkdtemp(prefix=".preimage-build-", dir=artifact_store.root))
        os.chmod(transaction, 0o700)
        try:
            for ordinal, (material, planned) in enumerate(
                zip(materials, planned_entries, strict=True)
            ):
                staged = transaction / f"{ordinal:06d}.object"
                _write_material(material, staged)
                artifact_ref = artifact_store.install_object_file(
                    staged,
                    expected_digest=material.entry.sha256,
                )
                if artifact_ref != planned.artifact_ref:
                    _fail()
                staged.unlink(missing_ok=True)

            final_snapshot, final_census, _final_material = _sample_material(
                root,
                now=now,
                portability_limits=portability_limits,
            )
            if final_snapshot != first_snapshot or final_census != census:
                _fail()
            manifest_ref = artifact_store.install_preimage_bytes(manifest_bytes)
        finally:
            shutil.rmtree(transaction, ignore_errors=True)
        return verify_destination_preimage(
            manifest_ref,
            binding=checked_binding,
            artifact_store=artifact_store,
        )
    except ConsolidationPreimageUnavailable:
        raise
    except (
        consolidation_fingerprints.ConsolidationFingerprintUnavailable,
        consolidation_intake.ConsolidationIntakeUnavailable,
        hosted_portability.PortabilityError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        _fail()


def verify_destination_preimage(
    manifest_ref: str,
    *,
    binding: DestinationPreimageBinding,
    artifact_store: consolidation_intake.PrivateConsolidationArtifactStore,
) -> DestinationPreimage:
    """Re-read one manifest and every object before any publication may begin."""

    checked_binding = _binding(binding)
    if not isinstance(artifact_store, consolidation_intake.PrivateConsolidationArtifactStore):
        _fail()
    match = _PREIMAGE_REF.fullmatch(manifest_ref) if isinstance(manifest_ref, str) else None
    if match is None:
        _fail()
    try:
        manifest_path = artifact_store.resolve_preimage(manifest_ref)
        raw = manifest_path.read_bytes()
        if len(raw) > _MAX_MANIFEST_BYTES or hashlib.sha256(raw).hexdigest() != match.group(1):
            _fail()
        parsed = json.loads(raw)
        if not isinstance(parsed, Mapping) or _canonical(parsed) != raw:
            _fail()
        result = _result_from_value(
            parsed,
            manifest_digest=match.group(1),
            manifest_ref=manifest_ref,
        )
        if result.binding != checked_binding:
            _fail()
        for entry in result.entries:
            object_path = artifact_store.resolve_object(entry.artifact_ref)
            info = object_path.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_size != entry.size
            ):
                _fail()
        return result
    except ConsolidationPreimageUnavailable:
        raise
    except (
        consolidation_fingerprints.ConsolidationFingerprintUnavailable,
        consolidation_intake.ConsolidationIntakeUnavailable,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        _fail()

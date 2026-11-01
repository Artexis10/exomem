"""Immutable governance-companion classification for non-Markdown artifacts."""

from __future__ import annotations

import hashlib
import re
import shutil
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from .. import media_types, originals, reserved_paths, vault
from ..kbdir import kb_dirname

SEMANTIC_KEYS = ("projects", "tags", "types", "classes")
CompanionReason = Literal[
    "artifact_unsafe",
    "companion_unsafe",
    "descriptor_missing",
    "descriptor_invalid",
    "artifact_mismatch",
    "companion_ambiguous",
]

_DATASET_FORMATS = {".csv": "csv", ".tsv": "tsv", ".json": "json"}
_FRAME_PATH_RE = re.compile(
    r"^(?P<parent>.+)\.frames/(?P<leaf>scene-\d{3,}-t(?P<millis>\d+)ms\.jpg)$"
)
_BYTE_PROOFS: ContextVar[tuple[dict[str, reserved_paths.GenericFileCopy], bool] | None] = ContextVar(
    "companion_byte_proofs", default=None
)


@contextmanager
def byte_proof_scope(proofs: dict[str, reserved_paths.GenericFileCopy], *, allow_capture: bool):
    """Reuse request-owned parent bytes while recomputing classification and authority."""
    token = _BYTE_PROOFS.set((proofs, allow_capture))
    try:
        yield
    finally:
        _BYTE_PROOFS.reset(token)


@dataclass(frozen=True, slots=True)
class BoundCompanion:
    """Artifact semantics proven by one exact descriptor and byte snapshot."""

    projects: tuple[str, ...]
    tags: tuple[str, ...]
    types: tuple[str, ...]
    classes: tuple[str, ...]
    identities: tuple[BoundSnapshot, ...] = ()
    stored_originals: tuple[StoredOriginalProof, ...] = ()


@dataclass(frozen=True, slots=True)
class BoundSnapshot:
    """One exact held-file identity and byte digest in the classification."""

    role: str
    path: str
    sha256: str
    size: int
    device: int
    inode: int
    kind: str
    link_count: int


@dataclass(frozen=True, slots=True)
class StoredOriginalProof:
    """An exact stored binding, without pretending that an absent inode exists."""

    role: str
    path: str
    sha256: str
    size: int
    reference: originals.StoredObject
    sidecar_hash: str


@dataclass(slots=True)
class CompanionClassificationError(Exception):
    """Content-free reason a companion cannot classify its artifact."""

    reason: CompanionReason


def _canonical_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or normalized != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CompanionClassificationError("descriptor_invalid")
    return normalized


def _read_artifact(vault_root, rel_path: str) -> reserved_paths.GenericFileSnapshot | originals.OriginalSnapshot:
    try:
        return reserved_paths.read_generic_bytes(vault_root, rel_path)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "MISSING":
            stored = originals.original_state(vault_root, rel_path)
            if stored.state == "evicted":
                return stored
        raise CompanionClassificationError("artifact_unsafe") from error


def _companion_refusal(error: reserved_paths.ReservedPathLeafError) -> CompanionClassificationError:
    reason: CompanionReason = "descriptor_missing" if error.code == "MISSING" else "companion_unsafe"
    return CompanionClassificationError(reason)


def _read_companion(vault_root, rel_path: str) -> reserved_paths.GenericFileSnapshot:
    try:
        return reserved_paths.read_generic_bytes(vault_root, rel_path)
    except reserved_paths.ReservedPathLeafError as error:
        raise _companion_refusal(error) from error


def _semantics(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != set(SEMANTIC_KEYS):
        raise CompanionClassificationError("descriptor_invalid")
    result: dict[str, tuple[str, ...]] = {}
    for key in SEMANTIC_KEYS:
        raw = value[key]
        if not isinstance(raw, list) or any(
            not isinstance(item, str) or not item for item in raw
        ):
            raise CompanionClassificationError("descriptor_invalid")
        result[key] = tuple(raw)
    return result


def _descriptor(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
        frontmatter, _body, marker = vault.parse_frontmatter(text, strict=True)
    except (UnicodeDecodeError, vault.FrontmatterError) as error:
        raise CompanionClassificationError("descriptor_invalid") from error
    if "stored_object" in frontmatter:
        reference = originals.stored_reference(frontmatter["stored_object"])
        if reference is None or originals.binary_binding(frontmatter) is None:
            raise CompanionClassificationError("descriptor_invalid")
    descriptor = frontmatter.get("governance_companion")
    if descriptor is None:
        raise CompanionClassificationError("descriptor_missing")
    if not isinstance(descriptor, dict) or marker is None:
        raise CompanionClassificationError("descriptor_invalid")
    return descriptor


def _validate_common(
    descriptor: dict[str, object],
    *,
    expected_keys: set[str],
    expected_class: str,
    artifact_path: str,
    artifact_sha256: str,
    artifact_size: int,
) -> BoundCompanion:
    if set(descriptor) != {
        "version",
        "state",
        "artifact_class",
        "artifact_path",
        "artifact_sha256",
        "artifact_size",
        "semantics",
        *expected_keys,
    }:
        raise CompanionClassificationError("descriptor_invalid")
    if (
        descriptor.get("version") != 1
        or isinstance(descriptor.get("version"), bool)
        or descriptor.get("state") != "classified"
    ):
        raise CompanionClassificationError("descriptor_invalid")
    if descriptor.get("artifact_class") != expected_class:
        raise CompanionClassificationError("descriptor_invalid")
    if descriptor.get("artifact_path") != artifact_path:
        raise CompanionClassificationError("artifact_mismatch")
    expected_hash = descriptor.get("artifact_sha256")
    expected_size = descriptor.get("artifact_size")
    if (
        not isinstance(expected_hash, str)
        or expected_hash != artifact_sha256
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size != artifact_size
    ):
        raise CompanionClassificationError("artifact_mismatch")
    semantics = _semantics(descriptor.get("semantics"))
    return BoundCompanion(**semantics)



def _digest(snapshot: reserved_paths.GenericFileSnapshot | reserved_paths.GenericFileCopy | originals.OriginalSnapshot) -> str:
    if isinstance(snapshot, originals.OriginalSnapshot):
        assert snapshot.binary_sha256 is not None
        return snapshot.binary_sha256
    return snapshot.sha256 if isinstance(snapshot, reserved_paths.GenericFileCopy) else hashlib.sha256(snapshot.data).hexdigest()


def _size(snapshot: reserved_paths.GenericFileSnapshot | reserved_paths.GenericFileCopy | originals.OriginalSnapshot) -> int:
    if isinstance(snapshot, originals.OriginalSnapshot):
        assert snapshot.binary_size is not None
        return snapshot.binary_size
    return snapshot.size if isinstance(snapshot, reserved_paths.GenericFileCopy) else len(snapshot.data)


def _bind_snapshots(
    companion: BoundCompanion,
    *snapshots: tuple[str, str, reserved_paths.GenericFileSnapshot | reserved_paths.GenericFileCopy | originals.OriginalSnapshot],
) -> BoundCompanion:
    identities = tuple(
        BoundSnapshot(
            role=role,
            path=path,
            sha256=_digest(snapshot),
            size=_size(snapshot),
            device=snapshot.identity.device,
            inode=snapshot.identity.inode,
            kind=snapshot.identity.kind,
            link_count=snapshot.identity.link_count,
        )
        for role, path, snapshot in snapshots
        if not isinstance(snapshot, originals.OriginalSnapshot)
    )
    stored = tuple(
        StoredOriginalProof(role, path, snapshot.binary_sha256, snapshot.binary_size,
                            snapshot.reference, snapshot.sidecar_hash)
        for role, path, snapshot in snapshots
        if isinstance(snapshot, originals.OriginalSnapshot)
    )
    return replace(companion, identities=identities, stored_originals=stored)


def _classify_sibling(
    vault_root,
    artifact_path: str,
    artifact: reserved_paths.GenericFileSnapshot | reserved_paths.GenericFileCopy | originals.OriginalSnapshot,
) -> BoundCompanion:
    try:
        sidecar = originals.original_state(vault_root, artifact_path).sidecar_path
    except reserved_paths.ReservedPathLeafError as error:
        raise _companion_refusal(error) from error
    companion_path = sidecar.relative_to(Path(vault_root).resolve()).as_posix()
    companion = _read_companion(vault_root, companion_path)
    result = classify_proposed_sibling(
        artifact_path, artifact_sha256=_digest(artifact),
        artifact_size=_size(artifact), companion=companion.data,
    )
    return _bind_snapshots(
        result,
        ("artifact", artifact_path, artifact),
        ("companion", companion_path, companion),
    )


def classify_proposed_sibling(artifact_path: str, *, artifact_sha256: str,
                              artifact_size: int, companion: bytes) -> BoundCompanion:
    """Validate a proposed sibling against the batch writer's exact byte identity."""
    artifact_path = _canonical_relative_path(artifact_path)
    # Dataset cards and scene frames have separate canonical binding contracts.
    if PurePosixPath(artifact_path).suffix.casefold() in _DATASET_FORMATS or _FRAME_PATH_RE.fullmatch(artifact_path):
        raise CompanionClassificationError("descriptor_invalid")
    descriptor = _descriptor(companion)
    media_type = media_types.media_type_for(artifact_path)
    if media_type is None:
        result = _validate_common(
            descriptor,
            expected_keys=set(),
            expected_class="binary",
            artifact_path=artifact_path,
            artifact_sha256=artifact_sha256,
            artifact_size=artifact_size,
        )
        return result
    result = _validate_common(
        descriptor,
        expected_keys={"media_type", "original_filename"},
        expected_class="media",
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        artifact_size=artifact_size,
    )
    if descriptor.get("media_type") != media_type or descriptor.get(
        "original_filename"
    ) != PurePosixPath(artifact_path).name:
        raise CompanionClassificationError("artifact_mismatch")
    return result


def _dataset_cards(vault_root, artifact_path: str):
    try:
        entries = reserved_paths.list_generic_tree(
            vault_root, kb_dirname(), recursive=True
        )
    except reserved_paths.ReservedPathLeafError as error:
        raise CompanionClassificationError("companion_unsafe") from error
    cards: list[
        tuple[str, dict[str, object], reserved_paths.GenericFileSnapshot]
    ] = []
    for entry in entries:
        if entry.markdown is None:
            continue
        try:
            text = entry.markdown.decode("utf-8")
            frontmatter, _body, marker = vault.parse_frontmatter(text, strict=True)
        except (UnicodeDecodeError, vault.FrontmatterError):
            continue
        if (
            marker is not None
            and (
                frontmatter.get("type") == "dataset"
                or (
                    frontmatter.get("type") == "source"
                    and frontmatter.get("source_type") == "dataset-export"
                )
            )
            and frontmatter.get("data_file") == artifact_path
        ):
            cards.append(
                (
                    f"{kb_dirname()}/{entry.relative_path}",
                    frontmatter,
                    reserved_paths.GenericFileSnapshot(
                        entry.markdown,
                        entry.identity,
                        entry.mtime or 0.0,
                    ),
                )
            )
    return cards


def _classify_dataset(
    vault_root,
    artifact_path: str,
    artifact: reserved_paths.GenericFileSnapshot | reserved_paths.GenericFileCopy,
) -> BoundCompanion:
    cards = _dataset_cards(vault_root, artifact_path)
    if not cards:
        raise CompanionClassificationError("descriptor_missing")
    if len(cards) != 1:
        raise CompanionClassificationError("companion_ambiguous")
    companion_path, frontmatter, companion = cards[0]
    descriptor = _descriptor(companion.data)
    result = _validate_common(
        descriptor,
        expected_keys={"format"},
        expected_class="dataset",
        artifact_path=artifact_path,
        artifact_sha256=_digest(artifact),
        artifact_size=_size(artifact),
    )
    expected_format = _DATASET_FORMATS[PurePosixPath(artifact_path).suffix.casefold()]
    if descriptor.get("format") != expected_format or frontmatter.get(
        "format"
    ) != expected_format:
        raise CompanionClassificationError("artifact_mismatch")
    return _bind_snapshots(
        result,
        ("artifact", artifact_path, artifact),
        ("companion", companion_path, companion),
    )


def _classify_scene_frame(
    vault_root,
    artifact_path: str,
    artifact: reserved_paths.GenericFileSnapshot | reserved_paths.GenericFileCopy,
    match: re.Match[str],
) -> BoundCompanion:
    companion = _read_companion(vault_root, f"{artifact_path}.md")
    descriptor = _descriptor(companion.data)
    result = _validate_common(
        descriptor,
        expected_keys={"parent_path", "parent_sha256", "frame_timestamp_ms"},
        expected_class="scene_frame",
        artifact_path=artifact_path,
        artifact_sha256=_digest(artifact),
        artifact_size=_size(artifact),
    )
    parent_path = match.group("parent")
    if isinstance(artifact, reserved_paths.GenericFileCopy):
        scope = _BYTE_PROOFS.get()
        parent = scope[0].get(parent_path) if scope is not None else None
        if parent is not None:
            try:
                if parent.vault_root != vault_root or parent.relative_path != parent_path:
                    raise reserved_paths.ReservedPathLeafError("IO_REFUSED")
                reserved_paths.require_generic_copy_source_version(parent)
            except reserved_paths.ReservedPathLeafError as error:
                raise CompanionClassificationError("artifact_unsafe") from error
            return _bind_frame_parent(result, artifact_path, artifact, companion, descriptor, parent_path, parent, match)
        if scope is not None and not scope[1]:
            raise CompanionClassificationError("artifact_unsafe")
        with reserved_paths.private_copy_directory() as temporary:
            try:
                # A held artifact has no copy bound, so half the spool's free space bounds the parent's copy.
                parent = reserved_paths.copy_generic_file(
                    vault_root, parent_path, Path(temporary) / "parent",
                    max_bytes=shutil.disk_usage(temporary).free // 2,
                    deadline=artifact.deadline,
                )
                reserved_paths.require_current_generic_copy(parent)
                if scope is not None:
                    scope[0][parent_path] = parent
                return _bind_frame_parent(result, artifact_path, artifact, companion, descriptor, parent_path, parent, match)
            except reserved_paths.ReservedPathLeafError as error:
                raise CompanionClassificationError("artifact_unsafe") from error
    parent = _read_artifact(vault_root, parent_path)
    return _bind_frame_parent(result, artifact_path, artifact, companion, descriptor, parent_path, parent, match)


def _bind_frame_parent(result, artifact_path, artifact, companion, descriptor, parent_path, parent, match):
    timestamp = descriptor.get("frame_timestamp_ms")
    if (
        descriptor.get("parent_path") != parent_path
        or descriptor.get("parent_sha256")
        != _digest(parent)
        or not isinstance(timestamp, int)
        or isinstance(timestamp, bool)
        or not 0 <= timestamp <= 4_294_967_295
        or timestamp != int(match.group("millis"))
    ):
        raise CompanionClassificationError("artifact_mismatch")
    return _bind_snapshots(
        result,
        ("artifact", artifact_path, artifact),
        ("companion", f"{artifact_path}.md", companion),
        ("parent", parent_path, parent),
    )


def classify(vault_root, rel_path: str, *, artifact_copy: reserved_paths.GenericFileCopy | None = None) -> BoundCompanion:
    """Locate and validate the closed descriptor class for one artifact."""

    artifact_path = _canonical_relative_path(rel_path)
    if artifact_copy is not None and (artifact_copy.vault_root != vault_root or artifact_copy.relative_path != artifact_path):
        raise CompanionClassificationError("artifact_mismatch")
    artifact = artifact_copy if artifact_copy is not None else _read_artifact(vault_root, artifact_path)
    frame_match = _FRAME_PATH_RE.fullmatch(artifact_path)
    if frame_match is not None:
        return _classify_scene_frame(vault_root, artifact_path, artifact, frame_match)
    if PurePosixPath(artifact_path).suffix.casefold() in _DATASET_FORMATS:
        return _classify_dataset(vault_root, artifact_path, artifact)
    return _classify_sibling(vault_root, artifact_path, artifact)

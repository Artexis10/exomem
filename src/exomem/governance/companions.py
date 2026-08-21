"""Immutable governance-companion classification for non-Markdown artifacts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Literal

from .. import media_types, reserved_paths, vault
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


@dataclass(frozen=True, slots=True)
class BoundCompanion:
    """Artifact semantics proven by one exact descriptor and byte snapshot."""

    projects: tuple[str, ...]
    tags: tuple[str, ...]
    types: tuple[str, ...]
    classes: tuple[str, ...]
    identities: tuple[BoundSnapshot, ...] = ()


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


def _read_artifact(vault_root, rel_path: str) -> reserved_paths.GenericFileSnapshot:
    try:
        return reserved_paths.read_generic_bytes(vault_root, rel_path)
    except reserved_paths.ReservedPathLeafError as error:
        raise CompanionClassificationError("artifact_unsafe") from error


def _read_companion(vault_root, rel_path: str) -> reserved_paths.GenericFileSnapshot:
    try:
        return reserved_paths.read_generic_bytes(vault_root, rel_path)
    except reserved_paths.ReservedPathLeafError as error:
        reason: CompanionReason = (
            "descriptor_missing" if error.code == "MISSING" else "companion_unsafe"
        )
        raise CompanionClassificationError(reason) from error


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


def _descriptor(companion: reserved_paths.GenericFileSnapshot) -> dict[str, object]:
    try:
        text = companion.data.decode("utf-8")
        frontmatter, _body, marker = vault.parse_frontmatter(text, strict=True)
    except (UnicodeDecodeError, vault.FrontmatterError) as error:
        raise CompanionClassificationError("descriptor_invalid") from error
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
    artifact: reserved_paths.GenericFileSnapshot,
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
    actual_hash = hashlib.sha256(artifact.data).hexdigest()
    expected_hash = descriptor.get("artifact_sha256")
    expected_size = descriptor.get("artifact_size")
    if (
        not isinstance(expected_hash, str)
        or expected_hash != actual_hash
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size != len(artifact.data)
    ):
        raise CompanionClassificationError("artifact_mismatch")
    semantics = _semantics(descriptor.get("semantics"))
    return BoundCompanion(**semantics)


def _bind_snapshots(
    companion: BoundCompanion,
    *snapshots: tuple[str, str, reserved_paths.GenericFileSnapshot],
) -> BoundCompanion:
    identities = tuple(
        BoundSnapshot(
            role=role,
            path=path,
            sha256=hashlib.sha256(snapshot.data).hexdigest(),
            size=len(snapshot.data),
            device=snapshot.identity.device,
            inode=snapshot.identity.inode,
            kind=snapshot.identity.kind,
            link_count=snapshot.identity.link_count,
        )
        for role, path, snapshot in snapshots
    )
    return replace(companion, identities=identities)


def _classify_sibling(
    vault_root,
    artifact_path: str,
    artifact: reserved_paths.GenericFileSnapshot,
) -> BoundCompanion:
    companion = _read_companion(vault_root, f"{artifact_path}.md")
    descriptor = _descriptor(companion)
    media_type = media_types.media_type_for(artifact_path)
    if media_type is None:
        result = _validate_common(
            descriptor,
            expected_keys=set(),
            expected_class="binary",
            artifact_path=artifact_path,
            artifact=artifact,
        )
        return _bind_snapshots(
            result,
            ("artifact", artifact_path, artifact),
            ("companion", f"{artifact_path}.md", companion),
        )
    result = _validate_common(
        descriptor,
        expected_keys={"media_type", "original_filename"},
        expected_class="media",
        artifact_path=artifact_path,
        artifact=artifact,
    )
    if descriptor.get("media_type") != media_type or descriptor.get(
        "original_filename"
    ) != PurePosixPath(artifact_path).name:
        raise CompanionClassificationError("artifact_mismatch")
    return _bind_snapshots(
        result,
        ("artifact", artifact_path, artifact),
        ("companion", f"{artifact_path}.md", companion),
    )


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
            and frontmatter.get("type") == "dataset"
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
    artifact: reserved_paths.GenericFileSnapshot,
) -> BoundCompanion:
    cards = _dataset_cards(vault_root, artifact_path)
    if not cards:
        raise CompanionClassificationError("descriptor_missing")
    if len(cards) != 1:
        raise CompanionClassificationError("companion_ambiguous")
    companion_path, frontmatter, companion = cards[0]
    descriptor = _descriptor(companion)
    result = _validate_common(
        descriptor,
        expected_keys={"format"},
        expected_class="dataset",
        artifact_path=artifact_path,
        artifact=artifact,
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
    artifact: reserved_paths.GenericFileSnapshot,
    match: re.Match[str],
) -> BoundCompanion:
    companion = _read_companion(vault_root, f"{artifact_path}.md")
    descriptor = _descriptor(companion)
    result = _validate_common(
        descriptor,
        expected_keys={"parent_path", "parent_sha256", "frame_timestamp_ms"},
        expected_class="scene_frame",
        artifact_path=artifact_path,
        artifact=artifact,
    )
    parent_path = match.group("parent")
    parent = _read_artifact(vault_root, parent_path)
    timestamp = descriptor.get("frame_timestamp_ms")
    if (
        descriptor.get("parent_path") != parent_path
        or descriptor.get("parent_sha256")
        != hashlib.sha256(parent.data).hexdigest()
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


def classify(vault_root, rel_path: str) -> BoundCompanion:
    """Locate and validate the closed descriptor class for one artifact."""

    artifact_path = _canonical_relative_path(rel_path)
    artifact = _read_artifact(vault_root, artifact_path)
    frame_match = _FRAME_PATH_RE.fullmatch(artifact_path)
    if frame_match is not None:
        return _classify_scene_frame(vault_root, artifact_path, artifact, frame_match)
    if PurePosixPath(artifact_path).suffix.casefold() in _DATASET_FORMATS:
        return _classify_dataset(vault_root, artifact_path, artifact)
    return _classify_sibling(vault_root, artifact_path, artifact)

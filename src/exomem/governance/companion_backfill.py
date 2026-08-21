"""Pure planning for owner-reviewed legacy governance-companion backfill."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml

from .. import media_types, reserved_paths, vault
from ..clip_index import ClipIndex
from ..kbdir import kb_dirname
from . import companions

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_FRONTMATTER_RE = re.compile(
    r"\A---(?P<newline>\r?\n)(?P<inner>.*?)(?P=newline)---(?P<tail>\r?\n|\Z)",
    re.DOTALL,
)
_COMMON_KEYS = {
    "version",
    "artifact_class",
    "artifact_path",
    "expected_artifact_sha256",
    "expected_artifact_size",
    "expected_companion_path",
    "expected_companion_sha256",
    "semantics",
}
_EXTRA_KEYS = {
    "binary": set(),
    "media": {"media_type", "original_filename"},
    "dataset": {"format"},
    "scene_frame": {
        "parent_path",
        "expected_parent_sha256",
        "frame_timestamp_ms",
    },
}
_DATASET_FORMATS = {".csv": "csv", ".tsv": "tsv", ".json": "json"}


@dataclass(slots=True)
class CompanionBackfillError(Exception):
    """Content-free refusal raised while constructing an exact backfill plan."""

    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    """Exact predecessor and target bytes bound to immutable input snapshots."""

    normalized_input: dict[str, Any]
    descriptor: dict[str, Any]
    identities: tuple[dict[str, Any], ...]
    companion_path: str
    companion_identity: Any
    prior_bytes: bytes
    target_bytes: bytes

    @property
    def prior_value(self) -> dict[str, Any]:
        return _content_value(self.companion_path, self.prior_bytes)

    @property
    def target_value(self) -> dict[str, Any]:
        return _content_value(self.companion_path, self.target_bytes)


def _invalid(reason: str) -> CompanionBackfillError:
    return CompanionBackfillError("INVALID_COMPANION_BACKFILL", reason)


def _stale(reason: str) -> CompanionBackfillError:
    return CompanionBackfillError("STALE_COMPANION_BACKFILL", reason)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_path(value: object) -> str:
    if not isinstance(value, str):
        raise _invalid("path must be canonical text")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or normalized != path.as_posix()
        or not path.parts
        or path.parts[0] != kb_dirname()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise _invalid("path must be a canonical Knowledge Base relative path")
    return normalized


def _expected_hash(value: object, name: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise _invalid(f"{name} must be a lowercase SHA-256")
    return value


def _expected_size(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _invalid(f"{name} must be a non-negative integer")
    return value


def _semantics(value: object) -> dict[str, list[str]]:
    if not isinstance(value, Mapping) or set(value) != set(companions.SEMANTIC_KEYS):
        raise _invalid("semantics must contain exactly four explicit lists")
    result: dict[str, list[str]] = {}
    for key in companions.SEMANTIC_KEYS:
        raw = value[key]
        if (
            not isinstance(raw, list)
            or any(not isinstance(item, str) or not item for item in raw)
            or raw != sorted(set(raw))
        ):
            raise _invalid(f"semantics.{key} must be a canonical string list")
        result[key] = list(raw)
    return result


def _snapshot(vault_root, path: str, *, role: str):
    try:
        snapshot = reserved_paths.read_generic_bytes(vault_root, path)
    except reserved_paths.ReservedPathLeafError as error:
        raise _stale(f"{role} snapshot is unavailable") from error
    return snapshot


def _identity(role: str, path: str, snapshot) -> dict[str, Any]:
    return {
        "role": role,
        "path": path,
        "sha256": _sha(snapshot.data),
        "size": len(snapshot.data),
        "device": snapshot.identity.device,
        "inode": snapshot.identity.inode,
        "kind": snapshot.identity.kind,
        "link_count": snapshot.identity.link_count,
    }


def _content_value(path: str, value: bytes) -> dict[str, Any]:
    return {
        "path_hash": hashlib.sha256(path.encode("utf-8")).hexdigest(),
        "sha256": _sha(value),
        "size": len(value),
    }


def _frontmatter(companion: bytes) -> tuple[dict[str, Any], str, re.Match[str]]:
    try:
        text = companion.decode("utf-8")
        parsed, _body, marker = vault.parse_frontmatter(text, strict=True)
    except (UnicodeDecodeError, vault.FrontmatterError) as error:
        raise _invalid("companion frontmatter is invalid") from error
    match = _FRONTMATTER_RE.match(text)
    if marker is None or match is None:
        raise _invalid("companion requires canonical frontmatter")
    if "governance_companion" in parsed:
        raise _stale("companion is already classified")
    return parsed, text, match


def _render_target(companion: bytes, descriptor: Mapping[str, Any]) -> bytes:
    _parsed, text, match = _frontmatter(companion)
    newline = match.group("newline")
    rendered = yaml.safe_dump(
        {"governance_companion": dict(descriptor)},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).rstrip("\n")
    rendered = rendered.replace("\n", newline)
    insertion = newline + rendered
    target = text[: match.end("inner")] + insertion + text[match.end("inner") :]
    return target.encode("utf-8")


def _normalized_common(raw: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    artifact_class = raw.get("artifact_class")
    if artifact_class not in _EXTRA_KEYS:
        raise _invalid("artifact_class is not supported")
    if set(raw) != _COMMON_KEYS | _EXTRA_KEYS[str(artifact_class)]:
        raise _invalid("version-1 input fields are not exact")
    if raw.get("version") != 1 or isinstance(raw.get("version"), bool):
        raise _invalid("version must be integer 1")
    artifact_path = _canonical_path(raw.get("artifact_path"))
    companion_path = _canonical_path(raw.get("expected_companion_path"))
    normalized: dict[str, Any] = {
        "version": 1,
        "artifact_class": str(artifact_class),
        "artifact_path": artifact_path,
        "expected_artifact_sha256": _expected_hash(
            raw.get("expected_artifact_sha256"), "expected_artifact_sha256"
        ),
        "expected_artifact_size": _expected_size(
            raw.get("expected_artifact_size"), "expected_artifact_size"
        ),
        "expected_companion_path": companion_path,
        "expected_companion_sha256": _expected_hash(
            raw.get("expected_companion_sha256"), "expected_companion_sha256"
        ),
        "semantics": _semantics(raw.get("semantics")),
    }
    return normalized, str(artifact_class)


def _validate_artifact_snapshot(normalized: Mapping[str, Any], artifact) -> None:
    if (
        _sha(artifact.data) != normalized["expected_artifact_sha256"]
        or len(artifact.data) != normalized["expected_artifact_size"]
    ):
        raise _stale("artifact bytes changed")


def _validate_companion_snapshot(normalized: Mapping[str, Any], companion) -> None:
    if _sha(companion.data) != normalized["expected_companion_sha256"]:
        raise _stale("companion bytes changed")


def _descriptor_common(
    normalized: Mapping[str, Any], artifact
) -> dict[str, Any]:
    return {
        "version": 1,
        "state": "classified",
        "artifact_class": normalized["artifact_class"],
        "artifact_path": normalized["artifact_path"],
        "artifact_sha256": _sha(artifact.data),
        "artifact_size": len(artifact.data),
        "semantics": normalized["semantics"],
    }


def _class_specific(
    vault_root,
    raw: Mapping[str, Any],
    normalized: dict[str, Any],
    artifact_class: str,
    artifact,
    companion,
) -> tuple[dict[str, Any], list[tuple[str, str, Any]]]:
    artifact_path = str(normalized["artifact_path"])
    companion_path = str(normalized["expected_companion_path"])
    descriptor = _descriptor_common(normalized, artifact)
    additional: list[tuple[str, str, Any]] = []

    if artifact_class in {"binary", "media"}:
        if companion_path != f"{artifact_path}.md":
            raise _stale("sibling companion locator does not match")
        actual_media_type = media_types.media_type_for(artifact_path)
        if artifact_class == "binary":
            if actual_media_type is not None:
                raise _stale("artifact class does not match its media type")
        else:
            media_type = raw.get("media_type")
            original_filename = raw.get("original_filename")
            if (
                not isinstance(media_type, str)
                or media_type != actual_media_type
                or not isinstance(original_filename, str)
                or original_filename != PurePosixPath(artifact_path).name
            ):
                raise _stale("media binding fields do not match")
            normalized["media_type"] = media_type
            normalized["original_filename"] = original_filename
            descriptor["media_type"] = media_type
            descriptor["original_filename"] = original_filename
        return descriptor, additional

    if artifact_class == "dataset":
        expected_format = _DATASET_FORMATS.get(
            PurePosixPath(artifact_path).suffix.casefold()
        )
        if raw.get("format") != expected_format:
            raise _stale("dataset format does not match")
        cards = companions._dataset_cards(vault_root, artifact_path)
        if len(cards) != 1 or cards[0][0] != companion_path:
            raise _stale("dataset companion is missing or ambiguous")
        _path, frontmatter, card_snapshot = cards[0]
        if card_snapshot.identity != companion.identity or (
            frontmatter.get("format") != expected_format
        ):
            raise _stale("dataset companion binding changed")
        normalized["format"] = expected_format
        descriptor["format"] = expected_format
        return descriptor, additional

    match = companions._FRAME_PATH_RE.fullmatch(artifact_path)
    if match is None or companion_path != f"{artifact_path}.md":
        raise _stale("scene-frame locator does not match")
    parent_path = _canonical_path(raw.get("parent_path"))
    if parent_path != match.group("parent"):
        raise _stale("scene-frame parent path does not match")
    parent = _snapshot(vault_root, parent_path, role="parent")
    parent_hash = _expected_hash(
        raw.get("expected_parent_sha256"), "expected_parent_sha256"
    )
    if _sha(parent.data) != parent_hash:
        raise _stale("scene-frame parent bytes changed")
    parsed, _text, _match = _frontmatter(companion.data)
    legacy_timestamp = parsed.get("frame_ts")
    if (
        not isinstance(legacy_timestamp, (int, float))
        or isinstance(legacy_timestamp, bool)
        or not math.isfinite(float(legacy_timestamp))
        or float(legacy_timestamp) < 0
    ):
        raise _invalid("legacy frame_ts must be finite and non-negative")
    milliseconds = int(round(float(legacy_timestamp) * 1000))
    if not 0 <= milliseconds <= 4_294_967_295:
        raise _invalid("legacy frame_ts is outside the bounded millisecond range")
    requested_timestamp = raw.get("frame_timestamp_ms")
    if (
        not isinstance(requested_timestamp, int)
        or isinstance(requested_timestamp, bool)
        or requested_timestamp != milliseconds
        or requested_timestamp != int(match.group("millis"))
        or parsed.get("parent_media") != parent_path
    ):
        raise _stale("scene-frame timestamp or parent disagrees")
    indexed = ClipIndex(vault_root).frame_timestamps(parent_path)
    matching_index = [
        value
        for value in indexed
        if math.isfinite(value)
        and value >= 0
        and int(round(value * 1000)) == milliseconds
    ]
    if len(matching_index) != 1 or matching_index[0] != float(legacy_timestamp):
        raise _stale("scene-frame index timestamp is missing or ambiguous")
    normalized["parent_path"] = parent_path
    normalized["expected_parent_sha256"] = parent_hash
    normalized["frame_timestamp_ms"] = milliseconds
    descriptor["parent_path"] = parent_path
    descriptor["parent_sha256"] = parent_hash
    descriptor["frame_timestamp_ms"] = milliseconds
    additional.append(("parent", parent_path, parent))
    return descriptor, additional


def plan(vault_root, value: object) -> BackfillPlan:
    """Validate exact v1 input and construct byte-exact predecessor/target state."""

    if not isinstance(value, Mapping):
        raise _invalid("companion_input must be an object")
    normalized, artifact_class = _normalized_common(value)
    artifact_path = str(normalized["artifact_path"])
    companion_path = str(normalized["expected_companion_path"])
    artifact = _snapshot(vault_root, artifact_path, role="artifact")
    companion = _snapshot(vault_root, companion_path, role="companion")
    _validate_artifact_snapshot(normalized, artifact)
    _validate_companion_snapshot(normalized, companion)
    _frontmatter(companion.data)
    descriptor, additional = _class_specific(
        vault_root,
        value,
        normalized,
        artifact_class,
        artifact,
        companion,
    )
    target = _render_target(companion.data, descriptor)
    identities = [
        _identity("artifact", artifact_path, artifact),
        _identity("companion", companion_path, companion),
        *(_identity(role, path, snapshot) for role, path, snapshot in additional),
    ]
    return BackfillPlan(
        normalized_input=normalized,
        descriptor=descriptor,
        identities=tuple(identities),
        companion_path=companion_path,
        companion_identity=companion.identity,
        prior_bytes=companion.data,
        target_bytes=target,
    )

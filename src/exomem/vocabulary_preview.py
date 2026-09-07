"""Pure canonical owner-preview encoding for vocabulary write images."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from .vocabulary_auxiliaries import _ROLES
from .vocabulary_effects import _MAX_IMAGE_BYTES, CanonicalWriteImage, Effect, _result

if TYPE_CHECKING:
    from .vocabulary_authority import CanonicalOperation

MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_PREVIEW_FILES = 16

_PAYLOAD_FIELDS = frozenset({"version", "images"})
_IMAGE_FIELDS = frozenset(
    {"path", "before", "after", "before_sha256", "after_sha256", "role"}
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _operation(value: object) -> CanonicalOperation:
    from .vocabulary_authority import CanonicalOperation

    if (
        not isinstance(value, CanonicalOperation)
        or not isinstance(value.image_digest, str)
        or _SHA256.fullmatch(value.image_digest) is None
        or not isinstance(value.effects, tuple)
        or not all(isinstance(effect, Effect) for effect in value.effects)
    ):
        raise ValueError("VOCABULARY_PREVIEW_INVALID")
    return value


def _check_hash(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("VOCABULARY_PREVIEW_INVALID")
    return value


def _check_role(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _ROLES:
        raise ValueError("VOCABULARY_PREVIEW_INVALID")
    return value


def _check_size(images: tuple[CanonicalWriteImage, ...]) -> None:
    if len(images) > MAX_PREVIEW_FILES:
        raise ValueError("VOCABULARY_PREVIEW_TOO_LARGE")
    total = 0
    for image in images:
        before_size = len(image.before) if image.before is not None else 0
        after_size = len(image.after) if image.after is not None else 0
        if max(before_size, after_size) > _MAX_IMAGE_BYTES:
            raise ValueError("VOCABULARY_PREVIEW_TOO_LARGE")
        total += before_size + after_size
        if total > MAX_PREVIEW_BYTES:
            raise ValueError("VOCABULARY_PREVIEW_TOO_LARGE")


def _check_images(images: Iterable[CanonicalWriteImage]) -> tuple[CanonicalWriteImage, ...]:
    received = tuple(images)
    if not all(isinstance(image, CanonicalWriteImage) for image in received):
        raise ValueError("VOCABULARY_PREVIEW_INVALID")
    _check_size(received)
    ordered = tuple(sorted(received, key=lambda image: image.path))
    if len({image.path for image in ordered}) != len(ordered):
        raise ValueError("VOCABULARY_PREVIEW_DUPLICATE_PATH")
    for image in ordered:
        _check_role(image.role)
        try:
            checked = CanonicalWriteImage(
                image.path,
                image.before,
                image.after,
                image.before_sha256,
                image.after_sha256,
                image.role,
            )
        except ValueError as exc:
            code = (
                "VOCABULARY_PREVIEW_HASH_MISMATCH"
                if str(exc) == "VOCABULARY_EFFECT_HASH_MISMATCH"
                else "VOCABULARY_PREVIEW_INVALID"
            )
            raise ValueError(code) from exc
        if checked.path != image.path:
            raise ValueError("VOCABULARY_PREVIEW_PATH_INVALID")
    return ordered


def _check_digest(
    images: tuple[CanonicalWriteImage, ...], operation: CanonicalOperation
) -> None:
    digest = _result("reviewed", operation.effects, (), images).digest
    if digest != operation.image_digest:
        raise ValueError("VOCABULARY_PREVIEW_DIGEST_MISMATCH")


def _encode_text(value: bytes | None) -> str | None:
    if value is None:
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("VOCABULARY_PREVIEW_UTF8_INVALID") from exc


def encode_preview(
    images: Iterable[CanonicalWriteImage], operation: CanonicalOperation
) -> dict[str, Any]:
    """Return a detached, closed v1 preview of exact canonical write images."""

    checked_operation = _operation(operation)
    ordered = _check_images(images)
    _check_digest(ordered, checked_operation)
    return {
        "version": 1,
        "images": [
            {
                "path": image.path,
                "before": _encode_text(image.before),
                "after": _encode_text(image.after),
                "before_sha256": image.before_sha256,
                "after_sha256": image.after_sha256,
                "role": image.role,
            }
            for image in ordered
        ],
    }


def _decode_text(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("VOCABULARY_PREVIEW_INVALID")
    return value.encode("utf-8")


def decode_preview(
    payload: Mapping[str, Any], operation: CanonicalOperation
) -> tuple[CanonicalWriteImage, ...]:
    """Validate and reconstruct the exact images bound to ``operation``."""

    checked_operation = _operation(operation)
    if (
        not isinstance(payload, Mapping)
        or set(payload) != _PAYLOAD_FIELDS
        or type(payload["version"]) is not int
        or payload["version"] != 1
        or not isinstance(payload["images"], list)
    ):
        raise ValueError("VOCABULARY_PREVIEW_INVALID")
    raw_images = payload["images"]
    if len(raw_images) > MAX_PREVIEW_FILES:
        raise ValueError("VOCABULARY_PREVIEW_TOO_LARGE")

    images: list[CanonicalWriteImage] = []
    total = 0
    previous_path: str | None = None
    for raw in raw_images:
        if not isinstance(raw, Mapping) or set(raw) != _IMAGE_FIELDS:
            raise ValueError("VOCABULARY_PREVIEW_INVALID")
        path = raw["path"]
        if not isinstance(path, str):
            raise ValueError("VOCABULARY_PREVIEW_PATH_INVALID")
        before = _decode_text(raw["before"])
        after = _decode_text(raw["after"])
        before_size = len(before) if before is not None else 0
        after_size = len(after) if after is not None else 0
        if max(before_size, after_size) > _MAX_IMAGE_BYTES:
            raise ValueError("VOCABULARY_PREVIEW_TOO_LARGE")
        total += before_size + after_size
        if total > MAX_PREVIEW_BYTES:
            raise ValueError("VOCABULARY_PREVIEW_TOO_LARGE")
        try:
            image = CanonicalWriteImage(
                path,
                before,
                after,
                _check_hash(raw["before_sha256"]),
                _check_hash(raw["after_sha256"]),
                _check_role(raw["role"]),
            )
        except ValueError as exc:
            if str(exc) == "VOCABULARY_EFFECT_HASH_MISMATCH":
                raise ValueError("VOCABULARY_PREVIEW_HASH_MISMATCH") from exc
            if str(exc) == "VOCABULARY_EFFECT_PATH_INVALID":
                raise ValueError("VOCABULARY_PREVIEW_PATH_INVALID") from exc
            raise
        if image.path != path:
            raise ValueError("VOCABULARY_PREVIEW_PATH_INVALID")
        if previous_path is not None and path <= previous_path:
            code = (
                "VOCABULARY_PREVIEW_DUPLICATE_PATH"
                if path == previous_path
                else "VOCABULARY_PREVIEW_PATH_INVALID"
            )
            raise ValueError(code)
        images.append(image)
        previous_path = path

    decoded = tuple(images)
    _check_digest(decoded, checked_operation)
    return decoded

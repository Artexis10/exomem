from __future__ import annotations

import copy
import hashlib

import pytest

from exomem.vocabulary_authority import CanonicalOperation
from exomem.vocabulary_effects import CanonicalWriteImage, Effect, _result
from exomem.vocabulary_preview import (
    MAX_PREVIEW_BYTES,
    MAX_PREVIEW_FILES,
    decode_preview,
    encode_preview,
)


def _image(
    path: str,
    before: bytes | None,
    after: bytes | None,
    *,
    role: str | None = None,
) -> CanonicalWriteImage:
    return CanonicalWriteImage(path, before, after, role=role)


def _operation(images: tuple[CanonicalWriteImage, ...]) -> CanonicalOperation:
    effects = (Effect("entity.create", "Knowledge Base/Entities/Things/example.md"),)
    digest = _result("reviewed", effects, (), images).digest
    return CanonicalOperation.from_effects(
        operation_id="operation-1",
        command_digest="a" * 64,
        effects=effects,
        image_digest=digest,
        registry_digests={},
        target_digests={},
    )


def test_round_trips_sorted_unicode_empty_new_and_deleted_images() -> None:
    images = (
        _image("Knowledge Base/z.md", "Tere, maailm 🌍\n".encode(), None),
        _image("Knowledge Base/a.md", None, b""),
        _image(
            "Knowledge Base/m.md",
            b"",
            "Muudetud ✓\n".encode(),
            role="relation-review",
        ),
    )
    operation = _operation(images)

    payload = encode_preview(images, operation)

    assert payload == {
        "version": 1,
        "images": [
            {
                "path": "Knowledge Base/a.md",
                "before": None,
                "after": "",
                "before_sha256": None,
                "after_sha256": hashlib.sha256(b"").hexdigest(),
                "role": None,
            },
            {
                "path": "Knowledge Base/m.md",
                "before": "",
                "after": "Muudetud ✓\n",
                "before_sha256": hashlib.sha256(b"").hexdigest(),
                "after_sha256": hashlib.sha256("Muudetud ✓\n".encode()).hexdigest(),
                "role": "relation-review",
            },
            {
                "path": "Knowledge Base/z.md",
                "before": "Tere, maailm 🌍\n",
                "after": None,
                "before_sha256": hashlib.sha256("Tere, maailm 🌍\n".encode()).hexdigest(),
                "after_sha256": None,
                "role": None,
            },
        ],
    }
    assert decode_preview(payload, operation) == tuple(sorted(images, key=lambda image: image.path))


def test_encode_returns_a_detached_snapshot() -> None:
    images = [_image("Knowledge Base/a.md", None, b"first")]
    operation = _operation(tuple(images))

    payload = encode_preview(images, operation)
    images[0] = _image("Knowledge Base/a.md", None, b"second")

    assert payload["images"][0]["after"] == "first"


def test_encode_refuses_invalid_utf8() -> None:
    images = (_image("Knowledge Base/a.md", None, b"\xff"),)

    with pytest.raises(ValueError, match="VOCABULARY_PREVIEW_UTF8_INVALID"):
        encode_preview(images, _operation(images))


@pytest.mark.parametrize("field", ["after", "path", "role"])
def test_decode_refuses_an_altered_byte_path_or_role(field: str) -> None:
    images = (_image("Knowledge Base/a.md", None, b"first"),)
    operation = _operation(images)
    payload = encode_preview(images, operation)
    item = payload["images"][0]
    if field == "after":
        item[field] = "second"
        item["after_sha256"] = hashlib.sha256(b"second").hexdigest()
    elif field == "path":
        item[field] = "Knowledge Base/b.md"
    else:
        item[field] = "relation-review"

    with pytest.raises(ValueError, match="VOCABULARY_PREVIEW_DIGEST_MISMATCH"):
        decode_preview(payload, operation)


def test_encode_and_decode_refuse_a_wrong_operation_digest() -> None:
    images = (_image("Knowledge Base/a.md", None, b"first"),)
    operation = _operation(images)
    wrong = CanonicalOperation.from_effects(
        operation_id="operation-1",
        command_digest="a" * 64,
        effects=operation.effects,
        image_digest="0" * 64,
        registry_digests={},
        target_digests={},
    )
    payload = encode_preview(images, operation)

    with pytest.raises(ValueError, match="VOCABULARY_PREVIEW_DIGEST_MISMATCH"):
        encode_preview(images, wrong)
    with pytest.raises(ValueError, match="VOCABULARY_PREVIEW_DIGEST_MISMATCH"):
        decode_preview(payload, wrong)


@pytest.mark.parametrize("change", ["omit", "add"])
def test_decode_refuses_omitted_or_additional_images(change: str) -> None:
    images = (
        _image("Knowledge Base/a.md", None, b"first"),
        _image("Knowledge Base/b.md", None, b"second"),
    )
    operation = _operation(images)
    payload = encode_preview(images, operation)
    if change == "omit":
        payload["images"].pop()
    else:
        extra = copy.deepcopy(payload["images"][0])
        extra["path"] = "Knowledge Base/c.md"
        payload["images"].append(extra)

    with pytest.raises(ValueError, match="VOCABULARY_PREVIEW_DIGEST_MISMATCH"):
        decode_preview(payload, operation)


@pytest.mark.parametrize("path", ["/absolute.md", "../escape.md", "a/../b.md", "a//b.md", "a\\b.md", "a.md/"])
def test_decode_refuses_unsafe_or_noncanonical_paths(path: str) -> None:
    images = (_image("Knowledge Base/a.md", None, b"first"),)
    operation = _operation(images)
    payload = encode_preview(images, operation)
    payload["images"][0]["path"] = path

    with pytest.raises(ValueError, match="VOCABULARY_PREVIEW_PATH_INVALID"):
        decode_preview(payload, operation)


def test_decode_refuses_duplicate_paths() -> None:
    images = (
        _image("Knowledge Base/a.md", None, b"first"),
        _image("Knowledge Base/b.md", None, b"second"),
    )
    operation = _operation(images)
    payload = encode_preview(images, operation)
    payload["images"][1]["path"] = payload["images"][0]["path"]

    with pytest.raises(ValueError, match="VOCABULARY_PREVIEW_DUPLICATE_PATH"):
        decode_preview(payload, operation)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(version=2),
        lambda payload: payload.update(extra=True),
        lambda payload: payload["images"][0].update(extra=True),
        lambda payload: payload.update(images={}),
        lambda payload: payload["images"][0].update(after=1),
        lambda payload: payload["images"][0].update(role=1),
        lambda payload: payload["images"][0].update(role="unknown-role"),
        lambda payload: payload["images"][0].update(after_sha256="A" * 64),
        lambda payload: payload["images"][0].pop("before"),
    ],
)
def test_decode_refuses_malformed_or_open_ended_payloads(mutate) -> None:
    images = (_image("Knowledge Base/a.md", None, b"first"),)
    operation = _operation(images)
    payload = encode_preview(images, operation)
    mutate(payload)

    with pytest.raises(ValueError, match="VOCABULARY_PREVIEW_INVALID"):
        decode_preview(payload, operation)


def test_decode_refuses_a_hash_that_does_not_match_content() -> None:
    images = (_image("Knowledge Base/a.md", None, b"first"),)
    operation = _operation(images)
    payload = encode_preview(images, operation)
    payload["images"][0]["after_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="VOCABULARY_PREVIEW_HASH_MISMATCH"):
        decode_preview(payload, operation)


def test_encode_refuses_more_than_the_file_limit() -> None:
    images = tuple(_image(f"Knowledge Base/{index}.md", None, b"") for index in range(MAX_PREVIEW_FILES + 1))

    with pytest.raises(ValueError, match="VOCABULARY_PREVIEW_TOO_LARGE"):
        encode_preview(images, _operation(images))


def test_encode_refuses_one_image_over_the_classifier_limit() -> None:
    images = (_image("Knowledge Base/a.md", None, b"x" * (1024 * 1024 + 1)),)

    with pytest.raises(ValueError, match="VOCABULARY_PREVIEW_TOO_LARGE"):
        encode_preview(images, _operation(images))


def test_encode_refuses_an_aggregate_over_the_preview_limit() -> None:
    chunk = b"x" * (1024 * 1024)
    images = tuple(
        _image(f"Knowledge Base/{index}.md", chunk, chunk) for index in range(MAX_PREVIEW_BYTES // len(chunk) // 2 + 1)
    )

    with pytest.raises(ValueError, match="VOCABULARY_PREVIEW_TOO_LARGE"):
        encode_preview(images, _operation(images))

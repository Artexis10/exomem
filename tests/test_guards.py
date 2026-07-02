"""guards.guard_text_content — reject base64 binary blobs at the write boundary."""

from __future__ import annotations

import base64
import io

import pytest

from exomem import guards
from exomem import preserve as preserve_module


# ---------------- the base64-blob heuristic ----------------


def test_prose_is_not_a_blob() -> None:
    # A large (~275 KB), legit markdown note is full of whitespace → never a blob.
    note = ("# Heading\n\nSome **prose** with spaces and punctuation. " * 5000)
    assert guards._looks_like_base64_blob(note) is False
    guards.guard_text_content(note, tool="note")  # does not raise


def test_data_uri_is_a_blob() -> None:
    assert guards._looks_like_base64_blob("data:image/png;base64," + "A" * 30000) is True


def test_unbroken_base64_is_a_blob() -> None:
    blob = base64.b64encode(b"\x89PNG" * 20000).decode("ascii")
    assert guards._looks_like_base64_blob(blob) is True


def test_newline_wrapped_base64_is_a_blob() -> None:
    # The classic 76-column-wrapped base64 shape.
    wrapped = "\n".join([base64.b64encode(b"x" * 57).decode("ascii")] * 600)
    assert guards._looks_like_base64_blob(wrapped) is True


def test_short_content_is_never_a_blob() -> None:
    assert guards._looks_like_base64_blob("QUJD" * 10) is False


# ---------------- guard_text_content ----------------


def test_guard_rejects_base64_with_actionable_message() -> None:
    blob = base64.b64encode(b"\x00\x01\x02" * 30000).decode("ascii")
    with pytest.raises(ValueError) as exc:
        guards.guard_text_content(blob, tool="add", field="content")
    msg = str(exc.value)
    assert "BINARY_BLOB_REJECTED" in msg
    assert "/upload" in msg  # points at the proper out-of-band path


def test_guard_rejects_oversized_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guards, "MAX_TEXT_CONTENT_BYTES", 100)
    with pytest.raises(ValueError) as exc:
        guards.guard_text_content("x " * 200, tool="create_file")  # spaced → not a blob
    assert "BINARY_BLOB_REJECTED" in str(exc.value)


def test_guard_noop_on_empty_or_none() -> None:
    guards.guard_text_content(None, tool="add")
    guards.guard_text_content("", tool="add")
    guards.guard_text_content("a normal short note", tool="note")


# ---------------- preserve_bytes (the /upload entrypoint) ----------------


def test_preserve_bytes_writes_file(vault) -> None:
    payload = b"\x89PNG\r\n\x1a\nrealbytes"
    result = preserve_module.preserve_bytes(
        vault,
        scope="Yolo",
        category="01 - Check-in",
        filename="shot.png",
        data=payload,
    )
    written = vault / result.path
    assert written.exists()
    assert written.read_bytes() == payload


def test_preserve_bytes_honors_max_bytes(vault) -> None:
    with pytest.raises(preserve_module.PreserveError) as exc:
        preserve_module.preserve_bytes(
            vault,
            scope="x",
            category="y",
            filename="big.bin",
            data=b"x" * 2048,
            max_bytes=1024,
        )
    assert exc.value.code == "TOO_LARGE"


# ---------------- preserve_stream (the streaming /upload write path) ----------------


def test_preserve_stream_writes_exact_bytes(vault) -> None:
    payload = b"\x00\x01\x02binary-stream" * 1000
    result = preserve_module.preserve_stream(
        vault,
        scope="Yolo",
        category="01 - Check-in",
        filename="clip.bin",
        stream=io.BytesIO(payload),
    )
    assert (vault / result.path).read_bytes() == payload


def test_preserve_stream_handles_file_larger_than_base64_cap(vault) -> None:
    # 6 MB — above the 5 MB base64-via-model cap. Proves /upload uses the streaming
    # path (chunked to disk), not the token-budget base64 path that MAX_DECODED_BYTES
    # guards.
    payload = b"x" * (6 * 1024 * 1024)
    result = preserve_module.preserve_stream(
        vault,
        scope="x",
        category="y",
        filename="big.bin",
        stream=io.BytesIO(payload),
        max_bytes=10 * 1024 * 1024,
    )
    assert (vault / result.path).stat().st_size == len(payload)


def test_preserve_stream_honors_max_bytes(vault) -> None:
    with pytest.raises(preserve_module.PreserveError) as exc:
        preserve_module.preserve_stream(
            vault,
            scope="x",
            category="y",
            filename="big.bin",
            stream=io.BytesIO(b"x" * 2048),
            max_bytes=1024,
        )
    assert exc.value.code == "TOO_LARGE"

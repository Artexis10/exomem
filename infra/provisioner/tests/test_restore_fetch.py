from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import httpx
import pytest

from exomem_provisioner.restore_fetch import RestoreFetchError, fetch_restore_source


def _url_file(tmp_path: Path) -> Path:
    path = tmp_path / "url"
    path.write_text("https://s3.example.invalid/object?signature=secret", encoding="utf-8")
    return path


def test_fetch_restore_source_streams_exact_bytes_without_exposing_url(tmp_path: Path) -> None:
    payload = b"portable-archive" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "s3.example.invalid"
        return httpx.Response(
            200,
            headers={"content-length": str(len(payload))},
            content=payload,
        )

    destination = tmp_path / "source.portable"
    fetch_restore_source(
        url_file=_url_file(tmp_path),
        output=destination,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_size=len(payload),
        allowed_host="s3.example.invalid",
        transport=httpx.MockTransport(handler),
    )

    assert destination.read_bytes() == payload
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not (tmp_path / ".source.portable.partial").exists()


@pytest.mark.parametrize("failure", ["host", "redirect", "size", "digest"])
def test_fetch_restore_source_fails_closed_and_removes_partial(
    tmp_path: Path,
    failure: str,
) -> None:
    payload = b"portable-archive"
    url_file = _url_file(tmp_path)
    expected_size = len(payload)
    digest = hashlib.sha256(payload).hexdigest()
    if failure == "host":
        url_file.write_text("https://evil.invalid/object?signature=secret", encoding="utf-8")
    elif failure == "size":
        expected_size += 1
    elif failure == "digest":
        digest = "0" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.invalid/stolen"})
        return httpx.Response(200, content=payload)

    destination = tmp_path / "source.portable"
    with pytest.raises(RestoreFetchError):
        fetch_restore_source(
            url_file=url_file,
            output=destination,
            expected_sha256=digest,
            expected_size=expected_size,
            allowed_host="s3.example.invalid",
            transport=httpx.MockTransport(handler),
        )

    assert not destination.exists()
    assert not (tmp_path / ".source.portable.partial").exists()


def test_fetch_restore_source_refuses_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "source.portable"
    destination.write_bytes(b"do-not-overwrite")

    with pytest.raises(RestoreFetchError):
        fetch_restore_source(
            url_file=_url_file(tmp_path),
            output=destination,
            expected_sha256="0" * 64,
            expected_size=1,
            allowed_host="s3.example.invalid",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x")),
        )

    assert destination.read_bytes() == b"do-not-overwrite"

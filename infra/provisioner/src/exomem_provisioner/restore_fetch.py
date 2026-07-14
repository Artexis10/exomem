"""Content-free bounded fetcher for one presigned restore staging object."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_MAX_URL_BYTES = 8192
_MAX_ARCHIVE_BYTES = 6 * 1024 * 1024 * 1024


class RestoreFetchError(RuntimeError):
    pass


def _read_url(path: Path, *, allowed_host: str) -> str:
    try:
        raw = path.read_bytes()
        value = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RestoreFetchError("restore source URL is unavailable") from error
    if not 1 <= len(raw) <= _MAX_URL_BYTES or value != value.strip() or "\n" in value:
        raise RestoreFetchError("restore source URL is invalid")
    parsed = urlsplit(value)
    if (
        not _HOST.fullmatch(allowed_host)
        or parsed.scheme != "https"
        or parsed.hostname != allowed_host
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not parsed.path.startswith("/")
        or not parsed.query
    ):
        raise RestoreFetchError("restore source URL is invalid")
    return value


def fetch_restore_source(
    *,
    url_file: Path,
    output: Path,
    expected_sha256: str,
    expected_size: int,
    allowed_host: str,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Stream exactly one authenticated archive into a new private file."""

    if (
        not _SHA256.fullmatch(expected_sha256)
        or not 1 <= expected_size <= _MAX_ARCHIVE_BYTES
        or not output.is_absolute()
        or output.exists()
    ):
        raise RestoreFetchError("restore fetch contract is invalid")
    url = _read_url(url_file, allowed_host=allowed_host)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = output.with_name(f".{output.name}.partial")
    if partial.exists():
        raise RestoreFetchError("restore fetch destination is busy")
    digest = hashlib.sha256()
    size = 0
    try:
        with httpx.Client(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(900.0, connect=10.0),
        ) as client:
            with client.stream(
                "GET", url, headers={"Accept": "application/octet-stream"}
            ) as response:
                if response.status_code != 200:
                    raise RestoreFetchError("restore source request failed")
                length = response.headers.get("content-length")
                if length is not None and (not length.isdigit() or int(length) != expected_size):
                    raise RestoreFetchError("restore source size differs")
                descriptor = os.open(
                    partial,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as stream:
                        for chunk in response.iter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > expected_size:
                                raise RestoreFetchError("restore source exceeds expected size")
                            digest.update(chunk)
                            stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                except Exception:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    raise
        if size != expected_size or digest.hexdigest() != expected_sha256:
            raise RestoreFetchError("restore source proof differs")
        os.replace(partial, output)
        output.chmod(0o600, follow_symlinks=False)
    except RestoreFetchError:
        raise
    except (OSError, httpx.HTTPError, ValueError) as error:
        raise RestoreFetchError("restore source fetch failed") from error
    finally:
        partial.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--url-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-size", required=True, type=int)
    parser.add_argument("--allowed-host", required=True)
    return parser


def run_restore_fetch(arguments: list[str] | None = None) -> None:
    values: Any = _parser().parse_args(arguments)
    try:
        fetch_restore_source(
            url_file=values.url_file,
            output=values.output,
            expected_sha256=values.expected_sha256,
            expected_size=values.expected_size,
            allowed_host=values.allowed_host,
        )
    except RestoreFetchError as error:
        raise SystemExit("restore source fetch failed") from error

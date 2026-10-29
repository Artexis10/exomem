"""Download one preserved original from Exomem Cloud over the owner's OAuth: verified bytes, or no file."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import string
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx2

from .cloud_import_client import (
    CloudImportClientConfig,
    add_endpoint_arguments,
    authorized_api_client,
    safe_code,
    server_code,
)

CHUNK_BYTES = 1 << 20
# A refusal is a short JSON object; anything longer is not read.
REFUSAL_BYTES = 64 * 1024
_DISPOSITION_PREFIX = "attachment; filename*=utf-8''"


class CloudDownloadError(Exception):
    """A refusal whose code and message are safe to print."""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.message = message


def _refusal(response: httpx2.Response, body: bytes) -> CloudDownloadError:
    """The cell's stable code when its JSON body names one, else the gateway's or the status."""
    try:
        code = safe_code(json.loads(body)["code"])
    except (ValueError, KeyError, TypeError):
        code = None
    code = code or server_code(httpx2.Response(response.status_code, content=body))
    return CloudDownloadError(code, "the Cloud service refused the download")


async def _read_refusal(response: httpx2.Response) -> bytes:
    body = b""
    async for chunk in response.aiter_bytes(CHUNK_BYTES):
        body += chunk
        if len(body) >= REFUSAL_BYTES:
            return body[:REFUSAL_BYTES]
    return body


def _place(pending: Path, final: Path, *, overwrite: bool) -> None:
    """Move the verified file into place; without `overwrite` an existing target is kept."""
    if overwrite:
        os.replace(pending, final)
        return
    try:
        if os.name == "nt":
            os.rename(pending, final)  # fails on an existing target
        else:
            os.link(pending, final)  # fails on an existing target; the pending name is removed by the caller
    except FileExistsError:
        raise CloudDownloadError("FILE_EXISTS", f"{final} already exists; use --overwrite to replace it") from None


def _digest(value: str | None) -> str:
    value = (value or "").lower()
    # A format check on a digest header: without a verifiable digest the file is never kept.
    if len(value) != 64 or not all(char in string.hexdigits for char in value):
        raise CloudDownloadError("DIGEST_UNAVAILABLE", "the response carries no digest to verify against")
    return value


def _count(value: str | None, code: str) -> int:
    if value is None or not value.isascii() or not value.isdigit():
        raise CloudDownloadError(code, "the response does not state a byte count to verify against")
    return int(value)


def _plain_name(name: str) -> bool:
    # nosemgrep: ep-word-membership -- the two directory entries no file can be named.
    return bool(name) and name not in (".", "..") and Path(name).name == name and all(ord(char) >= 32 for char in name)


def _file_name(disposition: str, path: str) -> str:
    """The server's file name when it is a plain name, else the last segment of the requested path."""
    name = unquote(disposition[len(_DISPOSITION_PREFIX):]) if disposition.startswith(_DISPOSITION_PREFIX) else ""
    if not _plain_name(name):
        name = Path(path).name
    if not _plain_name(name):
        raise CloudDownloadError("FILE_NAME_UNAVAILABLE", "the response names no usable file name")
    return name


async def download_original(
    config: CloudImportClientConfig, path: str, out_dir: Path, *, api=None, overwrite: bool = False
) -> dict:
    """Stream the original to a private file in `out_dir`; keep it only if size and SHA-256 verify."""
    gateway = urlsplit(config.mcp_url)
    if gateway.scheme != "https" or not gateway.hostname or gateway.username or gateway.password:
        raise CloudDownloadError("URL_NOT_HTTPS", "the Cloud URL is not a plain https URL")
    url = f"https://{gateway.netloc}/download?path={quote(path, safe='')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    descriptor, pending_name = tempfile.mkstemp(prefix=".exomem-download-", suffix=".part", dir=out_dir)
    pending = Path(pending_name)
    own_api = api is None
    if api is None:
        api = authorized_api_client(config)
    try:
        digest, written = hashlib.sha256(), 0
        with os.fdopen(descriptor, "wb") as output:
            # No redirects: the OAuth bearer must not follow a redirect to a host the gateway did not name.
            async with api.stream("GET", url, headers={"accept-encoding": "identity"},
                                  follow_redirects=False, timeout=httpx2.Timeout(30, read=120)) as response:
                if response.is_redirect:
                    raise CloudDownloadError("DOWNLOAD_REDIRECTED", "the Cloud service redirected; refusing to follow it")
                if response.status_code != 200:
                    raise _refusal(response, await _read_refusal(response))
                sha256 = _digest(response.headers.get("x-exomem-sha256"))
                size = _count(response.headers.get("x-exomem-size"), "SIZE_UNAVAILABLE")
                if _count(response.headers.get("content-length"), "SIZE_UNAVAILABLE") != size:
                    raise CloudDownloadError("SIZE_MISMATCH", "the download length differs from the original's recorded size")
                name = _file_name(response.headers.get("content-disposition", ""), path)
                # Raw bytes, so the digest covers exactly what the service sent.
                async for chunk in response.aiter_raw(CHUNK_BYTES):
                    written += len(chunk)
                    if written > size:
                        raise CloudDownloadError("SIZE_MISMATCH", "the download is larger than the original's recorded size")
                    digest.update(chunk)
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if written != size:
            raise CloudDownloadError("SIZE_MISMATCH", "the download is smaller than the original's recorded size")
        if digest.hexdigest() != sha256:
            raise CloudDownloadError("DIGEST_MISMATCH", "the download does not match the original's recorded digest")
        final = out_dir / name
        _place(pending, final, overwrite=overwrite)
    finally:
        pending.unlink(missing_ok=True)
        if own_api:
            await api.aclose()
    return {"path": str(final), "bytes": written, "sha256": sha256}


def main(argv=None, *, api=None) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem cloud download",
        description="Download one preserved original from your Cloud, verified against its recorded size and digest.")
    parser.add_argument("path", help="Vault-relative path of the preserved original.")
    parser.add_argument("--out", type=Path, default=Path("."), help="Folder that receives the file (default: current folder).")
    parser.add_argument("--overwrite", action="store_true", help="Replace the file if it already exists.")
    add_endpoint_arguments(parser)
    args = parser.parse_args(argv)
    try:
        config = CloudImportClientConfig(args.mcp_url, args.api_base_url, args.oauth_client_id)
        result = asyncio.run(download_original(config, args.path, args.out, api=api, overwrite=args.overwrite))
    except CloudDownloadError as error:
        print(json.dumps({"ok": False, "error": error.code, "message": error.message}))
        return 1
    except Exception as error:  # noqa: BLE001 -- print the type only, never a transport message
        print(json.dumps({"ok": False, "error": type(error).__name__}))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0

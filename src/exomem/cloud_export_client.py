"""Download the owner's Cloud export over OAuth: a verified archive, or no file."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import string
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx2

from .cloud_import_client import (
    CloudImportClientConfig,
    add_endpoint_arguments,
    authorized_api_client,
)

PACKING_DEADLINE_SECONDS = 3600
LINK_DEADLINE_SECONDS = 120
POLL_SECONDS = 5.0
CHUNK_BYTES = 1 << 20


class CloudExportError(Exception):
    """A refusal whose code and message are safe to print: neither can carry the download link."""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.message = message


def _server_code(response: httpx2.Response) -> str:
    """The error code of a Cloud API refusal, or the status when the body has none."""
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        code = None
    # A format check on a protocol field, so a hostile body cannot put free text in the output.
    if isinstance(code, str) and 0 < len(code) <= 64 and code.isascii() and code.replace("_", "").isalnum():
        return code
    return f"HTTP_{response.status_code}"


async def _api_json(api: httpx2.AsyncClient, method: str, url: str) -> tuple[int, dict]:
    response = await api.request(method, url)
    if response.status_code == 404 and method == "GET":
        raise CloudExportError("EXPORTS_NOT_OFFERED", "this Cloud deployment does not offer exports yet")
    if response.is_error:
        raise CloudExportError(_server_code(response), "the Cloud API refused the request")
    return response.status_code, response.json()


async def _wait_until_ready(api: httpx2.AsyncClient, url: str, wanted: str | None, poll_seconds: float,
                            deadline_seconds: float) -> dict:
    deadline = time.monotonic() + deadline_seconds
    while True:
        _, body = await _api_json(api, "GET", url)
        display = body["export"]
        state = display["state"]
        if state == "none":
            raise CloudExportError("EXPORT_NOT_FOUND", "no export exists; request one from your assistant or Home")
        if wanted is not None and display["export_id"].lower() != wanted:
            raise CloudExportError("EXPORT_ID_MISMATCH", "the newest export is a different one; it replaced the requested export")
        # The Cloud export protocol fixes these states.
        if state == "expired":
            raise CloudExportError("EXPORT_EXPIRED", "the export expired; request a new one")
        if state == "failed":
            raise CloudExportError("EXPORT_FAILED", "the export failed; request a new one")
        if state == "ready":
            return display
        if time.monotonic() >= deadline:
            raise CloudExportError("EXPORT_STILL_PACKING", "the export is still packing; run this command again later")
        await asyncio.sleep(poll_seconds)


async def _issue_link(api: httpx2.AsyncClient, url: str, poll_seconds: float, deadline_seconds: float) -> str:
    deadline = time.monotonic() + deadline_seconds
    while True:
        status, body = await _api_json(api, "POST", url)
        if status == 200:
            return body["url"]
        if time.monotonic() >= deadline:
            raise CloudExportError("LINK_PENDING", "the download link was not issued in time; run this command again")
        await asyncio.sleep(poll_seconds)


async def _download(link: str, out_dir: Path, final: Path, size: int | None, sha256: str, transport) -> None:
    parts = urlsplit(link)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise CloudExportError("LINK_NOT_HTTPS", "the download link is not a plain https URL")
    descriptor, pending_name = tempfile.mkstemp(prefix=".exomem-export-", suffix=".part", dir=out_dir)
    pending = Path(pending_name)
    try:
        digest, written = hashlib.sha256(), 0
        with os.fdopen(descriptor, "wb") as output:
            # No auth and no redirects: the OAuth bearer never reaches storage, and a redirect
            # could carry the archive request to a host the link did not name.
            async with httpx2.AsyncClient(transport=transport, follow_redirects=False,
                                          timeout=httpx2.Timeout(30, read=120)) as storage:
                async with storage.stream("GET", link) as response:
                    if response.is_redirect:
                        raise CloudExportError("LINK_REDIRECTED", "the download link redirected; refusing to follow it")
                    if response.status_code != 200:
                        raise CloudExportError("DOWNLOAD_REFUSED", "the storage service refused the download")
                    declared = response.headers.get("content-length")
                    if size is not None and declared is not None and declared != str(size):
                        raise CloudExportError("SIZE_MISMATCH", "the download size differs from the export record")
                    # Raw bytes, so the digest covers exactly what storage sent.
                    async for chunk in response.aiter_raw(CHUNK_BYTES):
                        written += len(chunk)
                        if size is not None and written > size:
                            raise CloudExportError("SIZE_MISMATCH", "the download is larger than the export record")
                        digest.update(chunk)
                        output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size is not None and written != size:
            raise CloudExportError("SIZE_MISMATCH", "the download is smaller than the export record")
        if digest.hexdigest() != sha256:
            raise CloudExportError("DIGEST_MISMATCH", "the download does not match the export's recorded digest")
        os.replace(pending, final)
    finally:
        pending.unlink(missing_ok=True)


async def download_export(config: CloudImportClientConfig, out_dir: Path, *, export_id: str | None = None,
                          api=None, download_transport=None, poll_seconds: float = POLL_SECONDS,
                          packing_deadline: float = PACKING_DEADLINE_SECONDS,
                          link_deadline: float = LINK_DEADLINE_SECONDS) -> dict:
    """Wait for the export, fetch its short-lived link, and keep the archive only if it verifies."""
    out_dir.mkdir(parents=True, exist_ok=True)
    own_api = api is None
    if api is None:
        api = authorized_api_client(config)
    base = config.api_base_url.rstrip("/") + "/api/exomem/cloud/exports"
    try:
        display = await _wait_until_ready(api, base, export_id, poll_seconds, packing_deadline)
        # The id becomes a file name, so it must be a UUID whatever the server sent.
        ready_id = str(uuid.UUID(display["export_id"]))
        sha256 = str(display.get("archive_sha256") or "").lower()
        # A format check on a digest field: without a verifiable digest the archive is never kept.
        if len(sha256) != 64 or not all(char in string.hexdigits for char in sha256):
            raise CloudExportError("DIGEST_UNAVAILABLE", "the export record has no digest to verify against")
        size = display.get("archive_bytes")
        link = await _issue_link(api, f"{base}/{ready_id}/link", poll_seconds, link_deadline)
        final = out_dir / f"exomem-export-{ready_id}.zip"
        await _download(link, out_dir, final, size, sha256, download_transport)
    finally:
        if own_api:
            await api.aclose()
    return {"export_id": ready_id, "path": str(final), "bytes": final.stat().st_size, "sha256": sha256}


def _uuid_text(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError("must be a UUID") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem cloud export",
        description="Download your Cloud export to a folder, verified against its recorded digest.")
    parser.add_argument("--out", type=Path, required=True, help="Folder that receives the archive.")
    parser.add_argument("--export-id", type=_uuid_text, help="Only download this export.")
    add_endpoint_arguments(parser)
    args = parser.parse_args(argv)
    try:
        config = CloudImportClientConfig(args.mcp_url, args.api_base_url, args.oauth_client_id)
        result = asyncio.run(download_export(config, args.out, export_id=args.export_id))
    except CloudExportError as error:
        print(json.dumps({"ok": False, "error": error.code, "message": error.message}))
        return 1
    except Exception as error:  # noqa: BLE001 -- a transport error message can contain the download link
        print(json.dumps({"ok": False, "error": type(error).__name__}))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0

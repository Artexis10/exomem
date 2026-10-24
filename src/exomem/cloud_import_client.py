"""Resumable ciphertext-only Cloud import using the ordinary OAuth client."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import tarfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from fastmcp.client.auth import OAuth
from platformdirs import user_state_dir

from . import cloud_import_envelope
from .cell_init import _ensure_private_directory
from .cloud_import_job import _private_json


@dataclass(frozen=True)
class CloudImportClientConfig:
    mcp_url: str
    api_base_url: str
    oauth_client_id: str = "exomem-cloud-cli"

    def __post_init__(self):
        for value in (self.mcp_url, self.api_base_url):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("Cloud URLs must use HTTPS")


def authorized_api_client(config: CloudImportClientConfig) -> httpx2.AsyncClient:
    """The Cloud API client that signs in through a local browser; the one OAuth setup for every `exomem cloud` command."""
    oauth = OAuth(mcp_url=config.mcp_url, client_id=config.oauth_client_id,
                  client_name="Exomem CLI", scopes=["exomem.read", "exomem.write", "offline_access"],
                  callback_host="127.0.0.1", callback_port=None)
    return httpx2.AsyncClient(auth=oauth, timeout=60)


def add_endpoint_arguments(command: argparse.ArgumentParser) -> None:
    """The Cloud endpoint options that every `exomem cloud` command shares."""
    command.add_argument("--mcp-url", default=os.environ.get("EXOMEM_CLOUD_MCP_URL"), required=not os.environ.get("EXOMEM_CLOUD_MCP_URL"))
    command.add_argument("--api-base-url", default=os.environ.get("EXOMEM_CLOUD_API_BASE_URL"), required=not os.environ.get("EXOMEM_CLOUD_API_BASE_URL"))
    command.add_argument("--oauth-client-id", default="exomem-cloud-cli")


def _ciphertext(source: Path, target: Path, recipient: str, envelope) -> None:
    pending = target.with_suffix(".pending")
    pending.unlink(missing_ok=True)
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            process = subprocess.Popen(["age", "-r", recipient], stdin=subprocess.PIPE,
                                       stdout=output, stderr=subprocess.DEVNULL)
            try:
                with process.stdin:
                    if envelope is not None:
                        cloud_import_envelope.write(process.stdin, source, envelope)
                    else:
                        with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                            for path in sorted(source.rglob("*")):
                                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                                    raise ValueError("source contains a special entry")
                                archive.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
                # This CLI encryption process owns the archive lifetime, outside request serving.
                if process.wait() != 0:
                    raise RuntimeError("encryption failed")
                output.flush()
                os.fsync(output.fileno())
            finally:
                if process.poll() is None:
                    process.kill()
                    # Reap the killed CLI child before removing its private pending output.
                    process.wait()
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)


async def import_source(source: Path, config: CloudImportClientConfig, checkpoint: Path, *,
                        scope: str | None = None, category: str | None = None,
                        api=None, uploads=None, poll_seconds: float = 2) -> dict:
    """Reauthorize on restart and reuse the same private ciphertext and part receipts."""
    source = source.resolve()
    checkpoint = checkpoint.resolve()
    if source.is_dir() and checkpoint.is_relative_to(source):
        raise ValueError("checkpoint must be outside the source folder")
    _ensure_private_directory(checkpoint)
    if os.name == "nt":
        from .state_paths import _prepare_windows_private_state_root

        _prepare_windows_private_state_root(checkpoint)
    record = checkpoint / "checkpoint.json"
    ciphertext = checkpoint / "archive.age"
    if record.exists():
        state = json.loads(record.read_text(encoding="utf-8"))
        if state["source"] != str(source) or state["config"] != asdict(config):
            raise ValueError("checkpoint belongs to another source or Cloud endpoint")
    else:
        if source.is_file():
            if not scope or not category:
                raise ValueError("ZIP imports require --scope and --category")
            envelope = cloud_import_envelope.Envelope(scope=scope, category=category, filename=source.name)
            encoded = envelope.encoded()
            kind, files, size = "archive_members", 2, source.stat().st_size + len(encoded)
        elif source.is_dir():
            paths = [path for path in source.rglob("*") if path.is_file()]
            kind, files, size = "adopt", len(paths), sum(path.stat().st_size for path in paths)
            envelope = None
        else:
            raise ValueError("source must be a folder or ZIP")
        state = {"source": str(source), "config": asdict(config), "import_id": str(uuid.uuid4()),
                 "kind": kind, "declared_files": files, "declared_bytes": size, "parts": {},
                 "envelope": envelope.model_dump() if envelope else None}
        _private_json(record, state)
    own_api, own_uploads = api is None, uploads is None
    if api is None:
        api = authorized_api_client(config)
    if uploads is None:
        # Presigned storage URLs never receive the API's OAuth bearer.
        uploads = httpx2.AsyncClient(timeout=120)
    base = config.api_base_url.rstrip("/") + "/api/exomem/cloud/imports"
    endpoint = base + "/" + state["import_id"]

    async def call(method, url, **kwargs):
        response = await api.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json()["import"]

    async def wait_for(predicate):
        deadline = time.monotonic() + 7200
        while True:
            item = await call("GET", endpoint)
            # nosemgrep: ep-word-membership -- The Cloud import protocol fixes these terminal failure states.
            if item["observed_state"] in ("failed", "expired", "discarded"):
                ciphertext.unlink(missing_ok=True)
                record.unlink(missing_ok=True)
                raise RuntimeError(item.get("error_code") or "import ended")
            if predicate(item):
                return item
            if time.monotonic() >= deadline:
                raise TimeoutError("import remains pending; rerun with the same checkpoint")
            await asyncio.sleep(poll_seconds)

    try:
        # A client-supplied UUID makes a dropped initial response safe to replay.
        await call("POST", base, json={name: state[name] for name in (
            "import_id", "kind", "declared_bytes", "declared_files")})
        item = await wait_for(lambda row: row.get("recipient") or row["observed_state"] == "done")
        if item["observed_state"] == "done":
            ciphertext.unlink(missing_ok=True)
            record.unlink(missing_ok=True)
            return item
        if not ciphertext.exists():
            if "ciphertext_sha256" in state:
                raise ValueError("checkpoint ciphertext is missing; cancel this import before restarting")
            envelope = cloud_import_envelope.Envelope.model_validate(state["envelope"]) if state["envelope"] else None
            await asyncio.to_thread(_ciphertext, source, ciphertext, item["recipient"], envelope)
        with ciphertext.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        size = ciphertext.stat().st_size
        if state.get("ciphertext_sha256", digest) != digest or state.get("ciphertext_bytes", size) != size:
            raise ValueError("checkpoint ciphertext changed")
        state.update(ciphertext_sha256=digest, ciphertext_bytes=size)
        _private_json(record, state)
        cipher = {"ciphertext_sha256": digest, "ciphertext_bytes": size}
        if item["requested_state"] == "requested":
            await call("POST", endpoint + "/prepare-upload", json=cipher)
            item = await wait_for(lambda row: bool(row.get("part_urls")))
            parts = item["part_urls"]
            if sum(part["part_size_bytes"] for part in parts) != size:
                raise ValueError("issued parts do not cover the ciphertext")
            with ciphertext.open("rb") as stream:
                for ordinal, part in enumerate(parts, 1):
                    if part["part_number"] != ordinal:
                        raise ValueError("issued part order is invalid")
                    payload = stream.read(part["part_size_bytes"])
                    if len(payload) != part["part_size_bytes"]:
                        raise ValueError("ciphertext ended early")
                    if str(ordinal) in state["parts"]:
                        continue
                    response = await uploads.put(part["url"], content=payload,
                                                 headers={"Content-Length": str(len(payload))})
                    response.raise_for_status()
                    state["parts"][str(ordinal)] = response.headers["etag"]
                    _private_json(record, state)
            await call("POST", endpoint + "/complete", json={**cipher, "part_etags": [
                {"part_number": part["part_number"], "etag": state["parts"][str(part["part_number"]) ]}
                for part in parts]})
        result = await wait_for(lambda row: row["observed_state"] == "done")
        ciphertext.unlink(missing_ok=True)
        record.unlink(missing_ok=True)
        return result
    finally:
        if own_api:
            await api.aclose()
        if own_uploads:
            await uploads.aclose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem cloud", epilog="To download an owner export, run: exomem cloud export --help")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("import")
    command.add_argument("source", type=Path)
    add_endpoint_arguments(command)
    command.add_argument("--scope")
    command.add_argument("--category")
    command.add_argument("--checkpoint", type=Path)
    args = parser.parse_args(argv)
    config = CloudImportClientConfig(args.mcp_url, args.api_base_url, args.oauth_client_id)
    key = hashlib.sha256((str(args.source.resolve()) + config.api_base_url).encode()).hexdigest()
    checkpoint = args.checkpoint or Path(user_state_dir("exomem")) / "cloud-imports" / key
    try:
        result = asyncio.run(import_source(args.source, config, checkpoint, scope=args.scope, category=args.category))
    except Exception as error:  # noqa: BLE001 -- provider errors may contain presigned URLs
        print(json.dumps({"ok": False, "error": type(error).__name__}))
        return 1
    print(json.dumps({"ok": True, "import_id": result["import_id"], "state": result["observed_state"]}))
    return 0
